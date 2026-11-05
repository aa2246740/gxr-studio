// 大图：照片条、画面区（成片 / 裁剪中 / 原图）、右侧六个标签、底栏。
// 默认标签是「配方」（不是裁剪）。大图始终显示 stage=final 的成片；只有「裁剪」标签才切到 stage=uncropped（DIAG 原因 1）。

import { api } from './api.js';
import { state, on } from './state.js';
import { h, toast, fmtClock, fmtDate, toastError } from './dom.js';
import { pairTag } from './gallery.js';
import { exportForm } from './export.js';
import { recipePanel } from './recipes.js';
import { adjustPanel } from './adjust.js';
import { cropSection, cycleGuide, cycleSpiral, closeMenus } from './crop.js';
import { correctSection, correctActions, nudgeStraighten, rotate90 } from './correct.js';
import { framePanel } from './frame_panel.js';
import { stampPanel } from './stamp_panel.js';
import { paintStage, stageMode } from './stage.js';
import {
  ensureEdit, loadCrop, schedulePreview, disposeEdit, undoChange, syncUndo, beginChange, endChange,
  persist, flushEdits, reloadEdit, stageLabels, refreshCaps, resetCropRemote, adjustBody,
  saveStatus, retrySaves, discardFailed,
} from './preview.js';
import { DEFAULT_GEOMETRY, normalizeFrame, normalizeStamp } from './finish.js';

const zeroAdj = () => ({
  exposure_ev: 0, contrast: 0, highlight: 0, shadow: 0, black_point: 0,
  warm: 0, tint: 0, saturation: 0, vibrance: 0, vignette: 0,
});

const TABS = [
  ['recipe', '配方', 'R'],
  ['adjust', '调整', 'A'],
  ['crop', '裁剪', 'C'],
  ['frame', '边框', 'F'],
  ['stamp', '时间戳', 'T'],
  ['export', '导出', 'E'],
];

let view = null;
let resizeObs = null;

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
        setShowOriginal(true);
        return true;
      }
      if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        if (view.tab !== 'crop' || isSliderFocus()) return false;
        nudgeStraighten(view, e.key === 'ArrowUp' ? 0.5 : -0.5);
        return true;
      }
      if (view.tab === 'crop' && (e.key === 'g' || e.key === 'G')) {
        cycleGuide(view);
        return true;
      }
      if (view.tab === 'crop' && (e.key === 'o' || e.key === 'O')) {
        cycleSpiral(view);
        return true;
      }
      if (view.tab === 'crop' && e.key === '[') {
        rotate90(view);
        return true;
      }
      const tab = TABS.find((t) => t[2].toLowerCase() === e.key.toLowerCase());
      if (tab) {
        setTab(tab[0]);
        return true;
      }
      return false;
    },
    up(e) {
      if (e.key === ' ' && view && view.showOriginal) {
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
  };
}

function isSliderFocus() {
  const a = document.activeElement;
  return !!a && ((a.tagName === 'INPUT' && a.type === 'range') || a.classList.contains('ruler'));
}

function setShowOriginal(on) {
  if (!view || view.showOriginal === on) return;
  view.showOriginal = on;
  paintStage(view);
  paintStatus(view);
}

// ---- 标签 -------------------------------------------------------------------

// 进入「裁剪」：画面切到未裁切的整张（stage=uncropped）；离开：回到成片（stage=final）
export function setTab(tab) {
  if (!view || view.tab === tab) return;
  if (tab === 'crop') view.lastTab = view.tab;
  view.tab = tab;
  state.viewerTab = tab === 'crop' ? 'recipe' : tab;
  view.stage = tab === 'crop' ? 'uncropped' : 'final';
  if (view.ed) schedulePreview(view, { full: true });
  renderSide();
  paintStage(view);
  paintStatus(view);
}

function renderTabs() {
  view.sideTabs.replaceChildren(...TABS.map(([id, label, key]) => h('button', {
    type: 'button', role: 'tab', 'aria-selected': String(view.tab === id), title: `${label}（${key}）`,
    onclick: () => setTab(id),
  }, label)));
}

function renderSide() {
  if (!view) return;
  renderTabs();
  view.sideBody.replaceChildren(panelFor(view.tab));
}

function panelFor(tab) {
  if (!view.ed) return h('p', { class: 'dim small' }, view.edError ? (view.edError.message || '无法读取编辑记录。请重试。') : '正在读取…');
  switch (tab) {
    case 'recipe': return recipePanel(view);
    case 'adjust': return adjustPanel(view);
    case 'crop': return cropPanelWrap();
    case 'frame': return framePanel(view);
    case 'stamp': return stampPanel(view);
    case 'export': return exportForm({
      ids: [view.photo.id], label: '这一张', photos: [view.photo],
      beforeSubmit: ((v) => () => flushEdits(v))(view),
    });
    default: return h('p', {}, '');
  }
}

function cropPanelWrap() {
  return h('div', { class: 'pane' },
    h('h2', {}, '裁剪'),
    view.canvas ? cropSection(view) : h('p', { class: 'dim small' }, '正在生成预览…'),
    correctSection(view),
    h('div', { class: 'sticky-foot' }, ...correctActions(view), h('button', { type: 'button', class: 'primary end', onclick: finishCrop }, '完成')),
  );
}

function finishCrop() {
  setTab(view.lastTab && view.lastTab !== 'crop' ? view.lastTab : 'recipe');
}

// ---- 状态行与底栏 ----------------------------------------------------------------

function paintStatus(v) {
  if (!view || v !== view) return;
  const el = view.statusEl;
  const ed = view.ed;
  if (!ed) {
    el.replaceChildren(h('span', { class: 'dim' }, '正在读取…'));
    return;
  }
  const mode = stageMode(view);
  const text = mode === 'orig' ? '原图' : mode === 'uncropped' ? '裁剪' : '成片';
  const right = ed.busy ? '正在生成预览…' : ed.error ? '无法生成预览。请重试。' : '';
  const chips = stageLabels(view).map((l) => h('span', { class: `chip ${l.kind}`, title: l.title || null }, l.text));
  el.replaceChildren(...chips, h('span', { class: 'dim' }, text), h('span', { class: 'right' }, right));
}

function renderPhotoBar() {
  const p = view.photo;
  // 空的子元素不能直接交给 replaceChildren（会变成文字 null）
  view.photoBar.replaceChildren(...[
    h('button', { type: 'button', class: 'ghost', onclick: () => { location.hash = '#/photos'; } }, '‹ 照片'),
    h('strong', {}, p.name),
    h('span', { class: 'dim small' }, `${fmtDate(p.date)} ${fmtClock(p.captured_at)}`),
    pairTag(p),
    p.date_source === 'mtime' ? h('span', { class: 'badge', title: '使用文件修改日期' }, '使用文件修改日期') : null,
    h('span', { class: 'dim small count' }, `${state.photos.findIndex((x) => x.id === p.id) + 1} / ${state.photos.length}`),
  ].filter(Boolean));
}

// 撤销：没有可以撤销的内容时按钮置灰，不弹提示（COPY 3.10）
function undoButton() {
  const btn = h('button', { type: 'button', onclick: () => undoChange(view) }, '撤销');
  view.undoBtn = btn;
  syncUndo(view);
  return btn;
}

// 保存状态（安静显示，不弹提示）：正在保存… / 已保存 / 未保存 · 重试 · 放弃修改（R05）
function paintSaveState(v) {
  if (!v.saveEl) return;
  const st = saveStatus(v.photo.id);
  const el = v.saveEl;
  el.dataset.state = st;
  if (st === 'saving') el.replaceChildren('正在保存…');
  else if (st === 'saved') el.replaceChildren('已保存');
  else if (st === 'failed') {
    el.replaceChildren(
      h('span', { class: 'bad-text' }, '有修改没有保存'),
      h('button', { type: 'button', class: 'btn ghost small', onclick: () => retrySaves(v.photo.id) }, '重试'),
      h('button', { type: 'button', class: 'btn ghost small', onclick: () => discardFailed(v) }, '放弃修改'),
    );
  } else el.replaceChildren();
}

on('save-status', (e) => {
  if (view && e.detail && e.detail.id === view.photo.id) paintSaveState(view);
});

function renderBottom() {
  const pos = state.photos.findIndex((p) => p.id === view.photo.id);
  view.saveEl = h('span', { class: 'save-state small dim', 'aria-live': 'polite' });
  paintSaveState(view);
  view.bottom.replaceChildren(
    undoButton(),
    h('button', { type: 'button', onclick: resetAll, title: '整张回到原图' }, '复原'),
    view.saveEl,
    h('span', { class: 'spacer' }),
    h('button', { type: 'button', 'aria-disabled': pos <= 0 ? 'true' : null, onclick: () => step(-1) }, '上一张'),
    h('button', { type: 'button', 'aria-disabled': pos < 0 || pos >= state.photos.length - 1 ? 'true' : null, onclick: () => step(1) }, '下一张'),
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
  const saved = persist(v, changes);
  const cropSaved = resetCropRemote(v);
  await Promise.all([saved, cropSaved]);
  if (view !== v || v.ed !== ed) return;
  endChange(v);
  schedulePreview(v, { full: true });
  renderSide();
}

function step(d) {
  const list = state.photos;
  const i = list.findIndex((p) => p.id === view.photo.id);
  const next = list[i + d];
  if (next) location.hash = `#/photo/${next.id}`;
}

async function ensurePhotos(id) {
  if (state.photos.some((p) => p.id === id)) return;
  const q = state.galleryDate ? `?date=${state.galleryDate}&limit=5000` : '?limit=5000';
  state.photos = await api(`/api/photos${q}`).catch(() => []);
}

function initialTab() {
  const t = state.viewerTab;
  return TABS.some(([id]) => id === t) && t !== 'crop' ? t : 'recipe';
}

// ---- 打开、关闭 ---------------------------------------------------------------

// isCurrent：路由层给的「我还是当前路由吗」（R11）。每次 await 回来、改页面或装快捷键之前都先问一次，
// 过时的照片响应不能把页面换回旧照片。
export async function renderViewer(root, id, isCurrent = () => true) {
  let photo;
  try {
    photo = await api(`/api/photos/${id}`);
  } catch (e) {
    if (!isCurrent()) return;
    root.replaceChildren(h('div', { class: 'empty' }, h('h2', {}, '找不到这张照片'), h('p', { class: 'dim' }, e.message), h('a', { href: '#/photos' }, '回到照片')));
    return;
  }
  if (!isCurrent()) return;
  await ensurePhotos(id);
  if (!isCurrent()) return;
  teardown();

  const tab = initialTab();
  const stageArea = h('div', { class: 'stage-area' });
  const statusEl = h('div', { class: 'status' });
  const sideTabs = h('div', { class: 'side-tabs', role: 'tablist' });
  const sideBody = h('div', { class: 'side-body', role: 'tabpanel' });
  const photoBar = h('div', { class: 'photobar' });
  const bottom = h('div', { class: 'bottombar' });
  root.replaceChildren(h(
    'section',
    { class: 'viewer' },
    photoBar,
    h('div', { class: 'viewer-body' },
      h('div', { class: 'stagecol' }, stageArea, statusEl),
      h('aside', { class: 'side' }, sideTabs, sideBody)),
    bottom,
  ));

  view = {
    photo, tab, lastTab: 'recipe', stage: tab === 'crop' ? 'uncropped' : 'final', showOriginal: false,
    stageArea, statusEl, sideTabs, sideBody, photoBar, bottom,
    undo: [], pending: null, saveQ: null, ed: null, edPromise: null,
    crop: null, canvas: null, zoom: 100, presetItem: null, presetPortrait: false,
    box: null, pixelsEl: null, rulerEl: null, refreshAngle: null, onRectDrag: null, mode: null, edError: null,
  };
  const v = view;
  v.onChange = () => {
    if (view !== v) return;
    paintStage(v);
    paintStatus(v);
  };
  v.redrawSide = () => {
    if (view === v) renderSide();
  };

  // 两个请求都等完再画（Promise.all 会在第一个失败时就返回，另一个回来后界面不会重画，留下「正在读取」）
  const [cropRes, edRes] = await Promise.allSettled([loadCrop(v), ensureEdit(v)]);
  if (view !== v) return;
  if (cropRes.status === 'rejected') {
    toastError(cropRes.reason, '无法读取裁剪。请重试。');
    v.crop = { preset: 'original', rect: { x: 0, y: 0, w: 1, h: 1 }, angle_deg: 0, active: false, frame: { width: photo.width || 1, height: photo.height || 1 }, rev: 0 };
  }
  if (edRes.status === 'rejected') {
    v.edError = edRes.reason;
    toastError(edRes.reason, '无法读取编辑记录。请重试。');
  }
  // 编辑记录坏了：按原图显示，并说明（COPY 3.9）
  if ((v.crop && v.crop.warning) || (v.ed && v.ed.warning)) toast('无法读取编辑记录，已按原图显示', 'info');

  renderPhotoBar();
  renderBottom();
  renderSide();
  paintStage(v);
  paintStatus(v);
  schedulePreview(v, { full: true });

  resizeObs = new ResizeObserver(() => {
    if (view === v) paintStage(v);
  });
  resizeObs.observe(stageArea);
}

function teardown() {
  if (resizeObs) {
    resizeObs.disconnect();
    resizeObs = null;
  }
  closeMenus();
  if (view) disposeEdit(view);
  view = null;
}

// 当前打开的照片视图（没有打开就是 null）。面板拿到的 view 和这里是同一个对象。
export function currentView() {
  return view;
}

// 离开大图（路由切换时调用）：停掉观察器和定时器，选中的照片不动
export function leaveViewer() {
  teardown();
}

// 相机数据或配方文件换了：照片的画面和能力都要重读
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
  paintStatus(v);
}

// ⌘E 在大图里：直接切到导出标签
export function openViewerTab(tab) {
  if (view) setTab(tab);
}
