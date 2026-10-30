// 大图：左右键切换，右侧四个标签（裁切、配方、调整、导出）。配方和调整的预览是电脑预测（见 preview.js）。

import { api } from './api.js';
import { state } from './state.js';
import { h, toast, fmtTime, fmtDate } from './dom.js';
import { pairTag } from './gallery.js';
import { applyCrop, attachDrag, attachGuides, cropPanel, cycleGuide, cycleSpiral, fitStage, loadCrop, resetCrop, undoCrop, saveCrop } from './crop.js';
import { exportForm } from './export.js';
import { showHint } from './onboarding.js';
import { recipePanel } from './recipes.js';
import { adjustPanel } from './adjust.js';
import { EDIT_TABS, defaultNote, disposeEdit, ensureEdit, reloadEdit, renderNote, schedulePreview, syncImage } from './preview.js';

const TABS = [
  ['crop', '裁切', 'C'],
  ['recipes', '配方', 'R'],
  ['adjust', '调整', 'A'],
  ['export', '导出', 'E'],
];

let view = null;
let resizeObs = null;
let nudgeTimer = null;

export function viewerKeys() {
  if (!view) return null;
  return {
    down(e) {
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        if (isRangeFocused()) return false;
        step(e.key === 'ArrowLeft' ? -1 : 1);
        return true;
      }
      if (e.key === ' ' && !e.repeat) {
        setShowOriginal(true);
        return true;
      }
      if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        if (view.tab !== 'crop' || isRangeFocused()) return false;
        nudge(e.key === 'ArrowUp' ? 0.5 : -0.5);
        return true;
      }
      // 裁切里的构图辅助线：G 换线，O 换螺旋方向（和 C/R/A/E 不冲突）
      if (view.tab === 'crop' && (e.key === 'g' || e.key === 'G')) {
        cycleGuide(view);
        return true;
      }
      if (view.tab === 'crop' && (e.key === 'o' || e.key === 'O')) {
        cycleSpiral(view);
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
      undoCrop(view);
      return true;
    },
  };
}

function isRangeFocused() {
  const a = document.activeElement;
  return !!a && a.tagName === 'INPUT' && a.type === 'range';
}

function setShowOriginal(on) {
  if (!view || view.showOriginal === on) return;
  view.showOriginal = on;
  applyCrop(view);
  syncImage(view);
  renderNote(view);
}

// 切换标签：配方、调整需要先读这张照片的编辑状态，读完再画预览
function setTab(tab) {
  if (!view) return;
  view.tab = tab;
  state.viewerTab = tab;
  const v = view;
  if (EDIT_TABS.includes(tab)) {
    ensureEdit(v).then(
      () => {
        if (view !== v || v.tab !== tab) return;
        v.ed.redraw = () => {
          if (view === v) renderSide();
        };
        schedulePreview(v, { final: true });
        renderSide();
      },
      (e) => {
        if (view !== v) return;
        v.edError = e;
        renderSide();
      },
    );
  }
  renderSide();
  applyCrop(view);
  syncImage(view);
  renderNote(view);
}

// 方向键上下：拉直 0.5 度，停一下再保存
function nudge(delta) {
  const next = Math.max(-10, Math.min(10, Math.round((view.angle + delta) * 10) / 10));
  view.angle = next;
  applyCrop(view);
  clearTimeout(nudgeTimer);
  nudgeTimer = setTimeout(() => {
    if (view) saveCrop(view, { preset: view.crop.preset, rect: null, angle_deg: view.angle });
  }, 350);
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

export async function renderViewer(root, id) {
  let photo;
  try {
    photo = await api(`/api/photos/${id}`);
  } catch (e) {
    root.replaceChildren(h('div', { class: 'empty' }, h('h2', {}, '找不到这张照片'), h('p', { class: 'dim' }, e.message), h('a', { href: '#/photos' }, '回到图库')));
    return;
  }
  await ensurePhotos(id);
  teardown();

  const tab = state.viewerTab || 'crop';
  const stage = h('div', { class: 'stage', dataset: { active: 'false' } });
  const img = h('img', {
    class: 'photo',
    src: photo.preview_url,
    dataset: { src: photo.preview_url },
    alt: photo.name,
    onerror: () => toast('这张照片读不出图像', 'error'),
  });
  const box = h('div', { class: 'crop-box', title: '拖动可以挪位置' });
  stage.append(img, box);
  const note = h('div', { class: 'stage-note dim small' }, defaultNote(photo));
  const stageWrap = h('div', { class: 'stage-wrap' }, stage, note);
  const side = h('aside', { class: 'side' });
  const sideBody = h('div', { class: 'side-body', role: 'tabpanel' });
  const tabBar = h(
    'div',
    { class: 'side-tabs', role: 'tablist' },
    TABS.map(([id2, label, key]) =>
      h(
        'button',
        {
          type: 'button',
          role: 'tab',
          dataset: { tab: id2 },
          'aria-selected': String(tab === id2),
          title: `${label}（${key}）`,
          onclick: () => setTab(id2),
        },
        label,
      ),
    ),
  );
  side.append(tabBar, sideBody);

  const pos = state.photos.findIndex((p) => p.id === id);
  const caption = h(
    'div',
    { class: 'viewer-top' },
    h('a', { href: '#/photos', class: 'btn ghost', title: '回到图库（Esc）' }, '‹ 返回图库'),
    h('strong', {}, photo.name),
    h('span', { class: 'dim' }, `${fmtTime(photo.captured_at)} · ${fmtDate(photo.date)}`),
    pairTag(photo),
    photo.date_source === 'mtime' ? h('span', { class: 'badge', title: '照片里没有拍摄时间，日期是按文件的修改时间估计的' }, '日期是估计的') : null,
    h('span', { class: 'dim small', style: { marginLeft: 'auto' } }, pos >= 0 ? `${pos + 1} / ${state.photos.length}` : ''),
  );

  const bottom = h(
    'div',
    { class: 'viewer-bottom' },
    h('button', { class: 'btn', type: 'button', title: '撤销上一步（⌘Z）', onclick: () => undoCrop(view) }, '↶ 撤销'),
    h('button', { class: 'btn', type: 'button', title: '清掉这张的裁切，原图不变', onclick: () => resetCrop(view) }, '还原原图'),
    h('span', { class: 'spacer' }),
    h('button', { class: 'btn', type: 'button', disabled: pos <= 0, onclick: () => step(-1) }, '‹ 上一张'),
    h('button', { class: 'btn', type: 'button', disabled: pos < 0 || pos >= state.photos.length - 1, onclick: () => step(1) }, '下一张 ›'),
  );

  const body = h('div', { class: 'viewer-body' }, stageWrap, side);
  root.replaceChildren(h('section', { class: 'viewer' }, caption, body, bottom));

  view = {
    photo,
    stage,
    stageWrap,
    img,
    box,
    noteEl: note,
    tab,
    sideBody,
    tabBar,
    history: [],
    angle: 0,
    zoom: 100,
    showOriginal: false,
    panelEl: null,
    ed: null,
    edPromise: null,
    edError: null,
  };
  attachDrag(view);
  attachGuides(view);

  try {
    await loadCrop(view);
  } catch (e) {
    toast(e.message, 'error');
    view.crop = { preset: 'original', rect: [0, 0, 1, 1], angle_deg: 0, active: false, frame: { width: photo.width || 1, height: photo.height || 1 }, rev: 0, pixels: null };
    view.rect = { x: 0, y: 0, w: 1, h: 1 };
  }
  fitStage(view);
  applyCrop(view);
  setTab(tab);

  resizeObs = new ResizeObserver(() => {
    if (view) {
      fitStage(view);
      applyCrop(view);
    }
  });
  resizeObs.observe(stageWrap);
  showHint('viewer', '左右键换照片；C 裁切、R 配方、A 调整、E 导出。');
}

function renderSide() {
  if (!view) return;
  for (const btn of view.tabBar.querySelectorAll('button')) {
    btn.setAttribute('aria-selected', String(btn.dataset.tab === view.tab));
  }
  view.sideBody.replaceChildren();
  if (view.tab === 'crop') {
    view.sideBody.append(cropPanel(view));
  } else if (view.tab === 'recipes') {
    view.sideBody.append(editBody(recipePanel));
  } else if (view.tab === 'adjust') {
    view.sideBody.append(editBody(adjustPanel));
  } else if (view.tab === 'export') {
    view.sideBody.append(exportForm({ ids: [view.photo.id], label: '这一张', photos: [view.photo] }));
  }
}

function editBody(panel) {
  if (view.edError) return h('p', { class: 'result bad small' }, view.edError.message);
  if (!view.ed) return h('p', { class: 'dim small' }, '正在读取这张照片的配方…');
  return panel(view);
}

function teardown() {
  if (resizeObs) {
    resizeObs.disconnect();
    resizeObs = null;
  }
  clearTimeout(nudgeTimer);
  if (view) disposeEdit(view);
  view = null;
}

// 离开大图（路由切换时调用）：停掉观察器和定时器，选中的照片不动
export function leaveViewer() {
  teardown();
}

// 相机数据或标定换了：这张照片的图片地址（没有 JPG 的带新的 ?v=）和编辑状态（能不能出图）都要重新读
export async function refreshViewerPhoto() {
  const v = view;
  if (!v) return;
  try {
    const photo = await api(`/api/photos/${v.photo.id}`);
    if (view !== v) return;
    v.photo = photo;
    syncImage(v);
    renderNote(v);   // 相机数据变了，只有 RAW 的照片的画面说明（预测/中性）跟着变
  } catch {
    // 照片读不到不影响其它刷新
  }
  if (view === v && v.ed) await reloadEdit(v);
}

// ⌘E 在大图里：直接切到导出标签
export function openViewerTab(tab) {
  if (view) setTab(tab);
}
