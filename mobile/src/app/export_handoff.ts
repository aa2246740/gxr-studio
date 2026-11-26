// 把做好的成片交出去：手机和 iPad 开系统共享面板，电脑浏览器直接下载（2026-11-26）。
// 都必须在点击里同步调用：navigator.share 前面不能有 await，几个下载也要在同一次点击里一个接一个触发。

import { isComputer } from '../lib/save_mode';
import { shareChunks, SHARE_MAX_BYTES, SHARE_MAX_FILES } from '../lib/share_plan';
import { button, el } from './dom';
import { DOWNLOAD_BACKUP } from './export_ready';
import { registerSheet } from './sheet_lifecycle';

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

export interface DownloadBackup {
  /** 收起这张表并撤销它的对象 URL。 */
  close(): void;
  /** 焦点放回表里的第一个链接（队列收尾重画以后）。 */
  focus(): void;
}

/**
 * 电脑上一次下载几个文件以后的兜底链接（浏览器可能拦下后面几个）。调用时就把文件和对象 URL 抄下来，
 * 和导出队列无关：队列马上照「交出去了」往下走（放掉这一批、接着做排着的），这张表照样列着这几个文件。
 * 「关闭」、Esc、点遮罩、切换照片或清空都只收起这张表，同时撤销它自己的 URL。
 */
export function showDownloadBackup(shell: HTMLElement, files: readonly File[]): DownloadBackup {
  const returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const urls: string[] = [];
  const scrim = el('div', 'scrim export-details-scrim');
  const sheet = el('div', 'ms export-sheet');
  sheet.dataset.state = 'download-backup';
  sheet.setAttribute('role', 'dialog');
  sheet.setAttribute('aria-modal', 'true');
  sheet.setAttribute('aria-label', '已开始下载');
  sheet.tabIndex = -1;
  const hd = el('div', 'es-sheet-hd');
  hd.append(el('h2', '', '已开始下载'));
  const body = el('div', 'es-sheet-body');
  const box = el('div', 'links');
  box.dataset.state = 'download-backup';
  box.append(el('p', 'dim small', DOWNLOAD_BACKUP));
  for (const file of files) {
    const url = URL.createObjectURL(file);
    urls.push(url);
    const a = el('a', '', file.name);
    a.href = url;
    a.download = file.name;
    box.append(a);
  }
  const done = button('btn block', '关闭');
  done.dataset.act = 'close-download-backup';
  box.append(done);
  body.append(box);
  sheet.append(hd, body);

  let closed = false;
  const onKey = (ev: KeyboardEvent): void => {
    if (ev.key === 'Escape') {
      ev.preventDefault();
      close();
      return;
    }
    if (ev.key !== 'Tab') return;
    const nodes = [...sheet.querySelectorAll<HTMLElement>('a[href], button:not([disabled])')];
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (!first || !last) return;
    if (ev.shiftKey && (document.activeElement === first || !sheet.contains(document.activeElement))) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && (document.activeElement === last || !sheet.contains(document.activeElement))) {
      ev.preventDefault();
      first.focus();
    }
  };
  const unregister = registerSheet(shell, () => close());
  function close(): void {
    if (closed) return;
    closed = true;
    unregister();
    document.removeEventListener('keydown', onKey);
    scrim.remove();
    sheet.remove();
    for (const url of urls) URL.revokeObjectURL(url);
    urls.length = 0;
    if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
  }
  scrim.addEventListener('click', close);
  done.addEventListener('click', close);
  document.addEventListener('keydown', onKey);
  shell.append(scrim, sheet);
  const focus = (): void => {
    if (!closed) (box.querySelector<HTMLElement>('a[href]') ?? sheet).focus({ preventScroll: true });
  };
  focus();
  return { close, focus };
}
