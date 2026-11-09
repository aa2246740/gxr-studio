// 边框标签的样式卡片：浏览器用一张小底图（当前照片、已裁切、没有边框）加版面画出来，不再请服务器给每种样式出一张图。
// 底图优先取舞台上正在显示的画面（显卡读回，或服务器出的图），不另外请求；舞台上还没有时才请求一张小图（长边 480）。
// 底图缓存在 view 上，面板重搭（点一下选项就重搭）时直接用，不重新取。

import { h } from './dom.js';
import { apiBlob } from './api.js';
import { state } from './state.js';
import { draftBody } from './preview.js';
import { gpuSnapshot } from './gpu_stage.js';
import { DEFAULT_FRAME, selectFrame } from './finish.js';
import { makeDeco } from './stage_finish.js';
import { drawDecor } from './finish_draw.js';
import { photoSize } from './output_photo_size.js';

const BASE_EDGE = 480;
const CARD_EDGE = 240; // 卡片图的长边（CSS 像素）；高分屏按两倍画

const dpr = () => Math.min(2, globalThis.devicePixelRatio || 1);

function shrink(source, w, h) {
  const scale = Math.min(1, BASE_EDGE / Math.max(w, h));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(w * scale));
  canvas.height = Math.max(1, Math.round(h * scale));
  canvas.getContext('2d').drawImage(source, 0, 0, canvas.width, canvas.height);
  return canvas;
}

// 服务器出的、舞台上正在显示的图（没有边框的照片）当底图。舞台上不是它就返回 null。
function imageBase(view) {
  const img = view.stageArea && view.stageArea.querySelector('.pv-img');
  const ed = view.ed;
  if (!img || !img.complete || !img.naturalWidth || !ed || !ed.urls.final || img.src !== ed.urls.final) return null;
  const k = ed.scale || 1;
  return { image: shrink(img, img.naturalWidth, img.naturalHeight), ew: Math.round(img.naturalWidth * k), eh: Math.round(img.naturalHeight * k) };
}

// 显卡正在画的成片当底图：重画一帧，当场从显卡画布复制（画布就是显示的大小，不把整张成片读回 CPU）。不在显卡上返回 null。
function gpuBase(view) {
  const size = view.gpuExport;
  if (view.gpuActive !== 'final' || !size) return null;
  const image = gpuSnapshot((cv) => (cv.width && cv.height ? shrink(cv, cv.width, cv.height) : null));
  return image ? { image, ew: size.w, eh: size.h } : null;
}

async function requestBase(view) {
  const { blob, headers } = await apiBlob(`/api/photos/${view.photo.id}/render?stage=final&max_edge=${BASE_EDGE}`, { body: draftBody(view, 'final') });
  const bitmap = await createImageBitmap(blob);
  const k = Number(headers.get('X-GXR-Scale')) || 1;
  return { image: bitmap, ew: Math.round(bitmap.width * k), eh: Math.round(bitmap.height * k) };
}

// 当前草稿的指纹：同一份草稿，底图就是同一张
function draftKey(view) {
  return `${view.photo.id}|${state.camdata?.dataVersion ?? ''}|${JSON.stringify(draftBody(view, 'final'))}`;
}

function baseFor(view) {
  const key = draftKey(view);
  const kept = view.cardBase;
  if (kept && kept.key === key) return kept.base ? Promise.resolve(kept.base) : kept.promise;
  const now = imageBase(view) || gpuBase(view);
  if (now) {
    view.cardBase = { key, base: now };
    return Promise.resolve(now);
  }
  const promise = requestBase(view).then((base) => {
    if (view.cardBase && view.cardBase.key === key) view.cardBase = { key, base };
    return base;
  });
  view.cardBase = { key, base: null, promise, last: kept && (kept.base || kept.last) };
  promise.catch(() => { if (view.cardBase && view.cardBase.key === key) view.cardBase = null; });
  return promise;
}

function cardSpec(style, frame) {
  const base = style.defaults ? { ...DEFAULT_FRAME, ...style.defaults } : { ...DEFAULT_FRAME };
  if (style.id === 'none') return null;
  return style.id === frame.style ? { ...base, ...frame } : selectFrame({ ...base, ...frame }, style.id);
}

// 把一张卡片画进 holder（一个 .thumb）。同一个 holder 画第二次就换掉里面的 canvas。
function drawCard(view, style, holder, base) {
  const ed = view.ed;
  const stampSpec = ed.stamp && ed.stamp.enabled ? ed.stamp : null;
  const redo = () => { if (!view.disposed) drawCard(view, style, holder, base); };
  const exact = photoSize(view);
  if (!exact.value) exact.promise.then(redo, () => {});
  const [ew, eh] = exact.value ? exact.value.photo : [base.ew, base.eh];
  const deco = makeDeco(ed, cardSpec(style, ed.frame), stampSpec, ew, eh, redo, view.crop?.preset) || { frame: null, stamp: null, ew, eh, width: ew, height: eh };
  const scale = (CARD_EDGE * dpr()) / Math.max(deco.width, deco.height);
  const canvas = holder.querySelector('canvas') || h('canvas', { 'aria-hidden': 'true' });
  canvas.width = Math.max(1, Math.round(deco.width * scale));
  canvas.height = Math.max(1, Math.round(deco.height * scale));
  // 显示比例取自导出版面，不用小位图取整后的宽高比（宽幅照片尤其明显）。
  canvas.style.aspectRatio = `${deco.width} / ${deco.height}`;
  const ctx = canvas.getContext('2d');
  drawDecor(ctx, deco, scale, { photo: base.image, onLogo: redo });
  holder.classList.remove('skel');
  holder.removeAttribute('aria-busy');
  if (!canvas.parentElement) holder.replaceChildren(canvas);
}

function failed(holder) {
  holder.classList.remove('skel');
  holder.removeAttribute('aria-busy');
  holder.replaceChildren(h('span', { class: 'dim small' }, '没有预览'));
}

// items：[{ style, holder }]。底图一到手 7 张一起画；上一次的底图还在的话先用它画（不闪骨架），新的到了再换。
export function paintCards(view, items) {
  const stale = view.cardBase && (view.cardBase.base || view.cardBase.last);
  const fresh = baseFor(view);
  if (view.cardBase && view.cardBase.base) {
    for (const { style, holder } of items) drawCard(view, style, holder, view.cardBase.base);
    return;
  }
  if (stale) for (const { style, holder } of items) drawCard(view, style, holder, stale);
  fresh.then((base) => {
    if (view.disposed) return;
    for (const { style, holder } of items) drawCard(view, style, holder, base);
  }, () => {
    if (!stale) for (const { holder } of items) failed(holder);
  });
}
