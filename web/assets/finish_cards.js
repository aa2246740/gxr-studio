// 边框标签的样式卡片：浏览器用一张小底图（当前照片、已裁切、没有边框）加版面画出来，不再请服务器给每种样式出一张图。
// 底图优先取舞台上正在显示的画面（显卡读回，或服务器出的图），不另外请求；舞台上还没有时才请求一张小图（长边 480）。
// 底图缓存在 view 上，面板重搭（点一下选项就重搭）时直接用，不重新取。

import { h } from './dom.js';
import { DEFAULT_FRAME, selectFrame } from './finish.js';
import { makeDeco } from './stage_finish.js';
import { drawDecor } from './finish_draw.js';
import { photoSize } from './output_photo_size.js';
import { baseFor, draftKey } from './finish_source.js';
import { primeBlur } from './finish_background.js';

const CARD_EDGE = 240;
const dpr = () => Math.min(2, globalThis.devicePixelRatio || 1);

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
  if (style.id === 'blur_fill') primeBlur(base.image, ew, eh, ed.meta?.frame || {});
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
  view.refreshFrameCards=()=>{if(!view.disposed&&items.some(item=>item.holder.isConnected)) paintCards(view,items);};
  const stale = view.cardBase && (view.cardBase.base || view.cardBase.last);
  const key = draftKey(view);
  const fresh = baseFor(view);
  if (view.cardBase && view.cardBase.base) {
    for (const { style, holder } of items) drawCard(view, style, holder, view.cardBase.base);
    return;
  }
  if (stale) for (const { style, holder } of items) drawCard(view, style, holder, stale);
  fresh.then((base) => {
    if (view.disposed || draftKey(view) !== key) return;
    for (const { style, holder } of items) drawCard(view, style, holder, base);
  }, () => {
    if (!stale) for (const { holder } of items) failed(holder);
  });
}
