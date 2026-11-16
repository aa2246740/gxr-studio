// 只在开发服务器里给模拟器截导出那几屏（`?shot=export-…`）。照片走样本端点，正式包里没有这段。
// export-saved 截 05e：模拟器里 openurl 不算点击，Safari 不会打开共享面板，所以这一屏把 share 换成直接 resolve 的假的。

import { aliasFile } from './shots';
import type { ExportFlow } from './export_ui';

const wait = (ms: number) => new Promise((done) => setTimeout(done, ms));

async function until(test: () => boolean, ms: number): Promise<void> {
  const end = performance.now() + ms;
  while (!test() && performance.now() < end) await wait(200);
}

/**
 * `?shot=export-…-hdr`：截开着 HDR 的 05a / 05b / 05c。样本端点只发 `.dng` 名字，相机 JPG 由截图脚本以
 * `zz-<主干>.dng` 放进去，这里按原名 `<主干>.JPG` 拿回来；`zz-jpg.dng` 是那张只有 JPG 的（`R9990002.JPG`）。
 */
export async function hdrShotFiles(dngs: File[]): Promise<File[]> {
  const out: File[] = [];
  for (const dng of dngs) {
    const stem = dng.name.replace(/\.[^.]+$/, '');
    const jpg = await aliasFile(`zz-${stem.toLowerCase()}.dng`, `${stem}.JPG`);
    if (jpg) out.push(jpg);
  }
  const only = await aliasFile('zz-jpg.dng', 'R9990002.JPG');
  if (only) out.push(only);
  return out;
}

export async function exportShot(name: string, ids: string[], flow: ExportFlow, shell: HTMLElement): Promise<void> {
  const hooks = (window as unknown as { __devExport?: { trapAt?: number } }).__devExport;
  const hdr = name.endsWith('-hdr');
  const shot = hdr ? name.slice(0, -'-hdr'.length) : name;
  try {
    localStorage.setItem('gxr.mobile.export_hdr', hdr ? '1' : '0');
  } catch {
    /* 没有 localStorage：开关照默认。 */
  }
  if (shot === 'export-saved') {
    Object.defineProperty(navigator, 'canShare', { configurable: true, value: () => true });
    Object.defineProperty(navigator, 'share', { configurable: true, value: () => Promise.resolve() });
  }
  if (shot === 'export-partial' && hooks) hooks.trapAt = 1;
  const pick = hdr || shot === 'export-sheet' || shot === 'export-run' ? ids.slice(0, 5) : ids.slice(0, 3);
  flow.open(pick);
  await wait(800);
  if (shot === 'export-sheet') return;
  shell.querySelector<HTMLButtonElement>('.ms.export .go')?.click();
  const phase = () => flow.current()?.phase;
  if (shot === 'export-run') {
    await until(() => (flow.current()?.progress().done ?? 0) >= 1, 120_000);
    return;
  }
  await until(() => phase() === 'ready', 300_000);
  if (hooks) hooks.trapAt = undefined;
  // 截 05d 时脚本要在模拟器里真的点一下「存到相册」：把按钮的位置（CSS 像素）和视口大小报给它。
  const btn = shell.querySelector<HTMLElement>('.pdock [data-act="share"]')?.getBoundingClientRect();
  if (btn) {
    const rect = { x: btn.x + btn.width / 2, y: btn.y + btn.height / 2, vw: window.innerWidth, vh: window.innerHeight, sw: screen.width, sh: screen.height };
    await fetch('/__dev/report/shot-rect', { method: 'POST', body: JSON.stringify(rect) }).catch(() => undefined);
  }
  if (shot === 'export-saved') {
    shell.querySelector<HTMLButtonElement>('.pdock [data-act="share"]')?.click();
    await until(() => !!shell.querySelector('.pdock [data-act="done"]'), 10_000);
  }
}
