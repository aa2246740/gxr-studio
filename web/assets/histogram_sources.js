// 直方图的两个数据来源（算法在 histogram_math.js，画法在 histogram_draw.js）：
//   显卡路径：让引擎在成片纹理上数（compute 通道，异步读回），拿到的就是屏幕上这一帧的数据。
//   服务器出图的退路：把服务器出的那张 JPEG 解码、降到长边 384 再数；溢出提示画在叠在照片上的 canvas 里。
//   解码（createImageBitmap）在浏览器的解码线程里做，数 10 万个点不到一毫秒，不另开 worker。
// 成片里的边框和时间戳不算：服务器图带着它们时，另要一张没有它们的小图来数（cleanBlob）。
import { apiBlob } from './api.js';
import { countRGBA, markClipping, pixelRegion, summarize } from './histogram_math.js';
import { draftBody } from './preview.js';

const SAMPLE_EDGE = 384; // 数直方图的小图长边
const OVERLAY_EDGE = 800; // 溢出提示的小图长边
const CLEAN_EDGE = 800; // 带边框时另要的无边框小图长边

let sampleCanvas = null;
let overlayScratch = null;

function context2d(canvas) {
  return canvas.getContext('2d', { willReadFrequently: true });
}

// ---- 显卡 ----------------------------------------------------------------------

// region：归一化 {x, y, w, h} 或 null。返回 summarize() 的结果；引擎正忙或还没有成片纹理时返回 null（下一轮再来）。
export async function gpuHistogram(engine, region) {
  const d = await engine.histogram(region ? [region.x, region.y, region.w, region.h] : null);
  if (!d) return null;
  return { ...summarize({ counts: d.counts, samples: d.samples, stride: d.stride, rect: d.rect }, 'gpu'), version: d.version };
}

// ---- 服务器图 ------------------------------------------------------------------

export async function decodeBlob(blob) {
  // 不做色彩转换：数的是服务器写进 JPEG 的码值，和导出的一致（显卡路径读相机 JPG 时也是这样）
  return createImageBitmap(blob, { colorSpaceConversion: 'none', premultiplyAlpha: 'none' });
}

// 屏幕上那张 <img>（已经解码好）。不去 fetch 它的 blob: 链接——页面的内容安全策略不放行 blob: 的 fetch，只放行 <img>。
export function decodeImage(img) {
  return createImageBitmap(img, { colorSpaceConversion: 'none', premultiplyAlpha: 'none' });
}

// 带边框或时间戳的成片：另要一张没有它们的小图（同样的裁剪、拉直、颜色）。服务器上颜色结果有缓存，这一张很便宜。
export async function cleanBlob(view, stage, signal) {
  const body = { ...draftBody(view, stage), frame: { style: 'none' }, stamp: { enabled: false } };
  const { blob } = await apiBlob(`/api/photos/${view.photo.id}/render?stage=${stage}&max_edge=${CLEAN_EDGE}`, { body, signal });
  return blob;
}

// 数 bitmap 里 region 的部分（null = 整幅）
export function bitmapHistogram(bitmap, region) {
  const [x0, y0, x1, y1] = pixelRegion(bitmap.width, bitmap.height, region);
  const sw = x1 - x0;
  const sh = y1 - y0;
  const k = Math.min(1, SAMPLE_EDGE / Math.max(sw, sh));
  const w = Math.max(1, Math.round(sw * k));
  const h = Math.max(1, Math.round(sh * k));
  sampleCanvas ??= document.createElement('canvas');
  sampleCanvas.width = w;
  sampleCanvas.height = h;
  const ctx = context2d(sampleCanvas);
  ctx.imageSmoothingQuality = 'medium';
  ctx.drawImage(bitmap, x0, y0, sw, sh, 0, 0, w, h);
  const data = ctx.getImageData(0, 0, w, h).data;
  return summarize(countRGBA(data, w, h, { stride: 1 }), 'server');
}

// 把 bitmap 里暗部压死 / 高光溢出的点画到 target（叠在照片上的 canvas）。flags：{ shadow, highlight }。
export function paintOverlay(target, bitmap, flags) {
  const k = Math.min(1, OVERLAY_EDGE / Math.max(bitmap.width, bitmap.height));
  const w = Math.max(1, Math.round(bitmap.width * k));
  const h = Math.max(1, Math.round(bitmap.height * k));
  overlayScratch ??= document.createElement('canvas');
  overlayScratch.width = w;
  overlayScratch.height = h;
  const ctx = context2d(overlayScratch);
  ctx.imageSmoothingQuality = 'medium';
  ctx.drawImage(bitmap, 0, 0, w, h);
  const src = ctx.getImageData(0, 0, w, h);
  const out = new ImageData(w, h);
  const n = markClipping(src.data, out.data, flags);
  target.width = w;
  target.height = h;
  target.getContext('2d').putImageData(out, 0, 0);
  return n;
}

export function clearOverlay(target) {
  target.getContext('2d').clearRect(0, 0, target.width, target.height);
}
