// 电脑浏览器上，有照片或正在导出时，刷新、关标签页先问一声（2026-11-26）。手机和 iPad 不挂：
// 那边切走就是常事，弹窗只会挡路。照片只在这次使用中保留，问一声免得 Cmd+R、Cmd+W 一下全没了。

import { isComputer } from '../lib/save_mode';

let armed = false;

function onBeforeUnload(event: BeforeUnloadEvent): void {
  event.preventDefault();
  event.returnValue = '';
}

/** `busy`：有照片，或者有导出在跑。只在电脑上挂 beforeunload；清空了就摘掉。 */
export function guardUnload(busy: boolean): void {
  const want = busy && isComputer();
  if (want === armed) return;
  armed = want;
  if (want) window.addEventListener('beforeunload', onBeforeUnload);
  else window.removeEventListener('beforeunload', onBeforeUnload);
}
