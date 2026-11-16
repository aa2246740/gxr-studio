// 只在开发服务器里给模拟器截导出那几屏（`?shot=export-…`）。照片走样本端点，正式包里没有这段。
// export-saved 截 05e：模拟器里 openurl 不算点击，Safari 不会打开共享面板，所以这一屏把 share 换成直接 resolve 的假的。

import type { ExportFlow } from './export_ui';

const wait = (ms: number) => new Promise((done) => setTimeout(done, ms));

async function until(test: () => boolean, ms: number): Promise<void> {
  const end = performance.now() + ms;
  while (!test() && performance.now() < end) await wait(200);
}

export async function exportShot(shot: string, ids: string[], flow: ExportFlow, shell: HTMLElement): Promise<void> {
  const hooks = (window as unknown as { __devExport?: { trapAt?: number } }).__devExport;
  if (shot === 'export-saved') {
    Object.defineProperty(navigator, 'canShare', { configurable: true, value: () => true });
    Object.defineProperty(navigator, 'share', { configurable: true, value: () => Promise.resolve() });
  }
  if (shot === 'export-partial' && hooks) hooks.trapAt = 1;
  const pick = shot === 'export-sheet' || shot === 'export-run' ? ids.slice(0, 5) : ids.slice(0, 3);
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
