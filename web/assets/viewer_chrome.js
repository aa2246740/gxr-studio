// 编辑器外壳控件：顶栏、视图工具栏（含半格视图切换）、胶片条、画面标签、标签红点、单张只读态。

import { state } from './state.js';
import { h, button, icon, fmtClock, fmtDate, modKey } from './dom.js';
import { pairTag } from './gallery.js';
import { stageLabels, undoChange, redoChange, syncUndo } from './preview.js';
import { editorCopyPaste } from './paste_edits.js';
import {
  foldPhotos, isPairSecond, isSinglePairView, isUntouchedPairRecipe, partnerOf,
} from './halfframe.js';

const FILM_WINDOW = 40;
const PAIR_VIEWS = [['diptych', '双联'], ['first', '第 1 张'], ['second', '第 2 张']];

export function viewerPhotoList() {
  return foldPhotos(state.photos, { partners: state.partners, activeDate: state.galleryDate });
}

export function paintTabMods(v, tabs) {
  if (!v || !v.tabBtns) return;
  const ed = v.ed;
  const adj = ed ? Object.values(ed.adj || {}).some((x) => Number(x) !== 0) || !!ed.curve : false;
  const g = ed && ed.v2 ? ed.geometry || {} : {};
  const crop = !!(v.crop && v.crop.rect && (v.crop.rect.w < 0.999 || v.crop.rect.h < 0.999))
    || !!(g.straighten || g.vertical || g.horizontal || g.distortion || g.rotate || g.flip);
  const mods = {
    recipe: !!(ed && !isUntouchedPairRecipe(v) && (ed.recipe || ed.lut)),
    adjust: adj,
    crop,
    frame: !!(ed && ed.frame && ed.frame.style !== 'none'),
    stamp: !!(ed && ed.stamp && ed.stamp.enabled),
    info: false,
  };
  for (const [id] of tabs) {
    const dot = v.tabBtns[id].querySelector('.mod');
    if (dot) dot.hidden = !mods[id];
  }
}

export function paintLabels(v, currentView) {
  if (!currentView || v !== currentView || !v.labelsEl) return;
  const items = [];
  if (v.showOriginal) {
    const text = isPairSecond(v.photo) && !isSinglePairView(v) ? '原图 · 相机双联' : '原图';
    items.push(h('span', { class: 'tag' }, text));
  } else if (v.split) {
    items.push(h('span', { class: 'tag' }, '左：原图'), h('span', { class: 'tag' }, '右：编辑后'));
  }
  for (const l of v.showOriginal ? [] : stageLabels(v)) {
    items.push(h('span', { class: `tag ${l.kind === 'warn' ? 'warn' : ''}`, title: l.title || null }, l.text));
  }
  v.labelsEl.replaceChildren(...items);
}

function titleBadge(p, p1) {
  if (isPairSecond(p) && p.has_raw && p.has_jpg && (!p1 || (p1.has_raw && p1.has_jpg))) {
    return h('span', { class: 'badge pair tag' }, 'RAW+JPG ×2');
  }
  return pairTag(p);
}

export function renderTitlebar(v) {
  const bar = document.getElementById('titlebar');
  const slot = document.getElementById('tb-edit');
  if (bar) bar.dataset.mode = 'viewer';
  if (!slot) return;
  const p = v.photo;
  const pair = isPairSecond(p);
  const p1 = pair ? partnerOf(p, state.photos, state.partners) : null;
  const undo = button('', { kind: 'icon', ico: 'undo-2', title: `撤销（${modKey()}Z）`, onclick: () => undoChange(v) });
  const redo = button('', { kind: 'icon', ico: 'redo-2', title: `重做（${modKey() === '⌘' ? '⇧⌘Z' : 'Ctrl+Shift+Z'}）`, onclick: () => redoChange(v) });
  v.undoBtn = undo;
  v.redoBtn = redo;
  syncUndo(v);
  const titleName = pair ? '半格一对' : p.name;
  const titleMeta = pair
    ? (p1 ? `${p1.name} + ${p.name}` : p.name)
    : `${fmtDate(p.date)} ${fmtClock(p.captured_at)}`;
  slot.replaceChildren(...[
    button('照片', { kind: 'ghost', ico: 'chevron-left', title: '回到图库（Esc）', onclick: () => { location.hash = '#/photos'; } }),
    h('span', { class: 'sep-v' }),
    undo,
    redo,
    ...editorCopyPaste(v),
    h('div', { class: 'tb-title' },
      h('span', { class: 'name' }, titleName),
      h('span', { class: 'meta' }, titleMeta),
      titleBadge(p, p1),
      p.date_source === 'mtime' ? h('span', { class: 'tag warn', title: '没有拍摄时间，使用文件修改日期' }, '使用文件修改日期') : null),
  ].filter(Boolean));
  applyPairReadonly(v);
}

export function clearTitlebar() {
  const bar = document.getElementById('titlebar');
  const slot = document.getElementById('tb-edit');
  if (bar && bar.dataset.mode === 'viewer') bar.dataset.mode = 'library';
  if (slot) slot.replaceChildren();
}

export function renderViewbar(v, actions) {
  const bgs = actions.backgrounds.map(([id, name, hex]) => {
    const b = h('button', { type: 'button', class: 'swatch', title: `背景：${name}`, 'aria-label': `背景：${name}`, 'aria-pressed': 'false', onclick: () => actions.setBackground(id) }, h('i', {}));
    b.firstChild.style.background = hex;
    b.dataset.bg = id;
    return b;
  });
  const orig = button('', { kind: 'icon', ico: 'eye', title: '原图（\\，按住空格临时看）', onclick: () => { v.pinOriginal = !v.showOriginal; actions.setShowOriginal(!v.showOriginal); } });
  const split = button('', { kind: 'icon', ico: 'columns-2', title: '左右对比（Y）', onclick: () => actions.setSplit(!v.split) });
  const pairSeg = isPairSecond(v.photo)
    ? h('div', { class: 'seg', role: 'group', 'aria-label': '半格视图' },
        ...PAIR_VIEWS.map(([key, label]) => {
          const b = h('button', {
            type: 'button',
            'aria-pressed': String((v.pairView || 'diptych') === key),
            onclick: () => actions.setPairView(key),
          }, label);
          b.dataset.pv = key;
          return b;
        }))
    : null;
  const prev = button('', { kind: 'icon', ico: 'chevron-left', title: '上一张（←）', onclick: () => actions.step(-1) });
  const next = button('', { kind: 'icon', ico: 'chevron-right', title: '下一张（→）', onclick: () => actions.step(1) });
  const counter = h('span', { class: 'info num', 'aria-live': 'off' });
  const zoom = h('div', { class: 'seg', role: 'group', 'aria-label': '缩放' },
    h('button', { type: 'button', 'aria-pressed': 'true', title: '适合（Z）', onclick: () => actions.setZoom(false) }, '适合'),
    h('button', { type: 'button', 'aria-pressed': 'false', title: '100%（Z）', onclick: () => actions.setZoom(true) }, '100%'));
  const panelBtn = button('', { kind: 'icon', ico: 'panel-right', title: '显示 / 隐藏右侧面板', onclick: () => actions.setCollapsed(!v.collapsed) });
  v.vb = { bgs, orig, split, pairSeg, prev, next, counter, zoom, panelBtn };
  const parts = [...bgs, h('span', { class: 'sep-v' }), orig, split];
  if (pairSeg) parts.push(h('span', { class: 'sep-v' }), pairSeg);
  parts.push(h('span', { class: 'grow' }), prev, counter, next, h('span', { class: 'grow' }), zoom, panelBtn);
  v.viewbar.replaceChildren(...parts);
  syncViewbar(v);
}

export function syncViewbar(v) {
  if (!v || !v.vb) return;
  const { bgs, orig, split, pairSeg, prev, next, counter, zoom, panelBtn } = v.vb;
  for (const b of bgs) b.setAttribute('aria-pressed', String(b.dataset.bg === v.bg));
  orig.setAttribute('aria-pressed', String(!!v.showOriginal));
  split.setAttribute('aria-pressed', String(!!v.split));
  if (pairSeg) {
    const mode = v.pairView || 'diptych';
    for (const b of pairSeg.children) b.setAttribute('aria-pressed', String(b.dataset.pv === mode));
  }
  const list = viewerPhotoList();
  const pos = list.findIndex((p) => p.id === v.photo.id);
  const disable = (b, off) => (off ? b.setAttribute('aria-disabled', 'true') : b.removeAttribute('aria-disabled'));
  disable(prev, pos <= 0);
  disable(next, pos < 0 || pos >= list.length - 1);
  counter.textContent = pos >= 0 ? `${pos + 1} / ${list.length}` : '';
  const [fit, full] = zoom.children;
  fit.setAttribute('aria-pressed', String(!v.zoom100));
  full.setAttribute('aria-pressed', String(!!v.zoom100));
  panelBtn.setAttribute('aria-pressed', String(!v.collapsed));
}

export function renderFilmstrip(v) {
  const list = viewerPhotoList();
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
    const pair = isPairSecond(p);
    const badges = [];
    if (pair) badges.push(h('span', { class: 'badge hf', title: '半格一对' }, icon('columns-2'), '半格'));
    if (p.hdr === 'ready') badges.push(h('span', { class: 'badge hdr', title: '图库里已生成 HDR 版本' }, 'HDR'));
    return h('button', {
      type: 'button',
      class: pair ? 'fthumb pair' : 'fthumb',
      title: pair ? '半格一对' : p.name,
      'aria-label': pair ? '半格一对' : p.name,
      'aria-current': cur ? 'true' : null,
      onclick: () => { if (!cur) location.hash = `#/photo/${p.id}`; },
    }, img, badges.length ? h('span', { class: 'b' }, ...badges) : null);
  });
  v.film.replaceChildren(...items);
  const current = v.film.querySelector('[aria-current="true"]');
  if (current && current.scrollIntoView) current.scrollIntoView({ block: 'nearest', inline: 'center' });
}

function toggleControlReadonly(el, ro) {
  if (ro) {
    if (!el.disabled) {
      el.dataset.pairRo = '1';
      el.disabled = true;
    }
    if (el.classList.contains('lcard') && el.getAttribute('aria-disabled') !== 'true') {
      el.dataset.pairRoAria = '1';
      el.setAttribute('aria-disabled', 'true');
    }
    return;
  }
  if (el.dataset.pairRo === '1') {
    el.disabled = false;
    delete el.dataset.pairRo;
  }
  if (el.dataset.pairRoAria === '1') {
    el.removeAttribute('aria-disabled');
    delete el.dataset.pairRoAria;
  }
}

export function applyPairReadonly(v) {
  if (!v) return;
  const ro = isSinglePairView(v);
  if (v.panelEl) {
    v.panelEl.classList.toggle('pair-readonly', ro);
    if (ro) v.panelEl.dataset.readonly = '1';
    else delete v.panelEl.dataset.readonly;
    const roots = [...v.panelEl.querySelectorAll('[role="tabpanel"]:not(#tp-info)')];
    if (v.panelFoot) roots.push(v.panelFoot);
    for (const r of roots) {
      for (const el of r.querySelectorAll('button, input, select, textarea')) {
        toggleControlReadonly(el, ro);
      }
    }
  }
  const pasteBtn = document.querySelector('#tb-edit [data-paste="1"]');
  for (const b of [v.undoBtn, v.redoBtn, pasteBtn]) {
    if (!b) continue;
    toggleControlReadonly(b, ro);
  }
  if (!ro) syncUndo(v);
}
