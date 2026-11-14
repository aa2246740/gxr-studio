// 直方图的形状，照设计稿 ui.py 的 hist_svg：红绿蓝各一层、亮度一条线，平方根纵轴，
// 两端的档位常常是一根尖刺，所以纵轴的上限只看内部档位（2..253）。两端另用小三角提示裁切。
// 显卡给的 counts：0..255 红、256..511 绿、512..767 蓝、768..1023 亮度，1024 暗部压死、1025 高光溢出。

export interface HistShape {
  /** 每个通道 256 个高度，0..1。 */
  r: Float64Array;
  g: Float64Array;
  b: Float64Array;
  l: Float64Array;
  /** 压死 / 溢出的像素占比到 0.5% 就点亮那一端的三角。 */
  low: boolean;
  high: boolean;
}

export const CLIP_SHARE = 0.005;

export function histShape(counts: Uint32Array, samples: number): HistShape {
  const channel = (index: number) => counts.subarray(index * 256, index * 256 + 256);
  const channels = [channel(0), channel(1), channel(2), channel(3)];
  let innerMax = 0;
  for (const values of channels) for (let i = 2; i < 254; i++) if (values[i] > innerMax) innerMax = values[i];
  const shape = (values: Uint32Array) => {
    const out = new Float64Array(256);
    if (innerMax <= 0) return out;
    for (let i = 0; i < 256; i++) out[i] = Math.min(1, Math.sqrt(Math.min(values[i], innerMax) / innerMax));
    return out;
  };
  const total = samples > 0 ? samples : 0;
  return {
    r: shape(channels[0]),
    g: shape(channels[1]),
    b: shape(channels[2]),
    l: shape(channels[3]),
    low: total > 0 && (counts[1024] ?? 0) / total >= CLIP_SHARE,
    high: total > 0 && (counts[1025] ?? 0) / total >= CLIP_SHARE,
  };
}

/** 第 i 档在 w × h 画布上的点。和稿上一样：底部留 3，顶部留 5。 */
export function histPoint(i: number, value: number, w: number, h: number): [number, number] {
  return [(i / 255) * w, h - 3 - value * (h - 8)];
}
