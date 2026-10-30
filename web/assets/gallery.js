// 图库：按拍摄日期分组筛选，角标，多选，导出选中的。

import { api } from './api.js';
import { state, on } from './state.js';
import { h, fmtDate, fmtTime } from './dom.js';
import { openExportDialog } from './export.js';
import { cardPresent } from './import.js';
import { showHint } from './onboarding.js';
import { isPrediction, rawOnlyBadgeText } from './look_notes.js';

const ALL = null;
let root = null;
let focusId = null;

export function pairTag(p) {
  if (p.has_raw && p.has_jpg) return h('span', { class: 'badge pair tag' }, 'DNG+JPG');
  if (p.has_jpg) return h('span', { class: 'badge tag gray' }, '只有 JPG');
  // 只有 RAW 的照片没有相机 JPG 可看，缩略图是电脑画的：如实标出来
  const look = p.preview_look || {};
  return h('span', { class: `badge tag ${isPrediction(look) ? 'pred' : 'gray'}`, title: look.label || null }, rawOnlyBadgeText(look));
}

export async function renderGallery(container) {
  root = container;
  root.replaceChildren(
    h('aside', { class: 'dates', id: 'dates' }),
    h(
      'section',
      { class: 'gallery-main' },
      h('div', { class: 'grid-wrap', id: 'grid-wrap' }),
      h('div', { class: 'selbar', id: 'selbar' }),
    ),
  );
  showHint('gallery', '点一张照片就能看大图；左上角的小方框用来多选。');
  await loadAll();
}

on('library-changed', async () => {
  await pruneSelection();
  if (root) loadAll();
});

// 补全另一半后照片的 id 会变：勾选集里留着旧 id 的话，批量导出会拿到不存在的照片，所以先把已经不在图库里的去掉
async function pruneSelection() {
  if (!state.selection.size) return;
  const all = await api('/api/photos?limit=5000').catch(() => null);
  if (!all) return;
  const alive = new Set(all.map((p) => p.id));
  for (const id of [...state.selection]) if (!alive.has(id)) state.selection.delete(id);
}

async function loadAll() {
  await loadDates();
  await loadPhotos();
}

async function loadDates() {
  state.dates = await api('/api/dates').catch(() => []);
  const total = state.dates.reduce((n, d) => n + d.count, 0);
  const aside = document.getElementById('dates');
  if (!aside) return;
  const row = (label, count, date) =>
    h(
      'button',
      {
        type: 'button',
        class: state.galleryDate === date ? 'on' : '',
        onclick: () => {
          state.galleryDate = date;
          loadAll();
        },
      },
      h('span', {}, label),
      h('span', { class: 'dim' }, String(count)),
    );
  aside.replaceChildren(
    row('全部', total, ALL),
    ...state.dates.map((d) => row(fmtDate(d.date), d.count, d.date)),
  );
}

async function loadPhotos() {
  const q = state.galleryDate ? `?date=${state.galleryDate}&limit=5000` : '?limit=5000';
  state.photos = await api(`/api/photos${q}`).catch(() => []);
  renderGrid();
}

function renderGrid() {
  const wrap = document.getElementById('grid-wrap');
  if (!wrap) return;
  if (!state.photos.length) {
    wrap.replaceChildren(emptyState());
    renderSelBar();
    return;
  }
  const header = h(
    'div',
    { class: 'day-head' },
    h('h2', {}, state.galleryDate ? fmtDate(state.galleryDate) : '全部照片'),
    h('span', { class: 'dim small' }, `${state.photos.length} 张。点照片看大图，左上角勾选可多选。`),
  );
  const grid = h(
    'div',
    { class: 'grid' },
    state.photos.map((p) => thumb(p)),
  );
  wrap.replaceChildren(header, grid);
  renderSelBar();
}

function thumb(p) {
  const selected = state.selection.has(p.id);
  const box = h('input', {
    type: 'checkbox',
    class: 'check',
    checked: selected,
    'aria-label': `选中 ${p.name}`,
    onclick: (e) => e.stopPropagation(),
    onchange: (e) => {
      if (e.target.checked) state.selection.add(p.id);
      else state.selection.delete(p.id);
      el.classList.toggle('selected', e.target.checked);
      renderSelBar();
    },
  });
  const el = h(
    'div',
    {
      class: `thumb${selected ? ' selected' : ''}${focusId === p.id ? ' focus' : ''}`,
      tabindex: 0,
      role: 'button',
      title: `${p.name} · ${fmtTime(p.captured_at)}${p.date_source === 'mtime' ? '（日期是按文件时间估计的）' : ''}${
        p.preview_look && p.preview_look.label ? ` · 缩略图：${p.preview_look.label}` : ''
      }`,
      onclick: () => {
        focusId = p.id;
        location.hash = `#/photo/${p.id}`;
      },
      onkeydown: (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          location.hash = `#/photo/${p.id}`;
        }
      },
    },
    h('img', { src: p.thumb_url, alt: p.name, loading: 'lazy', decoding: 'async' }),
    box,
    pairTag(p),
  );
  return el;
}

function emptyState() {
  if (cardPresent()) {
    return h(
      'div',
      { class: 'empty' },
      h('h2', {}, '这张卡上还没有可导入的新照片'),
      h('p', { class: 'dim' }, '如果刚插卡，请稍等一下；卡上的照片都导入过了也会显示在这里。'),
    );
  }
  return h(
    'div',
    { class: 'empty' },
    h('h2', {}, '还没有照片'),
    h('p', {}, '插上相机的存储卡，照片会自动读进来，按拍摄日期排好。'),
    h('p', { class: 'dim small' }, '卡上的文件不会被改动。'),
    h(
      'details',
      {},
      h('summary', {}, '我怎么插卡？'),
      h(
        'ol',
        {},
        h('li', {}, '取出相机的存储卡（先关机）。'),
        h('li', {}, '插进电脑的读卡器。'),
        h('li', {}, '等它自动开始，不用点任何按钮。'),
      ),
    ),
  );
}

function renderSelBar() {
  const bar = document.getElementById('selbar');
  if (!bar) return;
  const n = state.selection.size;
  if (!n) {
    bar.replaceChildren();
    return;
  }
  bar.replaceChildren(
    h('span', {}, `已选 ${n} 张`),
    h(
      'button',
      {
        class: 'btn primary',
        type: 'button',
        onclick: () => openExportDialog({ ids: [...state.selection], label: `选中的 ${n} 张` }),
      },
      '导出选中的',
    ),
    h('button', { class: 'btn ghost', type: 'button', onclick: clearSelection }, '取消选择'),
  );
}

export function clearSelection() {
  state.selection.clear();
  renderGrid();
}

// Enter：打开最近点过的那张（没有就打开第一张）
export function openFocused() {
  const id = focusId || (state.photos[0] && state.photos[0].id);
  if (id) location.hash = `#/photo/${id}`;
}
