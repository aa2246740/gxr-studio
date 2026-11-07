// 直方图的算法：怎么取样、怎么分档、怎么算"暗部压死 / 高光溢出"。没有 DOM，也不碰显卡。
// 显卡路径（frontend/src/shaders/hist.wgsl）和服务器出图的退路（histogram_sources.js）都按这里的规则数，
// tests/test_histogram.py 用 numpy 逐档、逐个计数核对。改规则要三处一起改（这里、hist.wgsl、engine.ts 里的常量），
// 测试会拦住不一致。

export const LEVELS = 256;
// 一份计数的排列：红、绿、蓝、亮度各 256 档，后面跟两个计数
export const AT = { r: 0, g: 256, b: 512, y: 768, crushed: 1024, blown: 1025 };
export const COUNT_SIZE = 1026;

// 溢出的判定（对导出时的 8 位码值）：三个颜色都 ≤ SHADOW_MAX = 暗部压死；三个颜色都 ≥ HIGHLIGHT_MIN = 高光溢出。
// 留 2 级余量：JPEG 压缩和取整会让纯黑纯白差一两级，肉眼分不出。
// 只有一个颜色到顶（比如纯蓝的天）不算：它在直方图那个颜色的右边缘能看到，照片上没有"白到没层次"的地方。
export const SHADOW_MAX = 2;
export const HIGHLIGHT_MIN = 253;
// 溢出的点占取样点的比例达到这个数，三角才亮起（1/200）。更少的点多半是几粒噪点或一颗高光，不值得提醒。
export const WARN_SHARE = 0.005;
// 一次最多数这么多个点（512×512）。再多对形状没有帮助，只会多花时间。
export const MAX_SAMPLES = 262144;

// 溢出提示在画面上的颜色（8 位）：蓝 = 暗部压死，红 = 高光溢出。显卡着色器 present.wgsl 里是同一组数。
export const SHADOW_RGB = [40, 110, 255];
export const HIGHLIGHT_RGB = [255, 50, 40];

// 亮度（Rec.709 权重，整数运算，四舍五入）：和 numpy 的 (2126r + 7152g + 722b + 5000) // 10000 逐个相同
export function lumaOf(r, g, b) {
  return Math.floor((2126 * r + 7152 * g + 722 * b + 5000) / 10000);
}

export const isCrushed = (r, g, b) => Math.max(r, g, b) <= SHADOW_MAX;
export const isBlown = (r, g, b) => Math.min(r, g, b) >= HIGHLIGHT_MIN;

// 取样间隔：横竖都每隔 s 个像素取一个，取到的点不超过 maxSamples
export function sampleStride(width, height, maxSamples = MAX_SAMPLES) {
  let s = Math.max(1, Math.floor(Math.sqrt((width * height) / maxSamples)));
  while (Math.ceil(width / s) * Math.ceil(height / s) > maxSamples) s += 1;
  return s;
}

// 归一化的矩形 {x, y, w, h}（0–1）→ 像素 [x0, y0, x1, y1]（x1、y1 不含）。null = 整幅。至少留一个像素。
export function pixelRegion(width, height, region) {
  if (!region) return [0, 0, width, height];
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const x0 = clamp(Math.round(region.x * width), 0, width - 1);
  const y0 = clamp(Math.round(region.y * height), 0, height - 1);
  const x1 = clamp(Math.round((region.x + region.w) * width), x0 + 1, width);
  const y1 = clamp(Math.round((region.y + region.h) * height), y0 + 1, height);
  return [x0, y0, x1, y1];
}

// 数一遍 RGBA 字节（canvas 的 ImageData 或测试数据）。rect 是像素矩形，stride 是取样间隔。
export function countRGBA(rgba, width, height, { rect = null, stride = null } = {}) {
  const [x0, y0, x1, y1] = rect || [0, 0, width, height];
  const step = stride || sampleStride(x1 - x0, y1 - y0);
  const out = new Uint32Array(COUNT_SIZE);
  let samples = 0;
  for (let y = y0; y < y1; y += step) {
    for (let x = x0; x < x1; x += step) {
      const i = (y * width + x) * 4;
      const r = rgba[i];
      const g = rgba[i + 1];
      const b = rgba[i + 2];
      out[AT.r + r] += 1;
      out[AT.g + g] += 1;
      out[AT.b + b] += 1;
      out[AT.y + lumaOf(r, g, b)] += 1;
      if (isCrushed(r, g, b)) out[AT.crushed] += 1;
      if (isBlown(r, g, b)) out[AT.blown] += 1;
      samples += 1;
    }
  }
  return { counts: out, samples, stride: step, rect: [x0, y0, x1, y1] };
}

// 一份计数 → 界面要用的东西：四条曲线、两个溢出比例、三角该不该亮、亮度平均档
export function summarize({ counts, samples, stride = 1, rect = null }, source = 'cpu') {
  const share = (n) => (samples ? n / samples : 0);
  const shadowShare = share(counts[AT.crushed]);
  const highlightShare = share(counts[AT.blown]);
  let sum = 0;
  for (let i = 0; i < LEVELS; i++) sum += i * counts[AT.y + i];
  return {
    source,
    samples,
    stride,
    rect,
    r: counts.subarray(AT.r, AT.r + LEVELS),
    g: counts.subarray(AT.g, AT.g + LEVELS),
    b: counts.subarray(AT.b, AT.b + LEVELS),
    y: counts.subarray(AT.y, AT.y + LEVELS),
    crushed: counts[AT.crushed],
    blown: counts[AT.blown],
    shadowShare,
    highlightShare,
    shadowWarn: shadowShare >= WARN_SHARE,
    highlightWarn: highlightShare >= WARN_SHARE,
    mean: samples ? sum / samples : 0,
  };
}

// 在 RGBA 数据上标出溢出：蓝、红、其余全透明。给服务器出图的退路画叠加层用（显卡路径在着色器里做同样的判断）。
export function markClipping(rgba, out, { shadow = true, highlight = true } = {}) {
  let crushed = 0;
  let blown = 0;
  for (let i = 0; i < rgba.length; i += 4) {
    const r = rgba[i];
    const g = rgba[i + 1];
    const b = rgba[i + 2];
    let colour = null;
    if (shadow && isCrushed(r, g, b)) colour = SHADOW_RGB;
    else if (highlight && isBlown(r, g, b)) colour = HIGHLIGHT_RGB;
    if (colour) {
      out[i] = colour[0];
      out[i + 1] = colour[1];
      out[i + 2] = colour[2];
      out[i + 3] = 255;
      if (colour === SHADOW_RGB) crushed += 1;
      else blown += 1;
    } else {
      out[i + 3] = 0;
    }
  }
  return { crushed, blown };
}

// 百分比的写法：不到 0.1% 写"0%"，不到 10% 留一位小数，其余取整
export function percentText(share) {
  const p = share * 100;
  if (p < 0.05) return '0%';
  return `${p < 10 ? p.toFixed(1) : Math.round(p)}%`;
}
