// 透视（垂直、水平）和畸变在显卡预览里的浏览器一侧（gpu_stage.js 用）。
// 数学在引擎模块里（frontend/src/warp.ts，finish/geometry.py 的 float64 版）；这里只管：
//   把几何规整成引擎认的六个键；
//   裁剪页的有效区——拖滑块时每一步都是新的几何，不能等服务器，所以在浏览器里算（和服务器的 normalized_valid_rect 同一个算法，同一组取样点）；
//   全尺寸画布（原图像素）和成片在导出时的大小。
// 成片的裁切框（final 阶段）仍由服务器给：松手之后才会用到，一个几何一次请求，不在拖动的路上。

export function normalGeometry(g) {
  const s = g || {};
  return {
    straighten: s.straighten || 0,
    vertical: s.vertical || 0,
    horizontal: s.horizontal || 0,
    distortion: s.distortion || 0,
    rotate90: s.rotate90 || 0,
    flip: !!s.flip,
  };
}

export function expands(g) {
  return !!(g.vertical || g.horizontal || g.distortion);
}

const memo = new Map();

// 裁剪页的有效区 [x, y, w, h]（归一化，旋转、翻转之后的画布上）
export function warpValid(api, g, w, h) {
  const key = JSON.stringify([g, w, h]);
  let hit = memo.get(key);
  if (!hit) {
    hit = api.validRect(g, w, h);
    memo.set(key, hit);
    if (memo.size > 32) memo.delete(memo.keys().next().value);
  }
  return hit;
}

// 全尺寸画布（原图像素）：透视 / 畸变之后按 90° 次数交换宽高。fw、fh 是转正后的全尺寸。
export function warpCanvas(api, g, fw, fh) {
  return api.rotatedSize(api.canvasOf(g, fw, fh), ((g.rotate90 % 4) + 4) % 4);
}

// 成片在导出时的像素（边框和时间戳的版面按它算）。
// 没有透视、畸变：显卡上的成片按整体倍数 grow 放大。
// 透视 / 畸变：全尺寸的裁切框（/gpu/box 在全尺寸画布上算，和导出同一个 final_box）到了就用它的宽高，一个像素不差；
// 它到之前先按「成片 ÷ 颜色层画布 × 全尺寸画布」估一个（差几个像素，框到了就会换掉）。
export function exportSize(shown, grow, g, exact, fullSize, layerCanvas) {
  if (!shown) return null;
  if (!expands(g)) return { w: Math.round(shown.w * grow), h: Math.round(shown.h * grow) };
  if (exact && exact.box) return { w: exact.box[2], h: exact.box[3] };
  if (!fullSize || !layerCanvas) return { w: Math.round(shown.w * grow), h: Math.round(shown.h * grow) };
  return { w: Math.round((shown.w * fullSize.w) / layerCanvas.w), h: Math.round((shown.h * fullSize.h) / layerCanvas.h) };
}
