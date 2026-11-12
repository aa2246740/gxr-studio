// 开发壳用的中性烘焙表。桌面的表来自相机数据，手机上还没有，所以这里造一张只做 sRGB 显示的表。
// 开发用中性预览，不是配方。真正的配方表以后由 gxr-color 给出。布局与 gpu/tables.py bake_bytes 相同。

import type { BakeMeta } from '../../../frontend/src/index';

const MATRIX_AT = 12288;
const LOOKUP_AT = 12330;
const GAIN_AT = 12342;

/** BT.601，Cb/Cr 以 0 为中心（着色器的逆变换不再减 128）。行是 Y、Cb、Cr。 */
const BT601 = [
  0.299, 0.587, 0.114,
  -0.168736, -0.331264, 0.5,
  0.5, -0.418688, -0.081312,
];

export const NEUTRAL_BAKE_KEY = 'dev-neutral';

function srgb(linear: number): number {
  if (linear <= 0.0031308) return 12.92 * linear;
  return 1.055 * linear ** (1 / 2.4) - 0.055;
}

/** pre = sRGB(码值/4095) × 255，矩阵 = BT.601，增益网格全是 [1, 0]（不转色相）。 */
export function neutralBake(): { meta: BakeMeta; data: ArrayBuffer } {
  const gain = 2 * 2 * 2;
  const table = new Float32Array(GAIN_AT + gain);
  for (let i = 0; i < 4096; i++) {
    const encoded = srgb(i / 4095) * 255;
    table[i * 3] = encoded;
    table[i * 3 + 1] = encoded;
    table[i * 3 + 2] = encoded;
  }
  table.set(BT601, MATRIX_AT);
  table.set(BT601, LOOKUP_AT);
  for (let cell = 0; cell < 4; cell++) table[GAIN_AT + cell * 2] = 1;
  return {
    meta: { nx: 2, ny: 2, origin: [0, 0], step: 1 },
    data: table.buffer,
  };
}
