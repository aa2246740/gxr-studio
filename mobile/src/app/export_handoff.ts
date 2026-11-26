// 把做好的成片交出去：手机和 iPad 开系统共享面板，电脑浏览器直接下载（2026-11-26）。
// 都必须在点击里同步调用：navigator.share 前面不能有 await，几个下载也要在同一次点击里一个接一个触发。

import { isComputer } from '../lib/save_mode';
import { shareChunks, SHARE_MAX_BYTES, SHARE_MAX_FILES } from '../lib/share_plan';

/** 下载开始以后再撤销对象 URL：立刻撤销的话有的浏览器会下不下来。 */
const REVOKE_MS = 60_000;

/** 一次点击里把文件一个接一个下载下来，文件名和字节与共享时那份一样。 */
export function downloadFiles(files: readonly File[]): void {
  for (const file of files) {
    const url = URL.createObjectURL(file);
    const link = document.createElement('a');
    link.href = url;
    link.download = file.name;
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), REVOKE_MS);
  }
}

/**
 * 交出这些文件。`download`：电脑上已经下载了；`share`：共享面板开了，关掉后按结果调 `onShared` / `onCancel`；
 * `none`：这个浏览器没有共享面板，调用方给下载链接。
 */
export function handOff(files: File[], onShared: () => void, onCancel: () => void = () => undefined): 'download' | 'share' | 'none' {
  if (isComputer()) {
    downloadFiles(files);
    return 'download';
  }
  const nav = navigator as Navigator & { canShare?: (data: ShareData) => boolean };
  const can = typeof nav.share === 'function' && typeof nav.canShare === 'function' && nav.canShare({ files });
  if (!can) return 'none';
  nav.share({ files }).then(onShared, onCancel);
  return 'share';
}

/** 共享面板一次只交一批（张数、字节有上限）；电脑上下载没有这个限制，全部一批。 */
export function handOffChunks(sizes: readonly number[], maxFiles = SHARE_MAX_FILES): number[][] {
  if (!isComputer()) return shareChunks(sizes, maxFiles, SHARE_MAX_BYTES);
  return sizes.length ? [sizes.map((_, i) => i)] : [];
}
