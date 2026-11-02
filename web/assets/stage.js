// 大图的画面区。三种状态（DESIGN 1.3）：
//   成片预览：stage=final，大图默认是这个（配方、调整、拉直、裁切、边框、时间戳都在里面）。
//   裁切中：stage=uncropped，只有「裁切」标签时才有，画上裁切框。
//   原图：按住空格，照片本身（没有任何编辑）。
// 画面是后端渲染好的图（POST /render 的返回），前端不再自己画边框或时间戳。

import { h } from './dom.js';
import { beginChange, endChange, saveCrop, validRect, originalUrl, originalFallbackUrl, renderProblem, geometryOf } from './preview.js';
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
  const maxH = Math.max(120, area.clientHeight - 64);
  // 显卡预览接手了这个画面（gpu_stage.js）：画布只建一次，这里只调尺寸和叠加的裁切框
  const cv = ed && view.gpuActive && view.gpuActive === mode ? gpuCanvas(view, maxW, maxH) : null;
  area.classList.toggle('gpu', !!cv);
  if (cv) {
    paintGpu(view, area, cv, mode);
    return;
  }
  const children = [];

  let src = null;
  if (mode === 'orig') src = originalUrl(view);
  else if (ed) src = ed.urls[mode];

  if (!ed && mode !== 'orig') children.push(note('正在读取…'));
  else if (!src) children.push(note('正在生成预览…'));

  if (src) {
    // 不做淡入：淡入的那几百毫秒里整张画面（连白边）都是半透明的，看起来像蒙了一层灰
    const pv = h('div', { class: 'pv' });
    const img = h('img', {
      class: 'pv-img',
      src,
      alt: view.photo.name,
      draggable: 'false',
      style: { maxWidth: `${maxW}px`, maxHeight: `${maxH}px` },
    });
    // 旧版后端的裁切画面不带拉直：这里用浏览器旋转做个近似（只在裁切中，松手后由后端算准）
    if (mode === 'uncropped' && ed && !ed.v2) {
      const s = geometryOf(view).straighten || 0;
      if (s) img.style.transform = `rotate(${s}deg)`;
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

  if (mode !== 'orig') {
    const problem = renderProblem(view);
    if (problem) children.push(note(problem, 'warn-text'));
  }
  area.replaceChildren(...children);
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
