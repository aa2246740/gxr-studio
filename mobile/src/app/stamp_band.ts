// 128pt 单带里的「时间戳」视图：开关、常用配色/位置快选与「详细设置 ›」打开完整时间戳工作表。

import {
  STAMP_COLOR_HEX,
  STAMP_COLOR_NAMES,
  STAMP_CORNERS,
  stampSummary,
  type StampColor,
  type StampCorner,
} from '../lib/frame_spec';
import { button, el } from './dom';
import { commitStamp } from './frame_state';
import type { Photo } from './model';
import { openStampSheet } from './stamp_sheet';

const COLOR_ORDER: readonly StampColor[] = ['orange', 'red', 'yellow', 'white'];
const CORNER_ORDER: readonly StampCorner[] = ['br', 'bl', 'tr', 'tl'];

export interface StampBandHooks {
  isAlive(): boolean;
  onChange(): void;
  onBack?: () => void;
}

export class StampBandView {
  private readonly shell: HTMLElement;
  private readonly photo: Photo;
  private readonly hooks: StampBandHooks;
  private readonly root: HTMLElement;
  private readonly summaryEl: HTMLElement;
  private readonly toggle: HTMLInputElement;
  private readonly row2: HTMLElement;

  constructor(shell: HTMLElement, photo: Photo, hooks: StampBandHooks) {
    this.shell = shell;
    this.photo = photo;
    this.hooks = hooks;
    this.root = el('div', 'band-tool stamp-band');
    this.root.dataset.panel = 'stamp';

    const subHd = el('div', 'ipad-sub-hd');
    const subBack = button('ipad-sub-back', '‹ 更多');
    subBack.dataset.act = 'more-back';
    subBack.addEventListener('click', () => this.hooks.onBack?.());
    subHd.append(subBack, el('b', 'ipad-sub-title', '时间戳'));

    const row1 = el('div', 'band-row pad between');
    const left = document.createElement('label');
    left.className = 'row gap8 stamp-toggle-hit';
    this.toggle = document.createElement('input');
    this.toggle.type = 'checkbox';
    this.toggle.className = 'switch';
    this.toggle.setAttribute('aria-label', '时间戳');
    this.toggle.addEventListener('change', () => {
      if (!this.hooks.isAlive()) return;
      commitStamp(this.photo, { enabled: this.toggle.checked }, () => this.changed());
    });
    this.summaryEl = el('span', 'dim small stamp-sum', '');
    left.append(this.toggle, this.summaryEl);

    const detail = button('lnk stamp-detail', '详细设置 ›');
    detail.dataset.act = 'stamp-sheet';
    detail.addEventListener('click', () => {
      if (!this.hooks.isAlive()) return;
      openStampSheet(this.shell, this.photo, () => this.changed(), () => this.hooks.isAlive());
    });
    row1.append(left, detail);

    this.row2 = el('div', 'chips stamp-quick');
    this.root.append(subHd, row1, this.row2);
    this.refresh();
  }

  panelEl(): HTMLElement {
    return this.root;
  }

  refresh(): void {
    const stamp = this.photo.stamp;
    this.toggle.checked = stamp.enabled;
    this.summaryEl.textContent = stampSummary(stamp, this.photo.border);
    this.row2.replaceChildren();

    for (const color of COLOR_ORDER) {
      const sw = button(stamp.color === color ? 'swc on' : 'swc', '');
      sw.dataset.color = color;
      sw.setAttribute('aria-label', STAMP_COLOR_NAMES[color]);
      const dot = el('i');
      dot.style.background = STAMP_COLOR_HEX[color];
      sw.append(dot);
      sw.addEventListener('click', () => {
        if (!this.hooks.isAlive()) return;
        commitStamp(this.photo, { enabled: true, color }, () => this.changed());
      });
      this.row2.append(sw);
    }

    for (const corner of CORNER_ORDER) {
      const chip = button(stamp.corner === corner ? 'chip on' : 'chip', STAMP_CORNERS[corner]);
      chip.dataset.corner = corner;
      chip.addEventListener('click', () => {
        if (!this.hooks.isAlive()) return;
        commitStamp(this.photo, { enabled: true, corner }, () => this.changed());
      });
      this.row2.append(chip);
    }
  }

  private changed(): void {
    if (!this.hooks.isAlive()) return;
    this.refresh();
    this.hooks.onChange();
  }
}
