// 大图的画面区。三种状态（DESIGN 1.3）：
//   编辑后：stage=final，大图默认是这个（配方、调整、拉直、裁剪、边框、时间戳都在里面）。
//   裁剪中：stage=uncropped，只有「裁剪」标签时才有，画上裁剪框。
//   原图：按住空格或按 \\，照片本身（没有任何编辑）。
// 另外两个视图选项：左右对比（Y，左半边是没有颜色编辑的同一画面）和 适合 / 100%（Z）。
// 画面是后端渲染好的图（POST /render 的返回）或显卡预览的画布，前端不再自己画边框或时间戳。

import { h } from './dom.js';
import { beginChange, endChange, saveCrop, validRect, originalUrl, originalFallbackUrl, renderProblem, geometryOf, beforeUrl } from './preview.js';
import { cropBody, guidesSvg, guideFade, lockRatio, orientationNow, rectArray, pixelsText } from './crop.js';
import { fitInto, moveRect, resizeRect } from './geom.js';
import { gpuCanvas } from './gpu_stage.js';

const pct = (v) => `${(v * 100).toFixed(4)}%`;

export function stageMode(view) {
  if (view.showOriginal) return 'orig';
  return view.tab === 'crop' ? 'uncropped' : 'final';
}

function note(text, cls = 'dim') {
  return h('p', { class: `stage-note ${cls}` }, text);
}

export function paintStage(view) {
  const area = view.stageArea;
  if (!area) return;
  const mode = stageMode(view);
  view.mode = mode;
  const ed = view.ed;
  const maxW = Math.max(120, area.clientWidth - 48);
  const maxH = Math.max(120, area.clientHeight - 32);
  // 100%：一个图像像素对一个屏幕像素，放不下就滚动（只在编辑后和原图上，裁剪中总是适合）
  const natural = !!view.zoom100 && mode !== 'uncropped';
  area.classList.toggle('zoom100', natural);
  // 显卡预览接手了这个画面（gpu_stage.js）：画布只建一次，这里只调尺寸和叠加的裁剪框
  const cv = ed && view.gpuActive && view.gpuActive === mode ? gpuCanvas(view, maxW, maxH, { natural }) : null;
  area.classList.toggle('gpu', !!cv);
  if (cv) {
    paintGpu(view, area, cv, mode);
    paintSplit(view, area.firstChild, mode);
    return;
  }
  let src = null;
  if (mode === 'orig') src = originalUrl(view);
  else if (ed) src = ed.urls[mode];

  if (!ed && mode !== 'orig') children.push(note('正在读取…'));
  else if (!src) children.push(note('正在生成预览…'));

  const pv = h('div', { class: 'pv' });
  pv.dataset.mode = mode;
  pv.dataset.src = src;
  pv.dataset.natural = String(natural);
  const img = h('img', {
    class: 'pv-img',
    alt: view.photo.name,
    draggable: 'false',
    decoding: 'async',
    style: natural ? {} : { maxWidth: `${maxW}px`, maxHeight: `${maxH}px` },
  });
  // 旧版后端的裁切画面不带拉直：这里用浏览器旋转做个近似（只在裁切中，松手后由后端算准）
  if (mode === 'uncropped' && ed && !ed.v2) {
    const s = geometryOf(view).straighten || 0;
    if (s) img.style.transform = `rotate(${s}deg)`;
  }
  img.addEventListener('load', () => {
    if (natural) img.style.width = `${Math.round(img.naturalWidth / (globalThis.devicePixelRatio || 1))}px`;
    if (mode !== 'uncropped') return;
    const prev = view.canvas;
    const next = { w: img.naturalWidth, h: img.naturalHeight };
    view.canvas = next;
    if (!view.box || !pv.contains(view.box)) pv.append(cropBox(view, pv));
    if (!prev || prev.w !== next.w || prev.h !== next.h) view.redrawSide?.();
  });
  img.addEventListener('error', () => {
    // 原图的新接口还没有（404）：退回原来的预览图，只退一次
    if (mode === 'orig' && img.dataset.fallback !== '1') {
      img.dataset.fallback = '1';
      img.src = originalFallbackUrl(view);
      return;
    }
    img.addEventListener('load', () => {
      if (mode !== 'uncropped') return;
      const prev = view.canvas;
      const next = { w: img.naturalWidth, h: img.naturalHeight };
      view.canvas = next;
      if (!view.box || !pv.contains(view.box)) pv.append(cropBox(view, pv));
      if (!prev || prev.w !== next.w || prev.h !== next.h) view.redrawSide?.();
    });
    img.addEventListener('error', () => {
      // 原图的新接口还没有（404）：退回原来的预览图，只退一次
      if (mode === 'orig' && img.dataset.fallback !== '1') {
        img.dataset.fallback = '1';
        img.src = originalFallbackUrl(view);
        return;
      }
      area.replaceChildren(note('无法读取这张照片。请重试。', 'warn-text'));
    });
    pv.append(img);
    // 裁切的尺寸放在画面右下角，只写数字（COPY 3.1）
    if (mode === 'uncropped' && view.crop && (view.crop.frame || view.canvas)) pv.append(h('div', { class: 'crop-size' }, pixelsText(view)));
    children.push(pv);
  }
}

function childWith(area, cls) {
  for (const c of area.children) if (c.classList && c.classList.contains(cls)) return c;
  return null;
}

function paintProblem(view, area, mode) {
  const old = childWith(area, 'stage-note');
  if (old) old.remove();
  if (mode === 'orig') return;
  const problem = renderProblem(view);
  if (problem) area.append(note(problem, 'warn-text'));
}

// 占位：图库里的缩略图放大到画面大小（有点糊，但位置、构图和颜色都对，比空白好）
function placeholder(view, maxW, maxH) {
  const pv = h('div', { class: 'pv placeholder' });
  pv.dataset.placeholder = '1';
  const img = h('img', { class: 'pv-img', src: view.photo.thumb_url, alt: '', draggable: 'false' });
  img.addEventListener('load', () => sizePlaceholder(pv, maxW, maxH));
  pv.append(img);
  return pv;
}

function sizePlaceholder(pv, maxW, maxH) {
  const img = pv.querySelector('.pv-img');
  if (!img || !img.naturalWidth) return;
  const t = Math.min(maxW / img.naturalWidth, maxH / img.naturalHeight);
  Object.assign(img.style, { width: `${Math.round(img.naturalWidth * t)}px`, height: `${Math.round(img.naturalHeight * t)}px` });
}

// 左右对比：左半边盖一张「没有颜色编辑」的同一画面（同样的裁剪和边框，所以和右边严丝合缝），中间一条分界线。
// 显卡预览时也是同一块画布加一张图，不开第二块画布。
function paintSplit(view, pv, mode) {
  if (!pv) return;
  const old = pv.querySelector('.pv-before');
  const on = view.split && mode === 'final';
  if (!on) {
    if (old) old.remove();
    pv.classList.remove('split');
    return;
  }
  const url = beforeUrl(view, () => { if (view.split) paintStage(view); });
  pv.classList.add('split');
  if (!url) {
    if (old) old.remove();
    return;
  }
  if (old && old.dataset.src === url) return;
  const img = h('img', { class: 'pv-before', alt: '', src: url, draggable: 'false' });
  img.dataset.src = url;
  if (old) old.replaceWith(img);
  else pv.append(img);
}

function paintGpu(view, area, cv, mode) {
  let pv = cv.parentElement;
  if (!pv || pv.parentElement !== area || area.childElementCount !== 1) {
    pv = h('div', { class: 'pv' });
    pv.append(cv);
    area.replaceChildren(pv);
    view.box = null;
  }
  if (mode !== 'uncropped') {
    for (const n of pv.querySelectorAll('.crop-box, .crop-size')) n.remove();
    view.box = null;
    return;
  }
  // 裁剪页：尺寸和服务器出图时 <img> 的 naturalWidth/Height 一样，用全尺寸（已转正、已按 90° 旋转）
  const prev = view.canvas;
  const next = view.gpuFull;
  view.canvas = next;
  if (!view.box || !pv.contains(view.box)) pv.append(cropBox(view, pv));
  const old = pv.querySelector('.crop-size');
  const text = view.crop && (view.crop.frame || view.canvas) ? h('div', { class: 'crop-size' }, pixelsText(view)) : null;
  if (old && text) old.replaceWith(text);
  else if (text) pv.append(text);
  if (!prev || prev.w !== next.w || prev.h !== next.h) view.redrawSide?.();
}

// ---- 裁切框 -------------------------------------------------------------------

function boxStyle(r) {
  return { left: pct(r.x), top: pct(r.y), width: pct(r.w), height: pct(r.h) };
}

function cropBox(view, pv) {
  const r = view.crop ? view.crop.rect : { x: 0, y: 0, w: 1, h: 1 };
  const box = h('div', {
    class: 'crop-box',
    'data-fade': String(guideFade()),
    'data-moving': 'false',
    style: boxStyle(r),
  });
  box.append(guidesSvg(r, view.canvas));
  for (const c of ['tl', 'tr', 'bl', 'br']) box.append(h('i', { class: `h ${c}`, 'data-h': c }));
  box.addEventListener('pointerdown', (e) => onDown(e, view, box, pv));
  view.box = box;
  return box;
}

export function paintBox(view) {
  const box = view.box;
  if (!box || !view.crop) return;
  const r = view.crop.rect;
  Object.assign(box.style, boxStyle(r));
  box.dataset.fade = String(guideFade());
  const old = box.querySelector('svg.guides');
  if (old) old.replaceWith(guidesSvg(r, view.canvas));
}

// 拖动框：移动，或拖角缩放（比例锁定时保持比例）。拖的时候只改框，松手才保存。
function onDown(e, view, box, pv) {
  if (e.button !== 0 || !view.canvas) return;
  e.preventDefault();
  const corner = e.target && e.target.dataset ? e.target.dataset.h : undefined;
  box.setPointerCapture(e.pointerId);
  beginChange(view);
  box.dataset.moving = 'true';
  const r0 = { ...view.crop.rect };
  const rect = pv.getBoundingClientRect();
  const lock = corner ? lockRatio(view, view.canvas) : null;

  const move = (ev) => {
    let next;
    if (!corner) {
      next = moveRect(r0, (ev.clientX - e.clientX) / rect.width, (ev.clientY - e.clientY) / rect.height);
    } else {
      const p = { x: (ev.clientX - rect.left) / rect.width, y: (ev.clientY - rect.top) / rect.height };
      next = resizeRect(r0, corner, p, view.canvas, lock);
    }
    // 越界就收回来（先挪，挪不进去再缩小）
    view.crop.rect = fitInto(next, validRect(view, view.canvas)).rect;
    paintBox(view);
    view.onRectDrag?.();
  };
  const up = () => {
    box.removeEventListener('pointermove', move);
    box.dataset.moving = 'false';
    saveRect(view).then(() => endChange(view));
  };
  box.addEventListener('pointermove', move);
  box.addEventListener('pointerup', up, { once: true });
  box.addEventListener('pointercancel', up, { once: true });
}

export function saveRect(view) {
  return saveCrop(view, cropBody(view, { preset: view.crop.preset, rect: rectArray(view.crop.rect) }, orientationNow(view)));
}
