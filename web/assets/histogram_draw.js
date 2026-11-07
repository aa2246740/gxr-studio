// 直方图的画法：红、绿、蓝三块半透明的填充叠在一起（重叠的地方混成黄、青、品红，三色都有就接近白），
// 亮度画成一条细白线加极淡的底。只管画，不管数（数在 histogram_math.js）。
import { LEVELS } from './histogram_math.js';

// 三种颜色：叠加混色（lighter），和 Lightroom 的直方图读起来一样
const CHANNELS = [['r', '255, 72, 72'], ['g', '64, 214, 96'], ['b', '72, 120, 255']];
const FILL_ALPHA = 0.62;
const GRID = [0.25, 0.5, 0.75];
const TOP_PAD = 3; // 最高的地方留几个像素（CSS 像素）

// 画之前的整理：中间的 254 档做一次 1-2-1 平滑（调过对比度的照片会有梳齿），最左和最右两档（0 和 255）原样留着——
// 它们常常是一根针，正是溢出的样子。返回 [r, g, b, y] 四条平滑后的数。
function smoothed(hist) {
  return [hist.r, hist.g, hist.b, hist.y].map((bins) => {
    const out = new Float32Array(LEVELS);
    out[0] = bins[0];
    out[LEVELS - 1] = bins[LEVELS - 1];
    for (let i = 1; i < LEVELS - 1; i++) out[i] = (bins[i - 1] + 2 * bins[i] + bins[i + 1]) / 4;
    return out;
  });
}

// 高度：开方（让暗处少量的点也看得见），按"中间档里最高的一档"归一化；两端的针封顶在框里
export function heights(curves) {
  let peak = 1;
  for (const c of curves) for (let i = 1; i < LEVELS - 1; i++) if (c[i] > peak) peak = c[i];
  return curves.map((c) => Float32Array.from(c, (v) => Math.min(1, Math.sqrt(v / peak))));
}

function trace(ctx, ys, w, h, dpr) {
  const top = TOP_PAD * dpr;
  ctx.beginPath();
  ctx.moveTo(0, h);
  for (let i = 0; i < LEVELS; i++) ctx.lineTo(((i + 0.5) / LEVELS) * w, h - ys[i] * (h - top));
  ctx.lineTo(w, h);
  ctx.closePath();
}

// hist：histogram_math.js 的 summarize() 结果；没有（还没数出来）时只画底格
export function drawHistogram(canvas, hist) {
  const dpr = globalThis.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(canvas.clientWidth * dpr));
  const h = Math.max(1, Math.round(canvas.clientHeight * dpr));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = 'rgba(255, 255, 255, 0.07)';
  for (const g of GRID) ctx.fillRect(Math.round(g * w), 0, Math.max(1, Math.round(dpr * 0.5)), h);
  if (!hist || !hist.samples) return;
  const [r, g, b, y] = heights(smoothed(hist));
  ctx.globalCompositeOperation = 'lighter';
  [r, g, b].forEach((ys, k) => {
    ctx.fillStyle = `rgba(${CHANNELS[k][1]}, ${FILL_ALPHA})`;
    trace(ctx, ys, w, h, dpr);
    ctx.fill();
  });
  ctx.globalCompositeOperation = 'source-over';
  trace(ctx, y, w, h, dpr);
  ctx.fillStyle = 'rgba(255, 255, 255, 0.08)';
  ctx.fill();
  ctx.lineWidth = Math.max(1, dpr * 1.1);
  ctx.lineJoin = 'round';
  ctx.strokeStyle = 'rgba(236, 236, 236, 0.9)';
  ctx.beginPath();
  for (let i = 0; i < LEVELS; i++) {
    const x = ((i + 0.5) / LEVELS) * w;
    const yy = h - y[i] * (h - TOP_PAD * dpr);
    if (i) ctx.lineTo(x, yy);
    else ctx.moveTo(x, yy);
  }
  ctx.stroke();
}
