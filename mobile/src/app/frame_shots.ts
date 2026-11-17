// 只在开发服务器里给 Playwright 截图用：摆出 03f 边框、更多选项、时间戳、标志四个界面。

async function waitFor(pred: () => boolean, timeoutMs = 6000): Promise<void> {
  const deadline = performance.now() + timeoutMs;
  while (!pred() && performance.now() < deadline) {
    await new Promise((r) => setTimeout(r, 80));
  }
}

export async function frameShot(shot: string, shell: HTMLElement, view: HTMLElement): Promise<void> {
  const frameTab = [...view.querySelectorAll<HTMLButtonElement>('.tabs button')].find(
    (btn) => btn.textContent?.trim() === '边框',
  );
  frameTab?.click();
  await waitFor(() => view.querySelectorAll('.panel.frame .rc .th.fr canvas').length >= 13);

  if (shot === 'frame' || shot === 'frame-more' || shot === 'frame-stamp' || shot === 'frame-logo') {
    const leica = view.querySelector<HTMLButtonElement>('.panel.frame .rc[data-style="leica"]');
    if (leica && !leica.classList.contains('on')) leica.click();
    await waitFor(() => Boolean(view.querySelector('.stage .pv[data-decor="1"] .pv-deco')));
  }

  if (shot === 'frame-more') {
    view.querySelector<HTMLButtonElement>('.panel.frame [data-act="frame-more"]')?.click();
    await waitFor(() => Boolean(shell.querySelector('.ms.frame-more-sheet')));
  } else if (shot === 'frame-stamp') {
    const sw = view.querySelector<HTMLInputElement>('.panel.frame .stamp-row input.switch');
    if (sw && !sw.checked) sw.click();
    view.querySelector<HTMLButtonElement>('.panel.frame [data-act="open-stamp"]')?.click();
    await waitFor(() => Boolean(shell.querySelector('.ms.stamp-sheet')));
  } else if (shot === 'frame-logo') {
    view.querySelector<HTMLButtonElement>('.panel.frame [data-act="frame-logo"]')?.click();
    await waitFor(() => Boolean(shell.querySelector('.ms.logo-sheet')));
  }

  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  await new Promise((r) => setTimeout(r, 250));
}
