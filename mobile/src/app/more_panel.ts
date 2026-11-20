// iPad 面板内联「更多」列表：不弹工作表，边框排第一项，其余与手机「更多」完全一致。

import { curveJson } from '../lib/export_request';
import { FRAME_NAMES, stampSummary } from '../lib/frame_spec';
import { button, el, icon } from './dom';
import type { Photo } from './model';
import type { MoreSheetHooks } from './more_sheet';

const CHEVRON = 'M9 6l6 6-6 6';

export class MorePanel {
  private readonly root: HTMLElement;
  private readonly list: HTMLElement;
  private readonly hooks: MoreSheetHooks;

  constructor(photo: Photo, hooks: MoreSheetHooks) {
    this.hooks = hooks;
    this.root = el('div', 'panel more-panel');
    this.root.dataset.panel = 'more';
    this.list = el('div', 'more-list');
    this.root.append(this.list);
    this.refresh(photo);
  }

  panelEl(): HTMLElement {
    return this.root;
  }

  refresh(photo: Photo): void {
    this.list.replaceChildren();

    const frameName = FRAME_NAMES[photo.border.style] || '无';
    const stampText = stampSummary(photo.stamp, photo.border);
    const curveText = curveJson(photo.curve) ? '已调' : '';

    const rows: Array<{ key: string; title: string; sub: string; run: () => void }> = [
      { key: 'frame', title: '边框', sub: frameName, run: this.hooks.onFrame },
      { key: 'stamp', title: '时间戳', sub: stampText, run: this.hooks.onStamp },
      { key: 'curves', title: '曲线', sub: curveText, run: this.hooks.onCurves },
      { key: 'export', title: '导出选项', sub: '', run: this.hooks.onExport },
      { key: 'calib', title: '校准与配方文件', sub: '', run: this.hooks.onCalib },
    ];

    for (const item of rows) {
      const row = button('lrow more-row', '');
      row.dataset.more = item.key;
      const tx = el('div', 'grow');
      tx.append(el('b', '', item.title));
      if (item.sub) tx.append(el('small', '', item.sub));
      const chev = el('span', 'more-chev');
      chev.append(icon(CHEVRON));
      row.append(tx, chev);
      row.addEventListener('click', () => item.run());
      this.list.append(row);
    }
  }
}
