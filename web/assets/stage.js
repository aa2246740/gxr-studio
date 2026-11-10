// 大图的画面区。三种状态（DESIGN 1.3）：
//   编辑后：stage=final，大图默认是这个（配方、调整、拉直、裁剪、边框、时间戳都在里面）。
//   裁剪中：stage=uncropped，只有「裁剪」标签时才有，画上裁剪框。
//   原图：按住空格或按 \\，照片本身（没有任何编辑）。
// 另外两个视图选项：左右对比（Y，左半边是没有颜色编辑的同一画面）和 适合 / 100%（Z）。
// 画面是后端渲染好的图（POST /render 的返回）或显卡预览的画布，前端不再自己画边框或时间戳。

import { h } from './dom.js';
import { beginChange, endChange, saveCrop, originalUrl, originalFallbackUrl, renderProblem, geometryOf, beforeUrl } from './preview.js';
import { cropBody, guidesSvg, guideFade, lockRatio, orientationNow, rectArray, pixelsText, fitCrop } from './crop.js';
import { moveRect, resizeRect } from './geom.js';
import { gpuCanvas } from './gpu_stage.js';
import { fitPhoto, fitImage, applyDecor, clearDecor } from './stage_finish.js';
import { refreshFrameSource } from './finish_source.js';

const pct = (v) => `${(v * 100).toFixed(4)}%`;

// 边框和时间戳（stage_finish.js）：显卡画布按「整张成片放得下」的大小放，再把边框和日期戳叠上去
function decorGpu(view, pv, cv, fit) {
  refreshFrameSource(view);
  if (!fit) clearDecor(pv, cv);
  else applyDecor(view, pv, cv, fit.deco, parseFloat(cv.style.width), parseFloat(cv.style.height));
}

// 服务器出的图是没有边框的照片：边框和日期戳同样叠上去。没有装饰时恢复成图自己的大小。
function decorImage(view, pv, img, mode, maxW, maxH, natural) {
  if(mode==='final') refreshFrameSource(view,{media:img});
  const fit = fitImage(view, mode, img, maxW, maxH, natural);
  if (fit) {
    Object.assign(img.style, { maxWidth: 'none', maxHeight: 'none', width: `${fit.cssW}px`, height: `${fit.cssH}px` });
    applyDecor(view, pv, img, fit.deco, fit.cssW, fit.cssH);
    return;
  }
  const was = pv.dataset.decor === '1';
  clearDecor(pv, img);
  if (!was) return;
  img.style.removeProperty('height');
  if (natural) img.style.width = `${Math.round(img.naturalWidth / (globalThis.devicePixelRatio || 1))}px`;
  else Object.assign(img.style, { width: '', maxWidth: `${maxW}px`, maxHeight: `${maxH}px` });
}

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
  const gpuMode = !!ed && !!view.gpuActive && view.gpuActive === mode;
  const fit = gpuMode ? fitPhoto(view, mode, maxW, maxH) : null;
  const cv = gpuMode ? gpuCanvas(view, fit ? fit.maxW : maxW, fit ? fit.maxH : maxH, { natural }) : null;
  area.classList.toggle('gpu', !!cv);
  if (cv) {
    paintGpu(view, area, cv, mode);
    paintSplit(view, area.firstChild, mode);
    decorGpu(view, area.firstChild, cv, fit);
    return;
  }
  let src = null;
  if (mode === 'orig') src = originalUrl(view);
  else if (ed) src = ed.urls[mode];

  // 同一张图、同一种模式：只调尺寸（窗口变了、面板收起），不重建 <img>，画面不闪
  const cur = childWith(area, 'pv');
  const curImg = cur && cur.querySelector('.pv-img');
  // 裁切页也一样：图没换、框已经在页面上，就只更新框（点比例、换参考线都不用重建整张图）
  const cropBoxKept = mode !== 'uncropped' || (!!view.box && cur && cur.contains(view.box));
  if (src && curImg && cur.dataset.mode === mode && cur.dataset.src === src && cur.dataset.natural === String(natural) && cropBoxKept) {
    if (!natural) Object.assign(curImg.style, { maxWidth: `${maxW}px`, maxHeight: `${maxH}px` });
    if (mode === 'uncropped') {
      if (ed && !ed.v2) curImg.style.transform = geometryOf(view).straighten ? `rotate(${geometryOf(view).straighten}deg)` : '';
      paintBox(view);
    }
    decorImage(view, cur, curImg, mode, maxW, maxH, natural);
    paintSplit(view, cur, mode);
    paintProblem(view, area, mode);
    return;
  }

  if (!src) {
    // 还没有出图：先放图库里已经加载过的缩略图（浏览器有缓存，和切路由同一帧出来），等大图好了再换
    if (mode !== 'orig' && view.photo.thumb_url) {
      if (!(cur && cur.dataset.placeholder === '1')) area.replaceChildren(placeholder(view, maxW, maxH));
      else sizePlaceholder(cur, maxW, maxH);
    } else if (!cur) {
      area.replaceChildren(note(!ed && mode !== 'orig' ? '正在读取…' : '正在生成预览…'));
    }
    paintProblem(view, area, mode);
    return;
  }

  // 新图解码时旧图仍然可见：边框先在旧图上更新，不等换图。
  if (curImg && cur.dataset.mode === mode) decorImage(view, cur, curImg, mode, maxW, maxH, natural);

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
    decorImage(view, pv, img, mode, maxW, maxH, natural);
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
    if (area.contains(pv) || !area.querySelector('.pv')) area.replaceChildren(note('无法读取这张照片。请重试。', 'warn-text'));
  });
  img.src = src;
  pv.append(img);
  // 裁切的尺寸放在画面右下角，只写数字（COPY 3.1）
  if (mode === 'uncropped' && view.crop && (view.crop.frame || view.canvas)) pv.append(h('div', { class: 'crop-size' }, pixelsText(view)));
  paintSplit(view, pv, mode);

  // 换图：旧画面留着，新图解码好了再换上（交叉淡入，不经过空白）；模式变了（进出裁剪、原图）直接换
  const swap = () => {
    if (view.stageArea !== area) return;
    const keep = childWith(area, 'pv');
    const fade = !!keep && keep !== pv;
    decorImage(view, pv, img, mode, maxW, maxH, natural);
    area.replaceChildren(pv);
    if (fade) {
      const under = keep.querySelector('.pv-img');
      const sameShape = under && under.naturalWidth && img.naturalWidth
        && Math.abs(under.naturalWidth / under.naturalHeight - img.naturalWidth / img.naturalHeight) < 0.01;
      // 带边框的画面不做叠影淡入（叠影是整块铺满的，对不上边框里的照片位置）
      if (sameShape && keep.dataset.mode === mode && !keep.dataset.decor && !pv.dataset.decor) {
        // 旧图垫在下面，新图淡入（下面一直有画面，不会像整张蒙了一层灰）；淡入结束后拿掉旧图
        const ghost = under.cloneNode();
        ghost.className = 'pv-under';
        ghost.removeAttribute('style');
        pv.prepend(ghost);
        img.classList.add('xfade');
        img.addEventListener('animationend', () => ghost.remove(), { once: true });
        setTimeout(() => ghost.remove(), 600);
      }
    }
    paintProblem(view, area, mode);
  };
  const seq = (view.paintSeq = (view.paintSeq || 0) + 1);
  if (cur && typeof img.decode === 'function') {
    img.decode().then(() => { if (view.paintSeq === seq) swap(); }, () => { if (view.paintSeq === seq) swap(); });
  } else {
    swap();
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
  else paintBox(view); // 框已经在：按现在的框（比如后端回来的准框、撤销还原的框）重画
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
  // 画面右下角的「W × H」跟着框走
  const size = box.parentElement && box.parentElement.querySelector('.crop-size');
  if (size) size.textContent = pixelsText(view);
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
    // 越界就收回来（先挪，挪不进去再缩小），范围和后端一样（fitCrop）
    view.crop.rect = fitCrop(view, next).rect;
    if (view.onRectDrag) view.onRectDrag();
    else paintBox(view);
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
