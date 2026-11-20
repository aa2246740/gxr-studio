// 只在开发服务器里给模拟器截 LUT 几屏（?shot=lut / ?shot=lut-space）。
// 用脚本现场合成的极简测试 .cube（不落仓库、不带第三方 LUT）。

import type { AppApi } from './model';

function syntheticCube(title: string, rMul: number, gMul: number, bMul: number): File {
  const n = 5;
  const lines = [
    `TITLE "${title}"`,
    '# Input: sRGB',
    `LUT_3D_SIZE ${n}`,
    'DOMAIN_MIN 0.0 0.0 0.0',
    'DOMAIN_MAX 1.0 1.0 1.0',
  ];
  for (let b = 0; b < n; b++) {
    for (let g = 0; g < n; g++) {
      for (let r = 0; r < n; r++) {
        const rv = Math.min(1, Math.max(0, (r / (n - 1)) * rMul));
        const gv = Math.min(1, Math.max(0, (g / (n - 1)) * gMul));
        const bv = Math.min(1, Math.max(0, (b / (n - 1)) * bMul));
        lines.push(`${rv.toFixed(6)} ${gv.toFixed(6)} ${bv.toFixed(6)}`);
      }
    }
  }
  const text = `${lines.join('\n')}\n`;
  return new File([text], `${title}.cube`, { type: 'text/plain', lastModified: Date.now() });
}

export async function lutShot(shot: string, api: AppApi, shell: HTMLElement): Promise<void> {
  const warm = syntheticCube('胶片暖调', 1.06, 0.98, 0.86);
  const tealOrange = syntheticCube('青橙电影', 1.04, 0.96, 1.05);
  const blackGold = syntheticCube('黑金', 1.0, 0.93, 0.78);
  const res = await api.lutBook().importFiles([warm, tealOrange, blackGold]);
  const first = res.added[0] ?? api.lutBook().list()[0];
  shell.querySelector<HTMLButtonElement>('.panel.recipe [data-mode="lut"]')?.click();
  await new Promise((done) => setTimeout(done, 400));
  if (first) {
    shell.querySelector<HTMLButtonElement>(`.panel.lut .rc[data-lut="${first.cube_id}"]`)?.click();
    await new Promise((done) => setTimeout(done, 600));
  }
  if (shot === 'lut-space') {
    shell.querySelector<HTMLButtonElement>('.band-lut-hd [data-act="lut-space"]')?.click();
    await new Promise((done) => setTimeout(done, 300));
  }
}
