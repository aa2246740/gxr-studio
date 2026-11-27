// 成片怎么交出去（2026-11-26）：电脑浏览器（没有触摸点、指针精确）直接下载；手机和 iPad 开系统共享面板。
// iPadOS Safari 报的是 Mac 的 UA，但有触摸点，所以仍算 iPad，走共享。每次现查，不缓存。

export function isComputer(): boolean {
  const nav = globalThis.navigator as Navigator | undefined;
  if (!nav || nav.maxTouchPoints !== 0) return false;
  return typeof globalThis.matchMedia === 'function' && globalThis.matchMedia('(pointer: fine)').matches;
}

/** 底栏和导出选项上的主按钮：手机「存到相册」，电脑「下载」。 */
export function saveWord(): string {
  return isComputer() ? '下载' : '存到相册';
}

/** 就绪条上交出成片的按钮：手机「存进相册」，电脑「下载」。 */
export function readyWord(): string {
  return isComputer() ? '下载' : '存进相册';
}

/** 底栏主按钮的读屏名称（Label in Name：含按钮上的字）。`ready` / `busy`：就绪条亮着、当前照片已做好或正在导出。 */
export function saveAria(state: 'default' | 'ready' | 'busy' = 'default'): string {
  const save = saveWord();
  if (state === 'ready') return `${save}：当前照片已做好，请点上方${readyWord()}`;
  if (state === 'busy') return `${save}：当前照片正在导出`;
  return isComputer() ? '导出当前照片并下载' : '导出当前照片并存到相册';
}
