// 入口：启动、路由（#/photos、#/photo/<id>、#/recipes、#/camera）、全局快捷键、定时刷新。

import { api } from './api.js';
import { state, emit, on } from './state.js';
import { h, toast, modalOpen, closeTopModal, isTyping, toastError } from './dom.js';
import { refreshCards, tickImport, loadLatestJob, renderBanner, renderCardStatus, isImporting, cardPresent } from './import.js';
import { renderGallery, openFocused } from './gallery.js';
import { renderViewer, viewerKeys, leaveViewer, openViewerTab, refreshViewerPhoto } from './viewer.js';
import { openExportDialog } from './export.js';
import { maybeShowOnboarding, showHelp, showHelpMenu, showSettings, clearHints } from './onboarding.js';
import { loadCamdata } from './camdata.js';
import { renderLutPage } from './lut.js';
import { createRouter } from './router.js';

const router = createRouter({
  before: (r) => {
    clearHints();
    setTabs(r.name === 'viewer' ? 'photos' : r.name);
  },
  pages: {
    photos: { enter: async (r, isCurrent) => { await renderGallery(viewRoot(), isCurrent); return null; } },
    viewer: {
      enter: async (r, isCurrent) => { await renderViewer(viewRoot(), r.id, isCurrent); return isCurrent() ? viewerKeys() : null; },
      leave: () => leaveViewer(),
    },
    recipes: { enter: async () => { renderLutPage(viewRoot()); return null; } },
    camera: { enter: async () => { renderCamera(viewRoot()); return null; } },
  },
});
const route = () => router.route();
const viewRoot = () => document.getElementById('view');

function setTabs(name) {
  for (const a of document.querySelectorAll('#tabs a')) {
    a.classList.toggle('on', a.dataset.tab === name);
  }
}

function renderCamera(view) {
  const card = cardPresent() ? '已插入' : '未插入';
  view.replaceChildren(
    h(
      'section',
      { class: 'empty', style: { maxWidth: '520px' } },
      h('h2', {}, '相机'),
      h('p', {}, `存储卡：${card}`),
      h('p', { class: 'small dim' }, '暂不支持写入存储卡'),
    ),
  );
}

function exportCurrent() {
  if (router.current() === 'viewer') {
    openViewerTab('export');
    return;
  }
  const ids = [...state.selection];
  if (!ids.length) {
    toast('先选择照片', 'info');
    return;
  }
  openExportDialog({ ids, label: `选中的 ${ids.length} 张` });
}

function bindGlobalKeys() {
  document.addEventListener('keydown', (e) => {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && (e.key === 'z' || e.key === 'Z') && !isTyping(e.target)) {
      const keys = router.keys();
      if (keys && keys.undo && keys.undo()) e.preventDefault();
      return;
    }
    if (mod && (e.key === 'e' || e.key === 'E')) {
      e.preventDefault();
      exportCurrent();
      return;
    }
    if (mod && e.key === ',') {
      e.preventDefault();
      showSettings();
      return;
    }
    if (e.key === 'Escape') {
      if (modalOpen()) closeTopModal();
      else if (router.current() === 'viewer') location.hash = '#/photos';
      return;
    }
    if (isTyping(e.target) || mod) return;
    if (e.key === '?') {
      e.preventDefault();
      showHelp();
      return;
    }
    if (modalOpen()) return;
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
  try {
    state.settings = await api('/api/settings');
  } catch (e) {
    toastError(e, '无法完成操作。请重试。');
  }
  try {
    state.prefs = await api('/api/prefs');
  } catch (e) {
    toastError(e, '无法完成操作。请重试。');
  }
  await loadLatestJob();
  await refreshCards();
  await loadCamdata();
  setInterval(() => loadCamdata(), 15000);
  bindGlobalKeys();
  document.getElementById('btn-export-top').addEventListener('click', exportCurrent);
  document.getElementById('btn-settings').addEventListener('click', showSettings);
  document.getElementById('btn-help').addEventListener('click', showHelpMenu);
  window.addEventListener('hashchange', route);
  on('open-settings', () => showSettings());
  on('camdata-changed', () => {
    emit('library-changed'); // 图库重新取照片列表（没有 JPG 的缩略图地址变了）
    refreshViewerPhoto();
  });
  await route();
  renderBanner();
  renderCardStatus();
  maybeShowOnboarding();

  setInterval(() => refreshCards(), 3000);
  setInterval(() => tickImport(), 800);
  // 状态栏和横幅每秒看一次（导入中进度会变）
  setInterval(() => {
    if (!isImporting()) return;
    renderBanner();
    renderCardStatus();
  }, 1000);
}

boot();
