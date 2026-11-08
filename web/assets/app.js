// 入口：启动、路由（#/photos、#/photo/<id>、#/recipes、#/camera）、全局快捷键、定时刷新。

import { api } from './api.js';
import { state, emit, on } from './state.js';
import { toast, modalOpen, closeTopModal, isTyping, toastError, modKey } from './dom.js';
import { refreshCards, tickImport, loadLatestJob, renderBanner, renderCardStatus, bindCardChip } from './import.js';
import { renderGallery, openFocused, galleryKeys, exportSelection, updateGalleryExport, galleryExportLabel } from './gallery.js';
import { renderViewer, viewerKeys, leaveViewer, exportCurrentPhoto, refreshViewerPhoto, currentView } from './viewer.js';
import { copyEdits, pasteEdits } from './paste_edits.js';
import { pasteToSelection } from './paste_batch.js';
import { openExportDialog } from './export.js';
import { showHelp, showSettings } from './onboarding.js';
import { loadCamdata } from './camdata.js';
import { renderLutPage } from './lut.js';
import { renderCamera } from './camera_page.js';
import { createRouter } from './router.js';
import { every } from './poll.js';

const router = createRouter({
  before: (r) => setTabs(r.name === 'viewer' ? 'photos' : r.name, r.name),
  pages: {
    photos: { enter: async (r, isCurrent) => { await renderGallery(viewRoot(), isCurrent); return null; } },
    viewer: {
      enter: async (r, isCurrent) => { await renderViewer(viewRoot(), r.id, isCurrent); return isCurrent() ? viewerKeys() : null; },
      leave: () => leaveViewer(),
    },
    recipes: { enter: async () => { renderLutPage(viewRoot()); return null; } },
    camera: { enter: async (r, isCurrent) => { renderCamera(viewRoot(), isCurrent); return null; } },
  },
});
const route = () => router.route();
const viewRoot = () => document.getElementById('view');

// 顶栏：当前页面的标签带 aria-current；进入大图时顶栏换成编辑模式（viewer.js 往 #tb-edit 里放东西）
function setTabs(name, page) {
  for (const a of document.querySelectorAll('#tabs a')) {
    const on = a.dataset.tab === name;
    a.classList.toggle('on', on);
    if (on) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  document.getElementById('titlebar')?.setAttribute('data-mode', page === 'viewer' ? 'viewer' : 'library');
  updateGalleryExport(page === 'photos');
}

// ⌘E：大图里导出这一张；图库里导出选中的；没选的话，在按日期筛选时导出这一天，否则说明怎么选
function exportCurrent() {
  if (router.current() === 'viewer') {
    exportCurrentPhoto();
    return;
  }
  if (router.current() === 'photos' && exportSelection()) return;
  if (router.current() === 'photos' && state.galleryDate) {
    const count = state.dates.find((d) => d.date === state.galleryDate)?.count ?? state.photos.length;
    openExportDialog({ ids: [], date: state.galleryDate, dayLabel: galleryExportLabel(state.galleryDate, count, 0).replace(/^导出 /, ''), photos: state.photos });
    return;
  }
  toast(`先在图库里选择要导出的照片：点照片左上角的圈或按住 ${modKey()} 点击加选，按住 ⇧ 连续选择，${modKey()}A 全选。`, 'info');
}

function copyPasteKey(paste) {
  if (router.current() === 'viewer') (paste ? pasteEdits : copyEdits)(currentView());
  else if (router.current() === 'photos' && paste) pasteToSelection();
}

function bindGlobalKeys() {
  document.addEventListener('keydown', (e) => {
    const mod = e.metaKey || e.ctrlKey;
    // 撤销：⌘Z；重做：⇧⌘Z / Ctrl+Y（视图里自己判断能不能做）
    if (mod && (e.key === 'z' || e.key === 'Z' || e.key === 'y' || e.key === 'Y') && !isTyping(e.target)) {
      const keys = router.keys();
      const redo = e.key === 'y' || e.key === 'Y' || e.shiftKey;
      const fn = keys && (redo ? keys.redo : keys.undo);
      if (fn && fn()) e.preventDefault();
      return;
    }
    // 复制 / 粘贴调整：⌘⇧C / ⌘⇧V（输入框里留给文字复制粘贴）
    if (mod && e.shiftKey && 'cCvV'.includes(e.key) && !isTyping(e.target)) {
      e.preventDefault();
      if (!modalOpen()) copyPasteKey(e.key.toLowerCase() === 'v');
      return;
    }
    if (mod && (e.key === 'e' || e.key === 'E')) {
      e.preventDefault();
      if (!modalOpen()) exportCurrent();
      return;
    }
    if (mod && e.key === ',') {
      e.preventDefault();
      if (!modalOpen()) showSettings();
      return;
    }
    if (e.key === 'Escape') {
      if (modalOpen()) closeTopModal();
      else if (router.current() === 'photos' && galleryKeys(e)) e.preventDefault();
      else if (router.current() === 'viewer' && !isTyping(e.target)) location.hash = '#/photos';
      return;
    }
    if (isTyping(e.target)) return;
    if (modalOpen()) return;
    if (router.current() === 'photos' && galleryKeys(e)) {
      e.preventDefault();
      return;
    }
    if (mod) return;
    if (e.key === '?') {
      e.preventDefault();
      showHelp();
      return;
    }
    const keys = router.keys();
    if (keys && keys.down(e)) {
      e.preventDefault();
      return;
    }
    if (e.key === 'Enter' && router.current() === 'photos' && e.target === document.body) {
      openFocused();
    }
  });
  document.addEventListener('keyup', (e) => {
    const keys = router.keys();
    if (keys && keys.up) keys.up(e);
  });
}

async function boot() {
  const [settings, prefs] = await Promise.allSettled([api('/api/settings'), api('/api/prefs')]);
  if (settings.status === 'fulfilled') state.settings = settings.value;
  else toastError(settings.reason, '无法读取设置。请刷新页面。');
  if (prefs.status === 'fulfilled') state.prefs = prefs.value;
  bindGlobalKeys();
  bindCardChip();
  document.getElementById('btn-export-top').addEventListener('click', exportCurrent);
  document.getElementById('btn-settings').addEventListener('click', () => showSettings());
  document.getElementById('btn-help').addEventListener('click', showHelp);
  window.addEventListener('hashchange', route);
  on('open-settings', () => showSettings());
  on('camdata-changed', () => {
    emit('library-changed'); // 图库重新取照片列表（没有 JPG 的缩略图地址变了）
    refreshViewerPhoto();
  });
  // 页面先出来，存储卡和相机数据的状态随后补上（不再等它们才画图库）
  const first = route();
  await loadLatestJob();
  await refreshCards();
  renderBanner();
  renderCardStatus();
  loadCamdata();
  await first;

  // 定时刷新：页面在后台时停，回来时立刻补一次；上一个请求没回来不发下一个（U15）
  every(15000, () => loadCamdata());
  every(3000, () => refreshCards());
  every(800, () => tickImport()); // 只在导入中才发请求；横幅和顶栏标签由 tickImport 刷新
}

boot();
