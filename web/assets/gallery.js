// 图库：左边按拍摄日期筛选，右边按日期分组的网格；多选（⇧ 连选、⌘ 点选、⌘A 全选、方向键），导出选中的。
// 照片列表分页读完（photos.js），第一页到了就先画出来，后面的接着往下加，不再截在 5000 张（U12）。
// 网格是一个 listbox，每张照片是 option（aria-selected），不再在 role=button 里套复选框（U16）。

import { api } from './api.js';
import { state, on } from './state.js';
import { h, icon, fmtDate, fmtTime, modKey, toast } from './dom.js';
import { openExportDialog } from './export.js';
import { pasteSelectionButton } from './paste_batch.js';
import { cardPresent, importNow } from './import.js';
import { isPrediction, rawOnlyBadgeText } from './look_notes.js';
import { fetchAllPhotos } from './photos.js';
import { delayed } from './busy.js';
import {
  expandSelectionIds, foldPhotos, halfframeBadge, isPairSecond, isPhotoSelected,
  missingPartnerIds, pairCount, pairMemberIds, patchDayGrid, selectionNoun, selectionStats, togglePairSelection,
} from './halfframe.js';

const ALL = null;
const SIZE_KEY = 'gxr-studio.thumb-size';
const FULL_KEY = 'gxr-studio.thumb-full';
const SIZES = { min: 120, max: 320, dflt: 196 };
let root = null;
let focusId = null;
let anchorId = null;
let loadSeq = 0;
let selectionRevision = 0;
let undoFilterToast = null;
let libraryPairs = 0;

export function pairTag(p) {
  if (p.has_raw && p.has_jpg) return h('span', { class: 'badge pair tag' }, 'RAW+JPG');
  if (p.has_jpg) return h('span', { class: 'badge tag gray' }, '只有 JPG');
  // 只有 RAW 的照片没有相机 JPG 可看，缩略图是电脑画的：如实标出来
  const look = p.preview_look || {};
  return h('span', { class: `badge tag ${isPrediction(look) ? 'pred' : 'gray'}`, title: look.label || null }, rawOnlyBadgeText(look));
}

// 选择逻辑是纯函数，方便单独测（tests/test_busy.py）
// order：当前网格里照片 id 的顺序；返回新的选择集合
export function rangeSelect(order, anchor, target, base = new Set()) {
  const a = order.indexOf(anchor);
  const b = order.indexOf(target);
  const out = new Set(base);
  if (b < 0) return out;
  if (a < 0) {
    out.add(target);
    return out;
  }
  const [lo, hi] = a < b ? [a, b] : [b, a];
  for (let i = lo; i <= hi; i++) out.add(order[i]);
  return out;
}

export function toggleSelect(selection, id) {
  const out = new Set(selection);
  if (out.has(id)) out.delete(id);
  else out.add(id);
  return out;
}

function storedSize() {
  try {
    const n = Number(localStorage.getItem(SIZE_KEY));
    return n >= SIZES.min && n <= SIZES.max ? n : SIZES.dflt;
  } catch {
    return SIZES.dflt;
  }
}

function storedFull() {
  try { return localStorage.getItem(FULL_KEY) === 'true'; }
  catch { return false; }
}

// isCurrent：路由层给的「图库还是当前页面吗」（R11）。过时的列表不写进 state.photos，也不改页面。
let galleryCurrent = () => true;
export async function renderGallery(container, isCurrent = () => true) {
  galleryCurrent = isCurrent;
  root = container;
  const grid = h('div', {
    class: `grid${storedFull() ? ' show-full' : ''}`,
    id: 'grid-wrap',
    role: 'listbox',
    'aria-multiselectable': 'true',
    'aria-label': '照片',
    onkeydown: onGridKey,
  });
  grid.style.setProperty('--cell', `${storedSize()}px`);
  root.replaceChildren(
    h('div', { class: 'library' },
      h('aside', { class: 'sidebar', id: 'dates', 'aria-label': '筛选' }),
      h('section', { class: 'libmain' },
        h('div', { class: 'libbar', id: 'libbar' }),
        grid,
        h('div', { class: 'selbar', id: 'selbar', hidden: true }))),
  );
  renderLibbar();
  await loadAll();
}

function visiblePhotos() {
  return foldPhotos(state.photos, {
    partners: state.partners,
    halfframeOnly: state.galleryFilter === 'halfframe',
    activeDate: state.galleryDate,
  });
}

function visibleSelectionSet() {
  const kept = new Set();
  for (const p of visiblePhotos()) {
    if (!isPhotoSelected(state.selection, p)) continue;
    for (const id of pairMemberIds(p)) kept.add(id);
  }
  return kept;
}

function findKnownPhoto(id) {
  return state.photos.find((p) => p.id === id) || state.partners.get(id) || null;
}

function currentSelectionStats() {
  return selectionStats(visibleSelectionSet(), state.photos, state.partners);
}

// 后台刷新（导入完成、重建索引后）：网格已经有照片时不放骨架，读完整列表再一次换上；列表没变就什么都不动，缩略图不闪
on('library-changed', async () => {
  await pruneSelection();
  if (root && galleryCurrent()) loadAll({ quiet: true });
});

// 补全另一半后照片的 id 会变：勾选集里留着旧 id 的话，批量导出会拿到不存在的照片，所以先把已经不在图库里的去掉
async function pruneSelection() {
  if (!state.selection.size) return;
  const all = await fetchAllPhotos(ALL).catch(() => null);
  if (!all) return;
  const alive = new Set(all.map((p) => p.id));
  for (const id of [...state.selection]) if (!alive.has(id)) state.selection.delete(id);
}

async function loadAll(opts = {}) {
  await Promise.all([loadDates(), loadPhotos(opts)]);
}

function sideItem(label, count, date, numeric = false) {
  const pressed = state.galleryDate === date && !state.galleryFilter;
  const btn = h(
    'button',
    {
      type: 'button',
      class: 'it',
      'aria-pressed': String(pressed),
      onclick: () => {
        if (state.galleryDate === date && !state.galleryFilter) return;
        if (state.galleryDate === date && state.galleryFilter) {
          setGalleryFilter(null);
          return;
        }
        changeDate(date);
      },
    },
    h('span', { class: numeric ? 'num' : '' }, label),
    h('span', { class: 'n' }, String(count)),
  );
  return btn;
}

function setGalleryFilter(filter, restore = null) {
  if (!restore && state.galleryFilter === filter) return;
  if (filter === 'halfframe' && state.galleryDate !== ALL) {
    const prevFilter = state.galleryFilter;
    state.galleryFilter = 'halfframe';
    changeDate(ALL, null, { keepFilter: true, prevFilter });
    return;
  }
  const saved = { filter: state.galleryFilter, ids: [...state.selection], anchor: anchorId, focus: focusId };
  state.galleryFilter = filter;
  focusId = null;
  if (restore) {
    anchorId = restore.anchor;
    const expanded = expandSelectionIds(restore.ids, state.photos, state.partners);
    setSelection(new Set([...expanded].filter((id) => state.photos.some((p) => p.id === id) || state.partners.has(id))));
  } else if (state.selection.size) {
    const kept = visibleSelectionSet();
    const dropped = saved.ids.filter((id) => !kept.has(id));
    if (dropped.length) {
      const droppedStats = selectionStats(dropped, state.photos, state.partners);
      if (anchorId && !kept.has(anchorId)) anchorId = null;
      setSelection(kept);
      undoFilterToast?.();
      undoFilterToast = toast(`切换筛选，已清除 ${selectionNoun(droppedStats)}选择`, 'info', {
        timeout: 0,
        action: { label: '恢复选择', run: () => setGalleryFilter(saved.filter, saved) },
      });
    }
  }
  renderDates();
  renderLibbar();
  renderGrid();
  updateCount();
  if (restore) {
    const vis = visiblePhotos();
    if (vis.length) moveFocus(vis.some((p) => p.id === restore.focus) ? restore.focus : vis[0].id);
  }
}

async function changeDate(date, restore = null, { keepFilter = false, prevFilter = undefined } = {}) {
  const saved = {
    date: state.galleryDate,
    filter: prevFilter !== undefined ? prevFilter : state.galleryFilter,
    ids: [...state.selection],
    anchor: anchorId,
    focus: focusId,
  };
  const savedStats = selectionStats(saved.ids, state.photos, state.partners);
  state.galleryDate = date;
  if (restore) state.galleryFilter = restore.filter ?? null;
  else if (!keepFilter) state.galleryFilter = null;
  focusId = null;
  state.photos = [];
  state.photosComplete = false;
  clearSelection();
  const revision = selectionRevision;
  for (const b of document.querySelectorAll('#dates .it')) b.setAttribute('aria-pressed', 'false');
  if (saved.ids.length) {
    undoFilterToast?.();
    const droppedText = savedStats.items ? selectionNoun(savedStats) : `${saved.ids.length} 张`;
    undoFilterToast = toast(`切换日期，已清除 ${droppedText}选择`, 'info', {
      timeout: 0,
      action: { label: '恢复选择', run: () => changeDate(saved.date, saved) },
    });
  }
  await loadAll();
  if (!restore || state.galleryDate !== date || !galleryCurrent() || selectionRevision !== revision) return;
  if (!state.photosComplete) {
    toast('无法恢复选择，请重试。', 'error', { timeout: 0, action: { label: '恢复选择', run: () => changeDate(date, restore) } });
    return;
  }
  const alive = new Set(state.photos.map((p) => p.id));
  anchorId = restore.anchor;
  setSelection(new Set(restore.ids.filter((id) => alive.has(id))));
  const vis = visiblePhotos();
  moveFocus(alive.has(restore.focus) ? restore.focus : vis[0]?.id);
}

function renderDates() {
  const total = (state.dates || []).reduce((n, d) => n + d.count, 0);
  const aside = document.getElementById('dates');
  if (!aside) return;
  const hfNodes = libraryPairs > 0
    ? [
        h('div', { class: 'grp' }, '筛选'),
        h(
          'button',
          {
            type: 'button',
            class: 'it',
            'aria-pressed': String(state.galleryFilter === 'halfframe'),
            onclick: () => setGalleryFilter('halfframe'),
          },
          h('span', {}, '半格'),
          h('span', { class: 'n' }, `${libraryPairs} 对`),
        ),
      ]
    : [];
  aside.replaceChildren(
    h('div', { class: 'grp' }, '图库'),
    sideItem('全部照片', total, ALL),
    (state.dates || []).length ? h('div', { class: 'grp' }, '日期') : null,
    ...(state.dates || []).map((d) => sideItem(d.date ? fmtDate(d.date) : '未知日期', d.count, d.date, true)),
    ...hfNodes,
  );
  updateGalleryExport();
}

async function loadDates() {
  const dates = await api('/api/dates').catch(() => null);
  if (!galleryCurrent()) return;
  if (dates) state.dates = dates;
  renderDates();
}

function syncLibraryPairs() {
  if (state.galleryDate !== ALL) return;
  const next = pairCount(state.photos, { partners: state.partners });
  if (next === libraryPairs) return;
  libraryPairs = next;
  if (libraryPairs === 0 && state.galleryFilter === 'halfframe') state.galleryFilter = null;
  renderDates();
}

function libCountText() {
  if (!state.photos || !state.photos.length) return '';
  const pairs = pairCount(state.photos, { partners: state.partners, activeDate: state.galleryDate });
  if (state.galleryFilter === 'halfframe') return `· ${pairs} 对`;
  return pairs > 0 ? `${state.photos.length} 张 · ${pairs} 对半格` : `${state.photos.length} 张`;
}

function renderLibbar() {
  const bar = document.getElementById('libbar');
  if (!bar) return;
  const title = state.galleryFilter === 'halfframe'
    ? '半格'
    : (state.galleryDate ? fmtDate(state.galleryDate) : '全部照片');
  const count = h('span', { class: 'faint num', id: 'lib-count' }, libCountText());
  const size = h('input', {
    type: 'range',
    class: 'size',
    min: SIZES.min,
    max: SIZES.max,
    step: 4,
    value: storedSize(),
    'aria-label': '缩略图大小',
    title: '缩略图大小',
    oninput: (e) => {
      document.getElementById('grid-wrap')?.style.setProperty('--cell', `${e.target.value}px`);
      try {
        localStorage.setItem(SIZE_KEY, e.target.value);
      } catch { /* 隐私模式 */ }
    },
  });
  const full = h('button', {
    class: 'btn sm ghost', type: 'button', 'aria-pressed': String(storedFull()), title: '显示整张照片，包括宽幅和竖拍',
    onclick: () => {
      const on = full.getAttribute('aria-pressed') !== 'true';
      full.setAttribute('aria-pressed', String(on));
      document.getElementById('grid-wrap')?.classList.toggle('show-full', on);
      try { localStorage.setItem(FULL_KEY, String(on)); } catch { /* 隐私模式 */ }
    },
  }, '完整显示');
  bar.replaceChildren(h('span', { class: 't' }, title), count, h('span', { class: 'grow' }), full, icon('layout-grid', 'faint'), size);
}

async function hydratePartners(photos, isCurrent) {
  const missing = missingPartnerIds(photos, state.partners);
  if (!missing.length) return;
  const results = await Promise.all(missing.map((id) => api(`/api/photos/${id}`).catch(() => null)));
  if (!isCurrent()) return;
  let added = 0;
  for (const p of results) {
    if (p && p.id && !state.partners.has(p.id)) {
      state.partners.set(p.id, p);
      added += 1;
    }
  }
  if (added) {
    syncLibraryPairs();
    renderGrid({ incremental: true });
    updateCount();
  }
}

// 第一页没到之前：150ms 内什么都不画，超过就放骨架格子（加载态分档，见 busy.js）
async function loadPhotos({ quiet = false } = {}) {
  const seq = ++loadSeq;
  const isCurrent = () => seq === loadSeq && galleryCurrent();
  const wrap = document.getElementById('grid-wrap');
  const before = state.photos;
  quiet = quiet && !!wrap && !!wrap.querySelector('[role="option"]');
  if (!quiet) renderLibbar();
  const skeleton = delayed(() => {
    if (quiet || !isCurrent() || !wrap) return;
    wrap.setAttribute('aria-busy', 'true');
    wrap.replaceChildren(h('div', { class: 'cells' }, Array.from({ length: 12 }, () => h('div', { class: 'cell ph skel', 'aria-hidden': 'true' }))));
  });
  let first = true;
  let shown = 0;
  let photos;
  try {
    photos = await fetchAllPhotos(state.galleryDate, {
      isCurrent,
      onPage: (sofar, done) => {
        if (quiet || !isCurrent()) return;
        skeleton.cancel();
        state.photos = sofar;
        // 还没取完：大图据此自己把剩下的页取完，不把这一页当成全部（F07）
        state.photosComplete = done;
        syncLibraryPairs();
        const delta = sofar.slice(shown);
        if (first) {
          first = false;
          renderGrid();
        } else if (state.galleryFilter === 'halfframe' || delta.some((p) => p.pair)) {
          renderGrid({ incremental: true });
        } else {
          appendCells(delta);
        }
        shown = sofar.length;
        updateCount();
        hydratePartners(sofar, isCurrent);
      },
    });
  } catch (e) {
    skeleton.cancel();
    if (quiet || !isCurrent() || !wrap) return; // 后台刷新失败：留着现在的网格，下次再刷
    wrap.setAttribute('aria-busy', 'false');
    wrap.replaceChildren(h('div', { class: 'empty' },
      h('h2', {}, '无法读取照片列表'),
      h('p', {}, e.message || '请重试。'),
      h('div', { class: 'acts' }, h('button', { class: 'btn', type: 'button', onclick: () => loadPhotos() }, '重试'))));
    return;
  }
  skeleton.cancel();
  if (!photos || !isCurrent()) return;
  state.photos = photos;
  state.photosComplete = true;
  syncLibraryPairs();
  if (quiet) {
    if (!sameIds(before, photos)) renderGrid();
    updateCount();
    hydratePartners(photos, isCurrent);
    return;
  }
  const rest = photos.slice(shown);
  if (first) renderGrid();
  else if (rest.length && (state.galleryFilter === 'halfframe' || rest.some((p) => p.pair))) renderGrid({ incremental: true });
  else if (rest.length) appendCells(rest);
  wrap?.setAttribute('aria-busy', 'false');
  updateCount();
  hydratePartners(photos, isCurrent);
}

export function sameIds(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  // 整条比：编辑过、只有 RAW 等角标跟着字段变，变了要重画
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i] && JSON.stringify(a[i]) !== JSON.stringify(b[i])) return false;
  return true;
}

function updateCount() {
  const el = document.getElementById('lib-count');
  if (el) el.textContent = state.photos.length ? libCountText() : (state.galleryFilter === 'halfframe' ? '· 0 对' : '0 张');
  updateGalleryExport();
}

export function galleryExportLabel(date, count, selected) {
  const n = typeof selected === 'object' && selected ? Number(selected.items || 0) : Number(selected || 0);
  if (n) return `导出选中的 ${selectionNoun(selected)}`;
  if (date !== ALL) return count === null ? '导出这一天…' : `导出 ${fmtDate(date)} 的 ${count} 张`;
  return '导出';
}

export function galleryExportCount() {
  if (state.galleryDate === ALL) return null;
  const fromList = state.photosComplete ? state.photos.length : null;
  const fromDates = (state.dates || []).find((d) => d.date === state.galleryDate)?.count;
  return fromList ?? fromDates ?? null;
}

export function updateGalleryExport(active = true) {
  const btn = document.getElementById('btn-export-top');
  if (!btn) return;
  const stats = currentSelectionStats();
  const label = active ? galleryExportLabel(state.galleryDate, galleryExportCount(), stats) : '导出';
  const span = btn.querySelector('span');
  if (span) span.textContent = label;
  btn.title = `${label}（${modKey()}E）`;
}

function syncCell(el, p) {
  el.setAttribute('aria-selected', String(isPhotoSelected(state.selection, p)));
  el.classList.toggle('focus', p.id === focusId);
  el.tabIndex = p.id === focusId ? 0 : -1;
}

function renderGrid({ incremental = false } = {}) {
  const wrap = document.getElementById('grid-wrap');
  if (!wrap) return;
  wrap.setAttribute('aria-busy', 'false');
  const vis = visiblePhotos();
  if (!vis.length) {
    const empty = state.galleryFilter === 'halfframe'
      ? h('div', { class: 'empty' }, h('h2', {}, '没有成对的半格照片'))
      : (state.galleryDate ? h('div', { class: 'empty' }, h('h2', {}, '这一天没有照片')) : emptyLibrary());
    wrap.replaceChildren(empty);
    renderSelBar();
    return;
  }
  if (!focusId || !vis.some((p) => p.id === focusId)) focusId = vis[0].id;
  // 选了某一天：顶上的标题已经写了这个日期，分组标题只留给读屏，不再重复一遍
  wrap.classList.toggle('single-day', state.galleryDate !== ALL);
  if (incremental) {
    const unit = state.galleryFilter === 'halfframe' ? '对' : '张';
    const idxById = new Map(vis.map((p, i) => [p.id, i]));
    patchDayGrid(wrap, vis, { h, createCell: (p) => cell(p, idxById.get(p.id) ?? EAGER_THUMBS), syncCell, fmtDate, countOn, unit });
  } else {
    wrap.replaceChildren();
    appendCells(vis);
  }
  renderSelBar();
}

const EAGER_THUMBS = 16;

// 按日期分组往网格末尾加：和最后一组同一天就接着放，不同就新开一组
function appendCells(list) {
  const wrap = document.getElementById('grid-wrap');
  if (!wrap) return;
  let group = wrap.lastElementChild && wrap.lastElementChild.classList.contains('day') ? wrap.lastElementChild : null;
  const unit = state.galleryFilter === 'halfframe' ? '对' : '张';
  let index = wrap.querySelectorAll('.cell').length;
  for (const p of list) {
    if (!group || group.dataset.date !== String(p.date)) {
      const hid = `day-${p.date || 'unknown'}`;
      group = h('div', { class: 'day', role: 'group', 'aria-labelledby': hid },
        h('h3', { class: 'num', id: hid }, p.date ? fmtDate(p.date) : '未知日期', h('span', {}, `${countOn(p.date)} ${unit}`)),
        h('div', { class: 'cells' }));
      group.dataset.date = String(p.date);
      wrap.append(group);
    }
    group.lastElementChild.append(cell(p, index++));
  }
}

function countOn(date) {
  if (state.galleryFilter === 'halfframe') {
    return visiblePhotos().filter((p) => p.date === date).length;
  }
  const d = (state.dates || []).find((x) => x.date === (date ?? 'unknown'));
  return d ? d.count : state.photos.filter((p) => p.date === date).length;
}

function cell(p, index = EAGER_THUMBS) {
  const selected = isPhotoSelected(state.selection, p);
  const pair = isPairSecond(p);
  const eager = index < EAGER_THUMBS;
  const img = h('img', { loading: eager ? 'eager' : 'lazy', ...(eager ? { fetchpriority: 'high' } : {}), decoding: 'async', draggable: 'false', alt: '', src: p.thumb_url });
  // 缩略图到了再淡入（.ready），之前是同色的空格子，不闪白
  const ready = () => img.classList.add('ready');
  if (img.complete && img.naturalWidth) ready();
  else {
    img.addEventListener('load', ready, { once: true });
    img.addEventListener('error', () => img.classList.add('ready', 'broken'), { once: true });
  }
  const badges = [];
  const hfBadge = halfframeBadge(h, icon, p);
  if (hfBadge) badges.push(hfBadge);
  if (!p.has_raw || !p.has_jpg) badges.push(pairTag(p));
  const el = h(
    'div',
    {
      class: `cell${pair ? ' pair' : ''}${p.id === focusId ? ' focus' : ''}`,
      role: 'option',
      tabindex: p.id === focusId ? 0 : -1,
      'aria-selected': String(selected),
      'aria-label': `${p.name}，${fmtTime(p.captured_at) || '时间未知'}`,
      title: `${p.name} · ${fmtTime(p.captured_at)}${p.date_source === 'mtime' ? '（使用文件修改日期）' : ''}`,
      onclick: (e) => onCellClick(e, p.id),
    },
    img,
    h('span', { class: 'ck', 'aria-hidden': 'true', title: '选择' }, icon('check')),
    h('span', { class: 'b' }, badges),
    p.sidecar_rev > 0 ? h('span', { class: 'e', title: '编辑过', 'aria-label': '编辑过' }, icon('pencil')) : null,
  );
  el.dataset.id = p.id;
  return el;
}

function openPhoto(id) {
  focusId = id;
  location.hash = `#/photo/${id}`;
}

function order() {
  return visiblePhotos().map((p) => p.id);
}

function setSelection(next) {
  selectionRevision++;
  state.selection.clear();
  for (const id of next) state.selection.add(id);
  for (const el of document.querySelectorAll('#grid-wrap .cell[role="option"]')) {
    el.setAttribute('aria-selected', String(state.selection.has(el.dataset.id)));
  }
  renderSelBar();
}

function toggleCellSelection(id) {
  const p = findKnownPhoto(id);
  if (p) return togglePairSelection(new Set(state.selection), p);
  return toggleSelect(state.selection, id);
}

// 点击：点左上角的圈、⌘/Ctrl 点 → 加选或取消；⇧ 点 → 连选；普通点击始终打开。
function onCellClick(e, id) {
  const tick = e.target.closest && e.target.closest('.ck');
  const previousFocus = focusId;
  moveFocus(id, false);
  if (e.shiftKey) {
    if (!anchorId) anchorId = previousFocus || id;
    const raw = rangeSelect(order(), anchorId, id, e.metaKey || e.ctrlKey ? state.selection : new Set());
    setSelection(expandSelectionIds(raw, state.photos, state.partners));
    return;
  }
  if (tick || e.metaKey || e.ctrlKey) {
    anchorId = id;
    setSelection(toggleCellSelection(id));
    return;
  }
  openPhoto(id);
}

function cellEl(id) {
  return id ? document.querySelector(`#grid-wrap .cell[data-id="${CSS.escape(id)}"]`) : null;
}

function moveFocus(id, focus = true) {
  const prev = cellEl(focusId);
  if (prev) {
    prev.tabIndex = -1;
    prev.classList.remove('focus');
  }
  focusId = id;
  const el = cellEl(id);
  if (!el) return;
  el.tabIndex = 0;
  el.classList.add('focus');
  if (focus) {
    el.focus({ preventScroll: true });
    el.scrollIntoView({ block: 'nearest' });
  }
}

// 上下：找上一行 / 下一行里横向位置最近的那张（跨日期分组也能走）
function verticalNeighbour(cells, i, dir) {
  const r0 = cells[i].getBoundingClientRect();
  const cx = r0.left + r0.width / 2;
  let rowTop = null;
  let best = null;
  for (let j = i + dir; j >= 0 && j < cells.length; j += dir) {
    const r = cells[j].getBoundingClientRect();
    if (Math.abs(r.top - r0.top) < 2) continue;
    if (rowTop === null) rowTop = r.top;
    if (Math.abs(r.top - rowTop) >= 2) break;
    const d = Math.abs(r.left + r.width / 2 - cx);
    if (!best || d < best.d) best = { j, d };
  }
  return best ? best.j : i;
}

function onGridKey(e) {
  const mod = e.metaKey || e.ctrlKey;
  if (mod && (e.key === 'a' || e.key === 'A')) {
    e.preventDefault();
    selectAll();
    return;
  }
  const cells = [...document.querySelectorAll('#grid-wrap .cell[role="option"]')];
  if (!cells.length) return;
  let i = cells.findIndex((c) => c.dataset.id === focusId);
  if (i < 0) i = 0;
  let j = i;
  switch (e.key) {
    case 'ArrowLeft': j = Math.max(0, i - 1); break;
    case 'ArrowRight': j = Math.min(cells.length - 1, i + 1); break;
    case 'ArrowUp': j = verticalNeighbour(cells, i, -1); break;
    case 'ArrowDown': j = verticalNeighbour(cells, i, 1); break;
    case 'Home': j = 0; break;
    case 'End': j = cells.length - 1; break;
    case ' ':
      e.preventDefault();
      anchorId = focusId;
      setSelection(toggleCellSelection(focusId));
      return;
    case 'Enter':
      e.preventDefault();
      openPhoto(focusId);
      return;
    case 'Escape':
      if (state.selection.size) {
        e.preventDefault();
        e.stopPropagation();
        clearSelection();
      }
      return;
    default:
      return;
  }
  e.preventDefault();
  const id = cells[j].dataset.id;
  if (e.shiftKey) {
    if (!anchorId) anchorId = focusId;
    setSelection(expandSelectionIds(rangeSelect(order(), anchorId, id), state.photos, state.partners));
  } else {
    anchorId = id;
  }
  moveFocus(id);
}

export function selectAll() {
  const vis = order();
  if (!vis.length) return;
  setSelection(expandSelectionIds(vis, state.photos, state.partners));
  dropTextSelection();
}

// 空图库（替代原来的四步引导，U9）：没有照片时说明怎么开始，有照片后不再出现
function emptyLibrary() {
  const card = cardPresent();
  return h(
    'div',
    { class: 'empty' },
    h('h2', {}, '还没有照片'),
    h('p', {}, card ? '存储卡已经插好，可以开始导入。' : '插入 GXR 的存储卡（或读卡器），GXR Studio 会提示导入。'),
    h('ol', {},
      h('li', {}, '导入：只读复制，卡上的文件不会被改动。'),
      h('li', {}, '点一张照片开始编辑：配方、调整、裁剪、边框和时间戳。'),
      h('li', {}, `按 ${modKey()}E 导出，按 ? 查看全部快捷键。`)),
    h('div', { class: 'acts' },
      h('button', {
        class: 'btn primary',
        type: 'button',
        'aria-disabled': card ? null : 'true',
        title: card ? null : '插入存储卡后可以导入',
        onclick: () => importNow(),
      }, '导入照片')),
  );
}

function selectedPhotos(ids = state.selection) {
  const seen = new Set();
  return [...ids].map(findKnownPhoto).filter((p) => p && !seen.has(p.id) && (seen.add(p.id), true));
}

export function exportSelection() {
  const visIds = visibleSelectionSet();
  const stats = selectionStats(visIds, state.photos, state.partners);
  if (!stats.items) return false;
  const label = stats.pairs > 0 && stats.items === 1 ? '选中的 1 项（半格一对）' : `选中的 ${selectionNoun(stats)}`;
  openExportDialog({ ids: [...visIds], label, photos: selectedPhotos(visIds) });
  return true;
}

function renderSelBar() {
  updateGalleryExport();
  const bar = document.getElementById('selbar');
  if (!bar) return;
  const stats = currentSelectionStats();
  if (!stats.items) { bar.hidden = true; bar.replaceChildren(); return; }
  bar.hidden = false;
  const countText = stats.pairs > 0
    ? (stats.items === stats.pairs ? `已选 ${stats.pairs} 对半格` : `已选 ${stats.items} 项（含 ${stats.pairs} 对半格）`)
    : `已选 ${stats.items} 张`;
  bar.replaceChildren(
    h('span', { class: 't', role: 'status' }, countText),
    h('button', { class: 'btn ghost', type: 'button', title: `全选（${modKey()}A）`, onclick: selectAll }, '全选'),
    h('button', { class: 'btn ghost', type: 'button', title: '取消选择（Esc）', onclick: clearSelection }, '取消选择'),
    pasteSelectionButton(stats),
    h('button', { class: 'btn primary', type: 'button', title: `导出（${modKey()}E）`, onclick: exportSelection }, '导出…'),
  );
}

export function clearSelection() {
  anchorId = null;
  setSelection(new Set());
  dropTextSelection();
}

// 万一浏览器留下了文字选中（网格里本来禁止选字），按 Esc 一并清掉，整页不会一直是高亮的
function dropTextSelection() {
  globalThis.getSelection?.()?.removeAllRanges?.();
}

// Enter：打开最近点过的那张（没有就打开第一张）
export function openFocused() {
  const vis = visiblePhotos();
  const id = focusId || (vis[0] && vis[0].id);
  if (id) openPhoto(id);
}

// 图库键盘入口（app.js 在图库页、焦点不在输入框时调用）：⌘A 全选；焦点不在网格里时方向键先把焦点放进网格
export function galleryKeys(e) {
  const mod = e.metaKey || e.ctrlKey;
  if (mod && (e.key === 'a' || e.key === 'A')) {
    selectAll();
    return true;
  }
  if (e.key === 'Escape' && state.selection.size) {
    clearSelection();
    return true;
  }
  if (!mod && e.key.startsWith('Arrow') && !document.getElementById('grid-wrap')?.contains(document.activeElement)) {
    openFocusCell();
    return true;
  }
  return false;
}

function openFocusCell() {
  const vis = visiblePhotos();
  const id = focusId || (vis[0] && vis[0].id);
  if (id) moveFocus(id);
}
