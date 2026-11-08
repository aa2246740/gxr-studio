// 大图上的边框和时间戳：浏览器自己画，不请服务器。
// 照片（显卡画布，或服务器出的没有边框的图）放在边框里的位置，边框上的字、线、标志和日期戳画在一块叠加的 canvas 上。
// 版面（finish_layout.js）和导出同一套算式，数字是导出时的像素；这里只管换算成屏幕上的大小。

import { frameLayout, stampLayout, stampText, momentOf, needsCjk } from './finish_layout.js';
import { fontReady, fontsNeeded, loadFont, metrics, tableReady } from './finish_fonts.js';
import { drawDecor } from './finish_draw.js';
import { photoSize } from './output_photo_size.js';

const rgb = (c) => `rgb(${c[0]}, ${c[1]}, ${c[2]})`;

// 一组边框 + 时间戳设置（null = 不要）在导出时（已裁切）照片 ew×eh 像素上的版面。什么都不用画返回 null。
// onFonts：用到的字体（文件或版面表）还没下载好时，下载完调用一次（那时再画一遍）。版面要用字体表：表没到就先不画（返回 null）。
export function makeDeco(ed, frameSpec, stampSpec, ew, eh, onFonts, cropPreset = null) {
  if (!frameSpec && !stampSpec) return null;
  const meta = (ed.meta && ed.meta.frame) || {};
  const when = momentOf(ed.meta && ed.meta.captured_at);
  const text = stampSpec ? stampText(stampSpec, when) : '';
  const needed = fontsNeeded(frameSpec, text ? stampSpec : null, needsCjk(text));
  for (const id of needed) if (!tableReady(id) || !fontReady(id)) loadFont(id).then(onFonts, () => {});
  if (needed.some((id) => !tableReady(id))) return null;
  const frame = frameSpec ? frameLayout(ew, eh, frameSpec, meta, metrics, cropPreset) : null;
  const stamp = stampSpec ? stampLayout(ew, eh, stampSpec, text, metrics) : null;
  if (!frame && !stamp) return null;
  const key = JSON.stringify([frameSpec, stampSpec, meta, when, ew, eh, cropPreset, needed.map(readiness)]);
  return { key, frame, stamp, ew, eh, width: frame ? frame.width : ew, height: frame ? frame.height : eh };
}

// 这张照片现在要画的边框和时间戳（取自 view.ed）。ew、eh：导出时（已裁切）照片的像素宽高。
export function decorOf(view, ew, eh) {
  const ed = view.ed;
  if (!ed || !ed.v2 || !(ew > 0) || !(eh > 0)) return null;
  const frameSpec = ed.frame && ed.frame.style && ed.frame.style !== 'none' ? ed.frame : null;
  const stampSpec = ed.stamp && ed.stamp.enabled ? ed.stamp : null;
  if (!frameSpec && !stampSpec) return null;
  const exact = photoSize(view);
  if (exact.value) [ew, eh] = exact.value.photo;
  else if (!exact.stageListening) {
    exact.stageListening = true;
    exact.promise.then(() => { if (!view.disposed) view.onChange?.(); }, () => {});
  }
  const known = view.decor;
  const preset = view.crop?.preset;
  const mine = JSON.stringify([frameSpec, stampSpec, ed.meta, ew, eh, preset]);
  if (known && known.mine === mine && known.fonts === fontsState(frameSpec, stampSpec, ed)) return known.deco;
  const deco = makeDeco(ed, frameSpec, stampSpec, ew, eh, () => { if (!view.disposed) view.onChange?.(); }, preset);
  view.decor = { mine, fonts: fontsState(frameSpec, stampSpec, ed), deco };
  return deco;
}

// 字体（版面表、字体文件）下载好了几个：下载完要重新算
const readiness = (id) => `${+tableReady(id)}${+fontReady(id)}`;
function fontsState(frameSpec, stampSpec, ed) {
  const text = stampSpec ? stampText(stampSpec, momentOf(ed.meta && ed.meta.captured_at)) : '';
  return fontsNeeded(frameSpec, text ? stampSpec : null, needsCjk(text)).map(readiness).join();
}

// 显卡画布：整张成片（照片 + 边框）要放进 maxW×maxH，照片能用多大。没有装饰返回 null（照旧按 maxW、maxH 放）。
export function fitPhoto(view, mode, maxW, maxH) {
  const size = mode === 'final' && view.gpuExport;
  const deco = size ? decorOf(view, size.w, size.h) : null;
  if (!deco) return null;
  const s = Math.min(maxW / deco.width, maxH / deco.height);
  return { deco, maxW: deco.ew * s, maxH: deco.eh * s };
}

// 服务器出的图：没有边框的照片，自然大小 nw×nh（导出时是 ed.scale 倍）。返回照片在屏幕上的大小，和要画的装饰。
export function fitImage(view, mode, img, maxW, maxH, natural) {
  const ed = view.ed;
  if (mode !== 'final' || !ed || !img.naturalWidth) return null;
  const k = ed.scale || 1;
  const deco = decorOf(view, Math.round(img.naturalWidth * k), Math.round(img.naturalHeight * k));
  if (!deco) return null;
  const dpr = globalThis.devicePixelRatio || 1;
  // 和没有边框时一样：不放大到超过图自己的大小；100% 时一个图像像素对一个屏幕像素
  const s = natural ? img.naturalWidth / dpr / deco.ew : Math.min(maxW / deco.width, maxH / deco.height, img.naturalWidth / deco.ew);
  return { deco, cssW: deco.ew * s, cssH: deco.eh * s };
}

function overlay(pv) {
  for (const c of pv.children) if (c.classList && c.classList.contains('pv-deco')) return c;
  const canvas = document.createElement('canvas');
  canvas.className = 'pv-deco';
  canvas.setAttribute('aria-hidden', 'true');
  pv.append(canvas);
  return canvas;
}

// 边框版面写在 .pv 和照片元素上的行内样式：去掉就恢复成没有边框时的样子
const PV_STYLES = ['width', 'height', 'background', '--fx', '--fy', '--fw', '--fh'];
const MEDIA_STYLES = ['position', 'left', 'top'];

// 去掉装饰：恢复成没有边框时的样子（照片自己的大小，没有叠加层）。
// 不看这个 .pv 有没有装饰的记号就直接去：显卡画布是整页唯一的一块，换照片时它被搬进一个新的 .pv，
// 上一张边框留在它身上的 position/left/top 要在这里去掉；只看新 .pv 干不干净的话，画布留着绝对定位，.pv 缩成 0×0，画面一片空白。
export function clearDecor(pv, media) {
  for (const c of [...pv.children]) if (c.classList && c.classList.contains('pv-deco')) c.remove();
  if (pv.classList.contains('framed')) pv.classList.remove('framed');
  delete pv.dataset.decor;
  for (const p of PV_STYLES) pv.style.removeProperty(p);
  if (media) for (const p of MEDIA_STYLES) media.style.removeProperty(p);
}

// 装饰画到 pv 上。media：照片元素（显卡画布或 <img>），cssW×cssH 是它在屏幕上的大小（CSS 像素）。
export function applyDecor(view, pv, media, deco, cssW, cssH) {
  const s = cssW / deco.ew;
  const frame = deco.frame;
  const W = frame ? Math.round(frame.width * s) : Math.round(cssW);
  const H = frame ? Math.round(frame.height * s) : Math.round(cssH);
  pv.dataset.decor = '1';
  if (frame) {
    const fx = Math.round(frame.photo[0] * s);
    const fy = Math.round(frame.photo[1] * s);
    pv.classList.add('framed');
    Object.assign(pv.style, { width: `${W}px`, height: `${H}px`, background: rgb(frame.bg) });
    pv.style.setProperty('--fx', `${fx}px`);
    pv.style.setProperty('--fy', `${fy}px`);
    pv.style.setProperty('--fw', `${Math.round(cssW)}px`);
    pv.style.setProperty('--fh', `${Math.round(cssH)}px`);
    Object.assign(media.style, { position: 'absolute', left: `${fx}px`, top: `${fy}px` });
  } else {
    pv.classList.remove('framed');
    for (const p of PV_STYLES) pv.style.removeProperty(p);
    for (const p of MEDIA_STYLES) media.style.removeProperty(p);
  }
  const canvas = overlay(pv);
  const dpr = globalThis.devicePixelRatio || 1;
  const key = `${deco.key}|${W}x${H}|${dpr}|${rgb(frame ? frame.bg : [0, 0, 0])}`;
  if (canvas.dataset.key === key) return;
  canvas.dataset.key = key;
  canvas.width = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  // 标志图片还没下载好：好了再画一遍（让下一次 applyDecor 不被缓存挡住）
  drawDecor(ctx, deco, s * dpr, { onLogo: () => { delete canvas.dataset.key; if (!view.disposed) view.onChange?.(); } });
}
