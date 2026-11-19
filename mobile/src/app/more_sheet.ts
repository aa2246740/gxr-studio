// 「更多」工作表：边框（第一项）、时间戳、曲线、复制调整、导出选项、校准与配方文件。

import { curveJson } from '../lib/export_request';
import { FRAME_NAMES, stampSummary } from '../lib/frame_spec';
import { button, el, icon } from './dom';
import type { Photo } from './model';
import { registerSheet } from './sheet_lifecycle';

const CHEVRON = 'M9 6l6 6-6 6';

export interface MoreSheetHooks {
  onFrame(): void;
  onStamp(): void;
  onCurves(): void;
  onCopy(): void;
  onExport(): void;
  onCalib(): void;
}

export function openMoreSheet(shell: HTMLElement, photo: Photo, hooks: MoreSheetHooks): () => void {
  const scrim = el('div', 'scrim');
  const sheet = el('div', 'ms more-sheet');
  sheet.setAttribute('role', 'dialog');
  sheet.setAttribute('aria-label', '更多');

  let unregister: () => void = () => undefined;
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    unregister();
    scrim.remove();
    sheet.remove();
  };
  unregister = registerSheet(shell, close);
  scrim.addEventListener('click', close);

  const hd = el('div', 'sheet-hd');
  hd.append(el('h2', '', '更多'));
  const done = button('nb', '关闭');
  done.dataset.act = 'close';
  done.addEventListener('click', close);
  hd.append(done);

  const frameName = FRAME_NAMES[photo.border.style] || '无';
  const stampText = stampSummary(photo.stamp, photo.border);
  const curveText = curveJson(photo.curve) ? '已调' : '';

  const rows: Array<{
    key: string;
    act?: string;
    title: string;
    sub: string;
    run: () => void;
  }> = [
    { key: 'frame', title: '边框', sub: frameName, run: hooks.onFrame },
    { key: 'stamp', title: '时间戳', sub: stampText, run: hooks.onStamp },
    { key: 'curves', title: '曲线', sub: curveText, run: hooks.onCurves },
    { key: 'copy', act: 'copy', title: '复制调整', sub: '', run: hooks.onCopy },
    { key: 'export', title: '导出选项', sub: '', run: hooks.onExport },
    { key: 'calib', title: '校准与配方文件', sub: '', run: hooks.onCalib },
  ];

  const list = el('div', 'more-list');
  for (const item of rows) {
    const row = button('lrow more-row', '');
    row.dataset.more = item.key;
    if (item.act) row.dataset.act = item.act;
    const tx = el('div', 'grow');
    tx.append(el('b', '', item.title));
    if (item.sub) tx.append(el('small', '', item.sub));
    const chev = el('span', 'more-chev');
    chev.append(icon(CHEVRON));
    row.append(tx, chev);
    row.addEventListener('click', () => {
      close();
      item.run();
    });
    list.append(row);
  }

  sheet.append(hd, list);
  shell.append(scrim, sheet);
  return close;
}
