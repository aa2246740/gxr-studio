// 只在开发服务器里给模拟器截图用（?shot=paste-04a 之类）：把复制 / 套用摆到设计稿的那几屏。
// 模拟器没有显卡，滑块是停用的，所以调整直接写进这张照片的编辑状态（和滑块松手一样加 rev）。

import type { AppApi } from './model';

const wait = (ms: number) => new Promise((done) => setTimeout(done, ms));

async function until(test: () => boolean, ms = 8000): Promise<boolean> {
  const end = performance.now() + ms;
  while (performance.now() < end) {
    if (test()) return true;
    await wait(100);
  }
  return false;
}

function click(root: ParentNode, selector: string): boolean {
  const node = root.querySelector<HTMLElement>(selector);
  node?.click();
  return !!node;
}

export async function pasteShot(shot: string, api: AppApi & { take(files: File[]): void }, shell: HTMLElement): Promise<void> {
  const { aliasFile } = await import('./shots');
  const dat = await aliasFile('zz-dat.dng', 'GXRCE.DAT');
  if (dat) api.take([dat]);
  await until(() => !!api.book().dat, 15_000);
  const photos = api.photos();
  // 来源：最晚的那张真照片（截图样本里 R999* 是改了拍摄时间的副本）。
  const real = photos.filter((photo) => !photo.stem.startsWith('R999'));
  const source = real[real.length - 1] ?? photos[0];
  if (!source) return;
  source.knobs.exposure_ev = 0.7;
  source.rev += 1;
  api.open(source.id);
  await until(() => !!shell.querySelector('.editor .strip[data-recipes] .rc[data-card="builtin:2"]'));
  await wait(600);
  click(shell, '.editor .strip[data-recipes] .rc[data-card="builtin:2"]');
  await wait(300);
  click(shell, '.editor .nav [data-act="editor-more"]');
  await until(() => !!shell.querySelector('.ms[data-state="editor-more"] [data-act="more-copy"]'));
  click(shell, '.ms[data-state="editor-more"] [data-act="more-copy"]');
  await until(() => !!shell.querySelector('[data-state="copy-options"]'));
  if (shot === 'paste-04a') return;
  click(shell, '[data-state="copy-options"] .btn.primary');
  await until(() => !!shell.querySelector('[data-state="copied-where"]'));
  if (shot === 'paste-04b') return;
  if (shot === 'paste-02a') {
    click(shell, '[data-state="copied-where"] .btn.ghost');
    api.back();
    await until(() => !!shell.querySelector('.pastebar[data-state="copied"]'));
    return;
  }
  if (shot === 'paste-04d') {
    const pick = [...shell.querySelectorAll<HTMLButtonElement>('[data-state="copied-where"] .btn')].find((node) => node.textContent === '选照片…');
    pick?.click();
    await until(() => !!shell.querySelector('.gscroll .cell'));
    await wait(400);
    for (const photo of api.photos().filter((item) => item.id !== source.id).slice(0, 3)) click(shell, `.cell[data-id="${photo.id}"]`);
    await wait(300);
    return;
  }
  click(shell, '[data-state="copied-where"] [data-act="apply-group"]');
  await until(() => !!shell.querySelector('[data-state="confirm"]'));
  if (shot === 'paste-04c') return;
  click(shell, '[data-state="confirm"] [data-act="confirm"]');
  await until(() => !!shell.querySelector('.pastebar[data-state="applied"]'));
}
