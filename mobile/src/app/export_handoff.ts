// 把做好的成片交出去：手机和 iPad 开系统共享面板，电脑浏览器直接下载（2026-11-26）。
// 都必须在点击里同步调用：navigator.share 前面不能有 await。电脑上一批 2 个以上的文件打成一个 ZIP 下载：Safari
// 一次点击只放行 1 个下载，Chrome 最多 10 个（desktop-accept F1 / F2）。ZIP 里的名字和字节与逐个下载一样。

import { isComputer } from '../lib/save_mode';
import { shareChunks, SHARE_MAX_BYTES, SHARE_MAX_FILES } from '../lib/share_plan';
import { zipOf, zipParts, ZIP_MAX_ENTRIES } from '../lib/zip_write';
import { button, el } from './dom';
import { DOWNLOAD_BACKUP, zipStarted } from './export_ready';
import { registerSheet } from './sheet_lifecycle';

/** 下载开始以后再撤销对象 URL：立刻撤销的话有的浏览器会下不下来。 */
const REVOKE_MS = 60_000;

function downloadBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), REVOKE_MS);
}

/** 一次点击里把文件一个接一个下载下来，文件名和字节与共享时那份一样（浏览器可能拦下后面几个）。 */
export function downloadFiles(files: readonly File[]): void {
  for (const file of files) downloadBlob(file, file.name);
}

/**
 * 交出这些文件。`zip`：电脑上 2 个以上的文件打成一个 ZIP 下载了；`download`：电脑上逐个下载了（一个文件，或者有文件
 * 不是导出记下的、算不出 CRC）；`share`：共享面板开了，关掉后按结果调 `onShared` / `onCancel`；
 * `none`：这个浏览器没有共享面板，调用方给下载链接。
 */
export function handOff(
  files: File[],
  onShared: () => void,
  onCancel: () => void = () => undefined,
): 'zip' | 'download' | 'share' | 'none' {
  if (isComputer()) {
    const zip = files.length > 1 ? zipOf(files) : null;
    if (zip) {
      downloadBlob(zip.blob, zip.name);
      return 'zip';
    }
    downloadFiles(files);
    return 'download';
  }
  const nav = navigator as Navigator & { canShare?: (data: ShareData) => boolean };
  const can = typeof nav.share === 'function' && typeof nav.canShare === 'function' && nav.canShare({ files });
  if (!can) return 'none';
  nav.share({ files }).then(onShared, onCancel);
  return 'share';
}

/**
 * 共享面板一次只交一批（张数、字节有上限）。电脑上一批是一个 ZIP：不用 ZIP64，超过 65,000 个文件或 3.5 GB 才拆成几包。
 * `maxFiles` 是开发服务器测试用的分批上限。
 */
export function handOffChunks(sizes: readonly number[], maxFiles?: number): number[][] {
  if (!isComputer()) return shareChunks(sizes, maxFiles ?? SHARE_MAX_FILES, SHARE_MAX_BYTES);
  return zipParts(sizes, maxFiles ?? ZIP_MAX_ENTRIES);
}

export interface DownloadBackup {
  /** 收起这张表并撤销它的对象 URL。 */
  close(): void;
  /** 焦点放回表里的第一个链接（队列收尾重画以后）。 */
  focus(): void;
}

/**
 * 电脑上一次下载几个文件以后的兜底链接。`zip`：已经下载了一个 ZIP，逐个的链接收在「单张下载」里；否则浏览器可能拦下后面几个。
 * 点过的链接后面打 ✓。调用时就把文件和对象 URL 抄下来，
 * 和导出队列无关：队列马上照「交出去了」往下走（放掉这一批、接着做排着的），这张表照样列着这几个文件。
 * 「关闭」、Esc、点遮罩、切换照片或清空都只收起这张表，同时撤销它自己的 URL。
 */
export function showDownloadBackup(shell: HTMLElement, files: readonly File[], zip = false): DownloadBackup {
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
  box.append(el('p', 'dim small', zip ? zipStarted(files.length) : DOWNLOAD_BACKUP));
  const fold = zip ? el('details', 'dl-fold') : null;
  if (fold) {
    fold.append(el('summary', '', '单张下载'));
    box.append(fold);
  }
  for (const file of files) {
    const url = URL.createObjectURL(file);
    urls.push(url);
    const a = el('a', '', file.name);
    a.href = url;
    a.download = file.name;
    a.addEventListener('click', () => {
      if (a.dataset.clicked === '1') return;
      a.dataset.clicked = '1';
      a.append(el('span', 'dl-done', ' ✓'));
    });
    (fold ?? box).append(a);
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
    const nodes = [...sheet.querySelectorAll<HTMLElement>('a[href], summary, button:not([disabled])')].filter((n) => n.getClientRects().length > 0);
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
    if (!closed) (box.querySelector<HTMLElement>(zip ? 'summary' : 'a[href]') ?? sheet).focus({ preventScroll: true });
  };
  focus();
  return { close, focus };
}
