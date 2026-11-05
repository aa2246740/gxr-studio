// 大图（编辑器）：顶栏（返回 + 撤销 + 照片信息）、画面、视图工具栏、胶片条、右侧工具面板（固定底栏）。
// 默认标签是「配方」。大图始终显示 stage=final 的成片；只有「裁剪」标签才切到 stage=uncropped。
// 面板每个标签只建一次，切换标签只改 hidden；编辑记录变了只重建当前标签（有 sync() 的只改值），别的标签下次显示时再建。

import { api } from './api.js';
import { state, on } from './state.js';
import { h, button, icon, toast, fmtClock, fmtDate, toastError, modKey } from './dom.js';
import { pairTag } from './gallery.js';
import { openExportDialog } from './export.js';
import { recipePanel } from './recipes.js';
import { adjustPanel } from './adjust.js';
import { cropSection, cycleGuide, cycleSpiral, closeMenus } from './crop.js';
import { correctSection, correctActions, nudgeStraighten, rotate90 } from './correct.js';
import { framePanel } from './frame_panel.js';
import { stampPanel } from './stamp_panel.js';
import { infoPanel } from './info_panel.js';
import { paintStage } from './stage.js';
import {
  ensureEdit, loadCrop, schedulePreview, disposeEdit, undoChange, redoChange, syncUndo, beginChange, endChange,
  persist, reloadEdit, applyRetried, stageLabels, refreshCaps, resetCropRemote, adjustBody,
  saveStatus, retrySaves, discardFailed,
} from './preview.js';
import { fetchAllPhotos } from './photos.js';
import { delayed, busyButton, SHOW_AFTER_MS } from './busy.js';
import { DEFAULT_GEOMETRY, normalizeFrame, normalizeStamp } from './finish.js';

const zeroAdj = () => ({
  exposure_ev: 0, contrast: 0, highlight: 0, shadow: 0, black_point: 0,
  warm: 0, tint: 0, saturation: 0, vibrance: 0, vignette: 0,
});

// [id, 名称, 快捷键, 图标]。「导出」不再是标签，改成对话框（⌘E）；第六个是「信息」（I）
export const TABS = [
  ['recipe', '配方', 'R', 'swatch-book'],
  ['adjust', '调整', 'A', 'sliders-horizontal'],
  ['crop', '裁剪', 'C', 'crop'],
  ['frame', '边框', 'F', 'frame'],
  ['stamp', '时间戳', 'T', 'calendar-clock'],
  ['info', '信息', 'I', 'info'],
];

// 画面背景三选一（存在本机浏览器）
export const BACKGROUNDS = [['dark', '深灰', '#2a2a2a'], ['mid', '18% 灰', '#767676'], ['black', '黑', '#000000']];
const BG_KEY = 'gxr-studio.stage-bg';
const PANEL_KEY = 'gxr-studio.panel-collapsed';
const FILM_WINDOW = 40; // 胶片条只放当前照片前后各 40 张，几千张照片也不卡

let view = null;
let resizeObs = null;

function pref(key, dflt) {
  try { return localStorage.getItem(key) ?? dflt; } catch { return dflt; }
}
function setPref(key, value) {
  try { localStorage.setItem(key, value); } catch { /* 隐私模式写不进去就算了 */ }
}

// ---- 键盘 -------------------------------------------------------------------

export function viewerKeys() {
  if (!view) return null;
  return {
    down(e) {
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        if (isSliderFocus()) return false;
        step(e.key === 'ArrowLeft' ? -1 : 1);
        return true;
      }
      if (e.key === ' ' && !e.repeat) {
        if (isButtonFocus()) return false;
        setShowOriginal(true);
        return true;
      }
      if (e.key === '\\') {
        view.pinOriginal = !view.pinOriginal;
        setShowOriginal(view.pinOriginal);
        return true;
      }
      if (e.key === 'y' || e.key === 'Y') {
        setSplit(!view.split);
        return true;
      }
      if (e.key === 'z' || e.key === 'Z') {
        setZoom(!view.zoom100);
        return true;
      }
      if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        if (view.tab !== 'crop' || isSliderFocus()) return false;
        nudgeStraighten(view, e.key === 'ArrowUp' ? 0.5 : -0.5);
        return true;
      }
      // 参考线用 O（G 在 Lightroom 里是「回到图库」）；⇧O 转螺旋方向
      if (view.tab === 'crop' && (e.key === 'o' || e.key === 'O')) {
        if (e.shiftKey) cycleSpiral(view);
        else cycleGuide(view);
        return true;
      }
      if (view.tab === 'crop' && e.key === '[') {
        rotate90(view);
        return true;
      }
      const tab = TABS.find((t) => t[2].toLowerCase() === e.key.toLowerCase());
      if (tab && !e.shiftKey) {
        setTab(tab[0]);
        return true;
      }
      return false;
    },
    up(e) {
      if (e.key === ' ' && view && view.showOriginal && !view.pinOriginal) {
        setShowOriginal(false);
        return true;
      }
      return false;
    },
    undo() {
      if (!view) return false;
      undoChange(view);
      return true;
    },
    redo() {
      if (!view) return false;
      redoChange(view);
      return true;
    },
  };
}

function isSliderFocus() {
  const a = document.activeElement;
  return !!a && ((a.tagName === 'INPUT' && a.type === 'range') || a.classList.contains('ruler'));
}

function isButtonFocus() {
  const a = document.activeElement;
  return !!a && a.tagName === 'BUTTON';
}

function setShowOriginal(on) {
  if (!view || view.showOriginal === on) return;
  view.showOriginal = on;
  if (!on) view.pinOriginal = false;
  syncViewbar();
  paintStage(view);
  paintLabels(view);
}

function setSplit(on) {
  if (!view) return;
  view.split = on;
  syncViewbar();
  paintStage(view);
  paintLabels(view);
}

function setZoom(on) {
  if (!view) return;
  view.zoom100 = on;
  syncViewbar();
  paintStage(view);
}

function setBackground(id) {
  if (!view) return;
  view.bg = id;
  setPref(BG_KEY, id);
  view.stageCol.dataset.bg = id;
  syncViewbar();
}

function setCollapsed(on) {
  if (!view) return;
  view.collapsed = on;
  setPref(PANEL_KEY, on ? '1' : '0');
  view.editor.classList.toggle('collapsed', on);
  view.panelEl.setAttribute('aria-hidden', on ? 'true' : 'false');
  if (on) view.panelEl.setAttribute('inert', '');
  else view.panelEl.removeAttribute('inert');
  syncViewbar();
  // 面板收起/展开后画面的可用宽度变了：等过渡结束再按新尺寸排一次（ResizeObserver 也会跟上）
  setTimeout(() => { if (view) paintStage(view); }, 180);
}

// ---- 标签 -------------------------------------------------------------------

// 进入「裁剪」：画面切到未裁剪的整张（stage=uncropped）；离开：回到成片（stage=final）
export function setTab(tab) {
  if (!view || view.tab === tab) return;
  if (!TABS.some(([id]) => id === tab)) return;
  const wasCrop = view.tab === 'crop';
  if (tab === 'crop') view.lastTab = view.tab;
  view.tab = tab;
  state.viewerTab = tab === 'crop' ? 'recipe' : tab;
  view.stage = tab === 'crop' ? 'uncropped' : 'final';
  if (view.collapsed) setCollapsed(false);
  if (view.ed && (tab === 'crop' || wasCrop)) schedulePreview(view, { full: true });
  showTab();
  paintStage(view);
  paintLabels(view);
}

function tabButton(id, label, key, ico) {
  const b = h('button', {
    type: 'button', role: 'tab', id: `tab-${id}`, 'aria-controls': `tp-${id}`,
    'aria-selected': 'false', tabindex: '-1', title: `${label}（${key}）`,
    onclick: () => setTab(id),
  }, icon(ico), h('span', {}, label), h('i', { class: 'mod', hidden: true, 'aria-hidden': 'true' }));
  return b;
}

// 标签列表：←→ / Home / End 移动并切换（WAI-ARIA 标签页的做法）
function onTabKey(e) {
  const ids = TABS.map(([id]) => id);
  const i = ids.indexOf(view.tab);
  let next = null;
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = ids[(i + 1) % ids.length];
  else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = ids[(i - 1 + ids.length) % ids.length];
  else if (e.key === 'Home') next = ids[0];
  else if (e.key === 'End') next = ids[ids.length - 1];
  if (!next) return;
  e.preventDefault();
  e.stopPropagation();
  setTab(next);
  view.tabBtns[next].focus();
}

function showTab() {
  if (!view) return;
  for (const [id] of TABS) {
    const on = id === view.tab;
    const btn = view.tabBtns[id];
    btn.setAttribute('aria-selected', String(on));
    btn.setAttribute('tabindex', on ? '0' : '-1');
    const p = view.panes[id];
    p.el.hidden = !on;
    if (on && (!p.built || p.dirty)) buildPane(id);
  }
  paintTabMods(view);
}

function buildPane(id) {
  const p = view.panes[id];
  const content = panelFor(id);
  p.content = content;
  p.el.replaceChildren(content);
  p.built = true;
  p.dirty = false;
}

// 编辑记录变了：当前标签有 sync() 的只改值，没有的重建；其它已经建过的标签标记为「下次显示时重建」
function refreshPanes() {
  if (!view) return;
  for (const [id] of TABS) {
    const p = view.panes[id];
    if (!p.built) continue;
    if (id !== view.tab) {
      p.dirty = true;
      continue;
    }
    if (view.ed && p.content && typeof p.content.sync === 'function' && p.content.dataset.ready === '1') p.content.sync();
    else buildPane(id);
  }
  paintTabMods(view);
}

function panelFor(tab) {
  if (tab === 'info') return infoPanel(view);
  if (!view.ed) {
    if (view.edError) {
      return h('div', { class: 'pane' }, h('p', { class: 'small bad-text' }, view.edError.message || '无法读取编辑记录。请重试。'),
        button('重试', { ico: 'rotate-ccw', onclick: () => retryEdit(view) }));
    }
    // 编辑记录还没到：骨架占位，不写「正在读取」
    return h('div', { class: 'pane', 'aria-busy': 'true' }, h('div', { class: 'skel line' }), h('div', { class: 'skel line' }), h('div', { class: 'skel line' }));
  }
  switch (tab) {
    case 'recipe': return recipePanel(view);
    case 'adjust': return adjustPanel(view);
    case 'crop': return cropPanelWrap();
    case 'frame': return framePanel(view);
    case 'stamp': return stampPanel(view);
    default: return h('p', {}, '');
  }
}

async function retryEdit(v) {
  v.edError = null;
  v.edPromise = null;
  refreshPanes();
  try {
    await ensureEdit(v);
  } catch (e) {
    v.edError = e;
  }
  if (view !== v) return;
  refreshPanes();
  schedulePreview(v, { full: true });
}

function cropPanelWrap() {
  return h('div', { class: 'pane' },
    view.canvas ? cropSection(view) : h('div', { class: 'skel-group', 'aria-busy': 'true' }, h('div', { class: 'skel line' }), h('div', { class: 'skel line' })),
    correctSection(view),
    h('div', { class: 'sticky-foot' }, ...correctActions(view), h('button', { type: 'button', class: 'btn sm primary end', onclick: finishCrop }, '完成')),
  );
}

function finishCrop() {
  setTab(view.lastTab && view.lastTab !== 'crop' ? view.lastTab : 'recipe');
}

// 标签上的小蓝点：这一类有改动
function paintTabMods(v) {
  if (!v || !v.tabBtns) return;
  const ed = v.ed;
  const adj = ed ? Object.values(ed.adj || {}).some((x) => Number(x) !== 0) || !!ed.curve : false;
  const g = ed && ed.v2 ? ed.geometry || {} : {};
  const crop = !!(v.crop && v.crop.rect && (v.crop.rect.w < 0.999 || v.crop.rect.h < 0.999)) || !!(g.straighten || g.vertical || g.horizontal || g.distortion || g.rotate || g.flip);
  const mods = {
    recipe: !!(ed && (ed.recipe || ed.lut)),
    adjust: adj,
    crop,
    frame: !!(ed && ed.frame && ed.frame.style !== 'none'),
    stamp: !!(ed && ed.stamp && ed.stamp.enabled),
    info: false,
  };
  for (const [id] of TABS) {
    const dot = v.tabBtns[id].querySelector('.mod');
    if (dot) dot.hidden = !mods[id];
  }
}

// ---- 画面上的小标签（电脑预测 / 未校准 / 近似 / 原图）。普通结果不写「成片」 -------------

// 服务器出图时画面顶上一条细进度条（不挡画面、不挡操作）；150ms 内出完就不显示（busy.js 的分档）
function paintProgress(v) {
  // 编辑记录还在读、或服务器在出图
  const busy = v.ed ? !!v.ed.busy : !v.edError;
  if (busy) {
    if (!v.progTimer) v.progTimer = delayed(() => v.progressEl.classList.add('on'), SHOW_AFTER_MS);
    return;
  }
  v.progTimer?.cancel();
  v.progTimer = null;
  v.progressEl.classList.remove('on');
}

function paintLabels(v) {
  if (!view || v !== view || !v.labelsEl) return;
  const items = [];
  if (v.showOriginal) items.push(h('span', { class: 'tag' }, '原图'));
  else if (v.split) items.push(h('span', { class: 'tag' }, '左：原图'), h('span', { class: 'tag' }, '右：编辑后'));
  for (const l of v.showOriginal ? [] : stageLabels(v)) {
    items.push(h('span', { class: `tag ${l.kind === 'warn' ? 'warn' : ''}`, title: l.title || null }, l.text));
  }
  v.labelsEl.replaceChildren(...items);
}

// ---- 顶栏（编辑版）：返回、撤销、照片名和拍摄时间 ------------------------------------

function renderTitlebar(v) {
  const bar = document.getElementById('titlebar');
  const slot = document.getElementById('tb-edit');
  if (bar) bar.dataset.mode = 'viewer';
  if (!slot) return;
  const p = v.photo;
  const undo = button('', { kind: 'icon', ico: 'undo-2', title: `撤销（${modKey()}Z）`, onclick: () => undoChange(v) });
  v.undoBtn = undo;
  syncUndo(v);
  slot.replaceChildren(...[
    button('照片', { kind: 'ghost', ico: 'chevron-left', title: '回到图库（Esc）', onclick: () => { location.hash = '#/photos'; } }),
    h('span', { class: 'sep-v' }),
    undo,
    h('div', { class: 'tb-title' },
      h('span', { class: 'name' }, p.name),
      h('span', { class: 'meta' }, `${fmtDate(p.date)} ${fmtClock(p.captured_at)}`),
      pairTag(p),
      p.date_source === 'mtime' ? h('span', { class: 'tag warn', title: '没有拍摄时间，使用文件修改日期' }, '使用文件修改日期') : null),
  ].filter(Boolean));
}

function clearTitlebar() {
  const bar = document.getElementById('titlebar');
  const slot = document.getElementById('tb-edit');
  if (bar && bar.dataset.mode === 'viewer') bar.dataset.mode = 'library';
  if (slot) slot.replaceChildren();
}

// ---- 视图工具栏：背景、原图、左右对比 | 上一张 n/N 下一张 | 适合/100%、收起面板 ----------------

function renderViewbar(v) {
  const bgs = BACKGROUNDS.map(([id, name, hex]) => {
    const b = h('button', { type: 'button', class: 'swatch', title: `背景：${name}`, 'aria-label': `背景：${name}`, 'aria-pressed': 'false', onclick: () => setBackground(id) }, h('i', {}));
    b.firstChild.style.background = hex;
    b.dataset.bg = id;
    return b;
  });
  const orig = button('', { kind: 'icon', ico: 'eye', title: '原图（\\，按住空格临时看）', onclick: () => { v.pinOriginal = !v.showOriginal; setShowOriginal(!v.showOriginal); } });
  const split = button('', { kind: 'icon', ico: 'columns-2', title: '左右对比（Y）', onclick: () => setSplit(!v.split) });
  const prev = button('', { kind: 'icon', ico: 'chevron-left', title: '上一张（←）', onclick: () => step(-1) });
  const next = button('', { kind: 'icon', ico: 'chevron-right', title: '下一张（→）', onclick: () => step(1) });
  const counter = h('span', { class: 'info num', 'aria-live': 'off' });
  const zoom = h('div', { class: 'seg', role: 'group', 'aria-label': '缩放' },
    h('button', { type: 'button', 'aria-pressed': 'true', title: '适合（Z）', onclick: () => setZoom(false) }, '适合'),
    h('button', { type: 'button', 'aria-pressed': 'false', title: '100%（Z）', onclick: () => setZoom(true) }, '100%'));
  const panelBtn = button('', { kind: 'icon', ico: 'panel-right', title: '显示 / 隐藏右侧面板', onclick: () => setCollapsed(!v.collapsed) });
  v.vb = { bgs, orig, split, prev, next, counter, zoom, panelBtn };
  v.viewbar.replaceChildren(...bgs, h('span', { class: 'sep-v' }), orig, split, h('span', { class: 'grow' }), prev, counter, next, h('span', { class: 'grow' }), zoom, panelBtn);
  syncViewbar();
}

function syncViewbar() {
  const v = view;
  if (!v || !v.vb) return;
  const { bgs, orig, split, prev, next, counter, zoom, panelBtn } = v.vb;
  for (const b of bgs) b.setAttribute('aria-pressed', String(b.dataset.bg === v.bg));
  orig.setAttribute('aria-pressed', String(!!v.showOriginal));
  split.setAttribute('aria-pressed', String(!!v.split));
  const pos = state.photos.findIndex((p) => p.id === v.photo.id);
  const disable = (b, off) => (off ? b.setAttribute('aria-disabled', 'true') : b.removeAttribute('aria-disabled'));
  disable(prev, pos <= 0);
  disable(next, pos < 0 || pos >= state.photos.length - 1);
  counter.textContent = pos >= 0 ? `${pos + 1} / ${state.photos.length}` : '';
  const [fit, full] = zoom.children;
  fit.setAttribute('aria-pressed', String(!v.zoom100));
  full.setAttribute('aria-pressed', String(!!v.zoom100));
  panelBtn.setAttribute('aria-pressed', String(!v.collapsed));
}

// ---- 胶片条（只在画面下方） -------------------------------------------------------

function renderFilmstrip(v) {
  const list = state.photos;
  const pos = list.findIndex((p) => p.id === v.photo.id);
  if (pos < 0 || list.length < 2) {
    v.film.replaceChildren();
    v.stageCol.classList.add('nofilm');
    return;
  }
  v.stageCol.classList.remove('nofilm');
  const from = Math.max(0, pos - FILM_WINDOW);
  const to = Math.min(list.length, pos + FILM_WINDOW + 1);
  const items = list.slice(from, to).map((p) => {
    const img = h('img', { alt: '', loading: 'lazy', decoding: 'async', draggable: 'false' });
    img.addEventListener('load', () => img.classList.add('ready'));
    img.src = p.thumb_url;
    const cur = p.id === v.photo.id;
    return h('button', {
      type: 'button', class: 'fthumb', title: p.name, 'aria-label': p.name, 'aria-current': cur ? 'true' : null,
      onclick: () => { if (!cur) location.hash = `#/photo/${p.id}`; },
    }, img, p.hdr === 'ready' ? h('span', { class: 'b' }, h('span', { class: 'badge hdr', title: '图库里已生成 HDR 版本' }, 'HDR')) : null);
  });
  v.film.replaceChildren(...items);
  const current = v.film.querySelector('[aria-current="true"]');
  if (current && current.scrollIntoView) current.scrollIntoView({ block: 'nearest', inline: 'center' });
}

// ---- 面板底栏：保存状态（安静显示，不弹提示）+ 复原 -----------------------------------------

function paintSaveState(v) {
  if (!v.saveEl) return;
  const st = saveStatus(v.photo.id);
  const el = v.saveEl;
  el.dataset.state = st;
  if (st === 'saving') el.replaceChildren('正在保存…');
  else if (st === 'saved') el.replaceChildren(icon('check', 'sm'), '已保存');
  else if (st === 'failed') {
    el.replaceChildren(
      h('span', { class: 'bad-text' }, '有修改没有保存'),
      h('button', { type: 'button', class: 'btn ghost sm', onclick: () => retrySaves(v.photo.id) }, '重试'),
      h('button', { type: 'button', class: 'btn ghost sm', onclick: () => discardFailed(v) }, '放弃修改'),
    );
  } else el.replaceChildren();
}

on('save-status', (e) => {
  if (view && e.detail && e.detail.id === view.photo.id) paintSaveState(view);
});

// 重试保存成功（可能是切走又切回来以后才点的重试）：面板换成刚补写进去的内容，
// 不然面板还显示旧值，下一次调整会把它又盖回去（F03）。用写入的回应，不另读（N02）
on('save-retried', (e) => {
  const v = view;
  if (!v || !e.detail || e.detail.id !== v.photo.id || !v.ed) return;
  if (!applyRetried(v, e.detail)) return;
  // 正在拖曲线、滑块（beginChange 之后、endChange 之前）：不重建面板，不然拖着的控件和它松手时的保存都没了；
  // 这一步结束时 endChange 再刷新（W01）。每个开始都有结束：保存后 endChange，没改值的按下由滑块的 onCancel 结束（X01）
  if (v.pending) {
    v.panesStale = true;
    paintTabMods(v);
    return;
  }
  refreshPanes();
});

function renderPanelFoot(v) {
  // 点了马上锁住，两笔写入（编辑、裁剪）排完之前再点没有反应（F09）
  const resetBtn = button('复原', { kind: 'ghost', ico: 'rotate-ccw', title: '整张回到原图（可以撤销）', onclick: () => busyButton(resetBtn, resetAll) });
  v.saveEl = h('span', { class: 'save-state', role: 'status' });
  paintSaveState(v);
  v.panelFoot.replaceChildren(
    v.saveEl,
    h('span', { class: 'grow' }),
    resetBtn,
  );
}

// 复原：配方、调整、曲线、几何、边框、时间戳、裁剪全部回到原图。能用撤销找回来。
// 开始时就把照片视图绑定成局部变量（R03）：两笔写入都在等待之前排进这张照片的保存队列；
// 等待返回后，只有它还是当前视图才更新界面。中途切到别的照片，不会对那张照片写任何东西。
async function resetAll() {
  const v = view;
  const ed = v && v.ed;
  if (!ed) return;
  beginChange(v);
  ed.recipe = null;
  ed.lut = null;
  ed.adj = zeroAdj();
  ed.curve = null;
  const changes = { recipe: null, lut: null, adjust: adjustBody(ed), curve: null };
  if (ed.v2) {
    // 复原明确关掉装饰（PRD 第 11 节第 6 条）：不写 null，否则后端会按「继承默认值」处理
    Object.assign(changes, { geometry: { ...DEFAULT_GEOMETRY }, frame: { style: 'none' }, stamp: { enabled: false } });
  }
  ed.geometry = ed.v2 ? { ...DEFAULT_GEOMETRY } : ed.geometry;
  ed.frame = normalizeFrame({ style: 'none' });
  ed.frameFrom = 'photo';
  ed.stamp = normalizeStamp({ enabled: false });
  ed.stampFrom = 'photo';
  // 乐观更新：面板和画面立刻回到原图，保存在后台排队。
  // 和其它修改一样，保存失败时界面保留复原后的样子（不自动改回去），底栏标成「有修改没有保存」，
  // 给「重试 / 放弃修改」；放弃就按服务器上的内容重读。只写进去一半（比如裁剪成功、编辑失败）也一样：
  // 重试只补写没写进去的那部分。
  if (view === v) {
    refreshPanes();
    schedulePreview(v, { full: true });
  }
  const saved = persist(v, changes);
  const cropSaved = resetCropRemote(v);
  const results = await Promise.all([saved, cropSaved]);
  if (view !== v || v.ed !== ed) return;
  if (!results.every(Boolean)) {
    // 没写进去的那部分已经记在保存记录里，底栏会显示；撤销记录照常留着
    endChange(v);
    refreshPanes();
    return;
  }
  endChange(v);
  schedulePreview(v, { full: true });
  refreshPanes();
}

function step(d) {
  if (!view) return;
  const list = state.photos;
  const i = list.findIndex((p) => p.id === view.photo.id);
  const next = list[i + d];
  if (next) location.hash = `#/photo/${next.id}`;
}

// 胶片条和上一张/下一张要完整的列表。图库只取了第一页就被打开（照片多于 500 张）时，
// state.photosComplete 是 false：照片已经在列表里也要接着取完，取完后补画胶片条和顶栏的序号（F07）
async function ensurePhotos(id, isCurrent) {
  const have = state.photos.some((p) => p.id === id);
  if (have && state.photosComplete !== false) return;
  const date = state.galleryDate;
  const all = await fetchAllPhotos(date, { isCurrent }).catch(() => null);
  if (!all || !isCurrent() || state.galleryDate !== date) return;
  state.photos = all;
  state.photosComplete = true;
  if (view && view.photo.id === id) {
    renderFilmstrip(view);
    syncViewbar();
  }
}

function initialTab() {
  const t = state.viewerTab;
  return TABS.some(([id]) => id === t) && t !== 'crop' ? t : 'recipe';
}

// ---- 打开、关闭 ---------------------------------------------------------------

function editorSkeleton() {
  return h('section', { class: 'editor', 'aria-busy': 'true', 'aria-label': '正在打开照片' },
    h('div', { class: 'stagecol' }, h('div', { class: 'stage-area' }, h('div', { class: 'skel stage-skel' })), h('div', { class: 'viewbar' }), h('div', { class: 'filmstrip' })),
    h('aside', { class: 'panel' }, h('div', { class: 'tooltabs' }), h('div', { class: 'panel-body' }, h('div', { class: 'pane' }, h('div', { class: 'skel line' }), h('div', { class: 'skel line' }), h('div', { class: 'skel line' })))));
}

function buildLayout(root, photo) {
  const stageArea = h('div', { class: 'stage-area' });
  const labelsEl = h('div', { class: 'stage-labels', 'aria-live': 'polite' });
  const progressEl = h('div', { class: 'stage-progress', 'aria-hidden': 'true' });
  const viewbar = h('div', { class: 'viewbar', role: 'toolbar', 'aria-label': '视图' });
  const film = h('div', { class: 'filmstrip', role: 'group', 'aria-label': '胶片条' });
  const stageCol = h('div', { class: 'stagecol' }, h('div', { class: 'stage-shell' }, stageArea, labelsEl, progressEl), viewbar, film);
  const tabBtns = {};
  const panes = {};
  const tablist = h('div', { class: 'tooltabs', role: 'tablist', 'aria-label': '工具' });
  const body = h('div', { class: 'panel-body' });
  for (const [id, label, key, ico] of TABS) {
    tabBtns[id] = tabButton(id, label, key, ico);
    tablist.append(tabBtns[id]);
    const el = h('div', { role: 'tabpanel', id: `tp-${id}`, 'aria-labelledby': `tab-${id}`, hidden: true, tabindex: '0' });
    panes[id] = { el, built: false, dirty: false, content: null };
    body.append(el);
  }
  tablist.addEventListener('keydown', onTabKey);
  const panelFoot = h('div', { class: 'panel-foot' });
  const panelEl = h('aside', { class: 'panel', 'aria-label': '编辑' }, tablist, body, panelFoot);
  const editor = h('section', { class: 'editor', 'aria-label': photo.name }, stageCol, panelEl);
  root.replaceChildren(editor);
  return { stageArea, labelsEl, progressEl, viewbar, film, stageCol, tabBtns, panes, panelFoot, panelEl, editor };
}

// isCurrent：路由层给的「我还是当前路由吗」（R11）。每次 await 回来、改页面或装快捷键之前都先问一次，
// 过时的照片响应不能把页面换回旧照片。
export async function renderViewer(root, id, isCurrent = () => true) {
  // 从别的页面进来、照片信息 150ms 内还没到：先放画面骨架（从图库进来时原来的页面留着，不闪）
  const skel = root.querySelector && !root.querySelector('.editor') ? delayed(() => {
    if (isCurrent() && !view) root.replaceChildren(editorSkeleton());
  }, SHOW_AFTER_MS) : null;
  // 照片列表（胶片条、上一张/下一张要用）和这张照片同时去读，不排队
  const listP = ensurePhotos(id, isCurrent);
  // 图库里已经有这张照片的信息：直接用它搭页面，缩略图当占位，和切路由在同一帧出来；
  // 最新的照片信息在后台再读一次（refreshPhoto）
  const cached = state.photos.find((p) => p.id === id) || null;
  let photo = cached;
  try {
    if (!photo) photo = await api(`/api/photos/${id}`);
  } catch (e) {
    skel?.cancel();
    if (!isCurrent()) return;
    teardown();
    root.replaceChildren(h('div', { class: 'empty' }, h('h2', {}, '找不到这张照片'), h('p', { class: 'dim' }, e.message), h('div', { class: 'acts' }, h('a', { class: 'btn', href: '#/photos' }, '回到照片'))));
    return;
  }
  if (!isCurrent()) {
    skel?.cancel();
    return;
  }
  if (!cached) await listP;
  skel?.cancel();
  if (!isCurrent()) return;
  teardown();

  const tab = initialTab();
  const parts = buildLayout(root, photo);
  const bg = BACKGROUNDS.some(([b]) => b === pref(BG_KEY, 'dark')) ? pref(BG_KEY, 'dark') : 'dark';
  const collapsed = pref(PANEL_KEY, '0') === '1';

  view = {
    photo, tab, lastTab: 'recipe', stage: tab === 'crop' ? 'uncropped' : 'final', showOriginal: false, pinOriginal: false,
    split: false, zoom100: false, bg, collapsed: false,
    ...parts, statusEl: null,
    undo: [], redo: [], pending: null, saveQ: null, ed: null, edPromise: null,
    crop: null, canvas: null, zoom: 100, presetItem: null, presetPortrait: false,
    box: null, pixelsEl: null, rulerEl: null, refreshAngle: null, onRectDrag: null, mode: null, edError: null,
  };
  const v = view;
  v.stageCol.dataset.bg = bg;
  v.onChange = () => {
    if (view !== v) return;
    paintProgress(v);
    paintStage(v);
    paintLabels(v);
    paintTabMods(v);
  };
  v.redrawSide = () => {
    if (view === v) refreshPanes();
  };

  renderTitlebar(v);
  renderViewbar(v);
  renderFilmstrip(v);
  renderPanelFoot(v);
  if (collapsed) setCollapsed(true);
  showTab();
  paintStage(v);
  paintProgress(v);

  if (cached) refreshPhoto(v);

  // 两个请求都等完再画（Promise.all 会在第一个失败时就返回，另一个回来后界面不会重画，留下骨架）
  const [cropRes, edRes] = await Promise.allSettled([loadCrop(v), ensureEdit(v)]);
  if (view !== v) return;
  if (cropRes.status === 'rejected') {
    toastError(cropRes.reason, '无法读取裁剪。请重试。');
    v.crop = { preset: 'original', rect: { x: 0, y: 0, w: 1, h: 1 }, angle_deg: 0, active: false, frame: { width: photo.width || 1, height: photo.height || 1 }, rev: 0 };
  }
  if (edRes.status === 'rejected') v.edError = edRes.reason;
  // 编辑记录坏了：按原图显示，并说明（COPY 3.9）
  if ((v.crop && v.crop.warning) || (v.ed && v.ed.warning)) toast('无法读取编辑记录，已按原图显示', 'info');

  refreshPanes();
  paintStage(v);
  paintLabels(v);
  schedulePreview(v, { full: true });
  // 编辑记录读不出来：进度条收起（读到了就交给出图的 onChange，中间不闪一下）
  if (!v.ed) paintProgress(v);

  resizeObs = new ResizeObserver(() => {
    if (view === v) paintStage(v);
  });
  resizeObs.observe(v.stageArea);
}

function teardown() {
  if (resizeObs) {
    resizeObs.disconnect();
    resizeObs = null;
  }
  closeMenus();
  if (view) {
    view.progTimer?.cancel();
    disposeEdit(view);
  }
  view = null;
}

// 当前打开的照片视图（没有打开就是 null）。面板拿到的 view 和这里是同一个对象。
export function currentView() {
  return view;
}

// 离开大图（路由切换时调用）：停掉观察器和定时器，选中的照片不动
export function leaveViewer() {
  teardown();
  clearTitlebar();
}

// 相机数据或配方文件换了：照片的画面和能力都要重读
// 用图库里的照片信息打开时：后台再读一次这张照片，名字、拍摄时间等有变化就更新顶栏
async function refreshPhoto(v) {
  try {
    const photo = await api(`/api/photos/${v.photo.id}`);
    if (view !== v) return;
    v.photo = photo;
    renderTitlebar(v);
  } catch {
    // 读不到不影响已经打开的页面
  }
}

export async function refreshViewerPhoto() {
  const v = view;
  if (!v) return;
  try {
    const photo = await api(`/api/photos/${v.photo.id}`);
    if (view !== v) return;
    v.photo = photo;
  } catch {
    // 照片读不到不影响其它刷新
  }
  if (view !== v) return;
  await refreshCaps(v);
  if (v.ed) await reloadEdit(v);
  paintLabels(v);
}

// ⌘E 在大图里：打开这一张的导出对话框（导出前先等这张照片的修改写完）
export function openViewerTab(tab) {
  if (!view) return;
  if (tab === 'export') {
    const v = view;
    // 导出前等哪些照片的保存由导出对话框按选的范围决定（这一张 / 这一天的全部，F02）
    openExportDialog({
      ids: [v.photo.id], label: '这一张', photos: [v.photo],
      date: v.photo.date || null, dayLabel: v.photo.date ? `${fmtDate(v.photo.date)} 的全部` : null,
    });
    return;
  }
  setTab(tab);
}

export function exportCurrentPhoto() {
  openViewerTab('export');
}
