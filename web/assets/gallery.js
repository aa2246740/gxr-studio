// 图库：左边按拍摄日期筛选，右边按日期分组的网格；多选（⇧ 连选、⌘ 点选、⌘A 全选、方向键），导出选中的。
// 照片列表分页读完（photos.js），第一页到了就先画出来，后面的接着往下加，不再截在 5000 张（U12）。
// 网格是一个 listbox，每张照片是 option（aria-selected），不再在 role=button 里套复选框（U16）。

import { api } from './api.js';
import { state, on } from './state.js';
import { h, icon, fmtDate, fmtTime, modKey } from './dom.js';
import { openExportDialog } from './export.js';
import { cardPresent, importNow } from './import.js';
import { isPrediction, rawOnlyBadgeText } from './look_notes.js';
import { fetchAllPhotos } from './photos.js';
import { delayed } from './busy.js';

const ALL = null;
const SIZE_KEY = 'gxr-studio.thumb-size';
const SIZES = { min: 120, max: 320, dflt: 196 };
let root = null;
let focusId = null;
let anchorId = null;
let loadSeq = 0;

export function pairTag(p) {
  if (p.has_raw && p.has_jpg) return h('span', { class: 'badge pair tag' }, 'RAW+JPG');
  if (p.has_jpg) return h('span', { class: 'badge tag gray' }, '只有 JPG');
  // 只有 RAW 的照片没有相机 JPG 可看，缩略图是电脑画的：如实标出来
  const look = p.preview_look || {};
  return h('span', { class: `badge tag ${isPrediction(look) ? 'pred' : 'gray'}`, title: look.label || null }, rawOnlyBadgeText(look));
}

// 选择逻辑是纯函数，方便单独测（tests/test_gallery_select.py）
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

// isCurrent：路由层给的「图库还是当前页面吗」（R11）。过时的列表不写进 state.photos，也不改页面。
let galleryCurrent = () => true;
export async function renderGallery(container, isCurrent = () => true) {
  galleryCurrent = isCurrent;
  root = container;
  const grid = h('div', {
    class: 'grid',
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
  const btn = h(
    'button',
    {
      type: 'button',
      class: 'it',
      'aria-pressed': String(state.galleryDate === date),
      onclick: () => {
        if (state.galleryDate === date) return;
        state.galleryDate = date;
        anchorId = null;
        for (const b of document.querySelectorAll('#dates .it')) b.setAttribute('aria-pressed', String(b === btn));
        loadAll();
      },
    },
    h('span', { class: numeric ? 'num' : '' }, label),
    h('span', { class: 'n' }, String(count)),
  );
  return btn;
}

async function loadDates() {
  const dates = await api('/api/dates').catch(() => null);
  if (!galleryCurrent()) return;
  if (dates) state.dates = dates;
  const total = (state.dates || []).reduce((n, d) => n + d.count, 0);
  const aside = document.getElementById('dates');
  if (!aside) return;
  aside.replaceChildren(
    h('div', { class: 'grp' }, '图库'),
    sideItem('全部照片', total, ALL),
    (state.dates || []).length ? h('div', { class: 'grp' }, '日期') : null,
    ...(state.dates || []).map((d) => sideItem(d.date ? fmtDate(d.date) : '未知日期', d.count, d.date, true)),
  );
}

function renderLibbar() {
  const bar = document.getElementById('libbar');
  if (!bar) return;
  const title = state.galleryDate ? fmtDate(state.galleryDate) : '全部照片';
  const count = h('span', { class: 'faint num', id: 'lib-count' }, state.photos && state.photos.length ? `${state.photos.length} 张` : '');
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
  bar.replaceChildren(h('span', { class: 't' }, title), count, h('span', { class: 'grow' }), icon('layout-grid', 'faint'), size);
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
        if (first) {
          first = false;
          renderGrid();
        } else {
          appendCells(sofar.slice(shown));
        }
        shown = sofar.length;
        updateCount();
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
  if (quiet) {
    if (!sameIds(before, photos)) renderGrid();
    updateCount();
    return;
  }
  if (first) renderGrid();
  else if (photos.length > shown) appendCells(photos.slice(shown));
  wrap?.setAttribute('aria-busy', 'false');
  updateCount();
}

export function sameIds(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  // 整条比：角标（编辑过、HDR、只有 RAW）跟着字段变，变了要重画
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i] && JSON.stringify(a[i]) !== JSON.stringify(b[i])) return false;
  return true;
}

function updateCount() {
  const el = document.getElementById('lib-count');
  if (el) el.textContent = `${state.photos.length} 张`;
}

function renderGrid() {
  const wrap = document.getElementById('grid-wrap');
  if (!wrap) return;
  wrap.setAttribute('aria-busy', 'false');
  if (!state.photos.length) {
    wrap.replaceChildren(state.galleryDate ? h('div', { class: 'empty' }, h('h2', {}, '这一天没有照片')) : emptyLibrary());
    renderSelBar();
    return;
  }
  if (!focusId || !state.photos.some((p) => p.id === focusId)) focusId = state.photos[0].id;
  // 选了某一天：顶上的标题已经写了这个日期，分组标题只留给读屏，不再重复一遍
  wrap.classList.toggle('single-day', state.galleryDate !== ALL);
  wrap.replaceChildren();
  appendCells(state.photos);
  renderSelBar();
}

// 按日期分组往网格末尾加：和最后一组同一天就接着放，不同就新开一组
function appendCells(list) {
  const wrap = document.getElementById('grid-wrap');
  if (!wrap) return;
  let group = wrap.lastElementChild && wrap.lastElementChild.classList.contains('day') ? wrap.lastElementChild : null;
  for (const p of list) {
    if (!group || group.dataset.date !== String(p.date)) {
      const hid = `day-${p.date || 'unknown'}`;
      group = h('div', { class: 'day', role: 'group', 'aria-labelledby': hid },
        h('h3', { class: 'num', id: hid }, p.date ? fmtDate(p.date) : '未知日期', h('span', {}, `${countOn(p.date)} 张`)),
        h('div', { class: 'cells' }));
      group.dataset.date = String(p.date);
      wrap.append(group);
    }
    group.lastElementChild.append(cell(p));
  }
}

function countOn(date) {
  const d = (state.dates || []).find((x) => x.date === date);
  return d ? d.count : state.photos.filter((p) => p.date === date).length;
}

function cell(p) {
  const selected = state.selection.has(p.id);
  const img = h('img', { src: p.thumb_url, alt: '', loading: 'lazy', decoding: 'async', draggable: 'false' });
  // 缩略图到了再淡入（.ready），之前是同色的空格子，不闪白
  const ready = () => img.classList.add('ready');
  if (img.complete && img.naturalWidth) ready();
  else {
    img.addEventListener('load', ready, { once: true });
    img.addEventListener('error', () => img.classList.add('ready', 'broken'), { once: true });
  }
  const badges = [];
  if (p.hdr === 'ready') badges.push(h('span', { class: 'badge hdr', title: '已生成 HDR 版本' }, 'HDR'));
  if (!p.has_raw || !p.has_jpg) badges.push(pairTag(p));
  const el = h(
    'div',
    {
      class: `cell${p.id === focusId ? ' focus' : ''}`,
      role: 'option',
      tabindex: p.id === focusId ? 0 : -1,
      'aria-selected': String(selected),
      'aria-label': `${p.name}，${fmtTime(p.captured_at) || '时间未知'}`,
      title: `${p.name} · ${fmtTime(p.captured_at)}${p.date_source === 'mtime' ? '（使用文件修改日期）' : ''}`,
      onclick: (e) => onCellClick(e, p.id),
      ondblclick: () => openPhoto(p.id),
    },
    img,
    h('span', { class: 'ck', 'aria-hidden': 'true', title: '选择' }, icon('check')),
    h('span', { class: 'b' }, badges),
  );
  el.dataset.id = p.id;
  return el;
}

function openPhoto(id) {
  focusId = id;
  location.hash = `#/photo/${id}`;
}

function order() {
  return state.photos.map((p) => p.id);
}

function setSelection(next) {
  state.selection.clear();
  for (const id of next) state.selection.add(id);
  for (const el of document.querySelectorAll('#grid-wrap .cell[role="option"]')) {
    el.setAttribute('aria-selected', String(state.selection.has(el.dataset.id)));
  }
  renderSelBar();
}

// 点击：点右上角的圈、⌘/Ctrl 点 → 加选或取消；⇧ 点 → 连选；已经在选照片时，普通点击也是加选；否则打开
function onCellClick(e, id) {
  const tick = e.target.closest && e.target.closest('.ck');
  moveFocus(id, false);
  if (e.shiftKey && anchorId) {
    setSelection(rangeSelect(order(), anchorId, id, e.metaKey || e.ctrlKey ? state.selection : new Set()));
    return;
  }
  if (tick || e.metaKey || e.ctrlKey || state.selection.size) {
    anchorId = id;
    setSelection(toggleSelect(state.selection, id));
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
      setSelection(toggleSelect(state.selection, focusId));
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
    setSelection(rangeSelect(order(), anchorId, id));
  } else {
    anchorId = id;
  }
  moveFocus(id);
}

export function selectAll() {
  if (!state.photos.length) return;
  setSelection(new Set(order()));
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

function selectedPhotos() {
  return state.photos.filter((p) => state.selection.has(p.id));
}

export function exportSelection() {
  const n = state.selection.size;
  if (!n) return false;
  openExportDialog({ ids: [...state.selection], label: `选中的 ${n} 张`, photos: selectedPhotos() });
  return true;
}

function renderSelBar() {
  const bar = document.getElementById('selbar');
  if (!bar) return;
  const n = state.selection.size;
  bar.hidden = !n;
  if (!n) {
    bar.replaceChildren();
    return;
  }
  bar.replaceChildren(
    h('span', { class: 't', role: 'status' }, `已选 ${n} 张`),
    h('button', { class: 'btn ghost', type: 'button', title: `全选（${modKey()}A）`, onclick: selectAll }, '全选'),
    h('button', { class: 'btn ghost', type: 'button', title: '取消选择（Esc）', onclick: clearSelection }, '取消选择'),
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
  const id = focusId || (state.photos[0] && state.photos[0].id);
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
  const id = focusId || (state.photos[0] && state.photos[0].id);
  if (id) moveFocus(id);
}
