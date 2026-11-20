// 导出条占住顶槽时，拖动滑块 / 旋转尺 / 配方微调时在单带上方 12pt 显示 28pt 半透明数值胶囊；松手 400ms 后淡出。

import { el } from './dom';

const FADE_DELAY_MS = 400;
const HIDE_AFTER_FADE_MS = 180;

export class ScrubPill {
  private readonly root: HTMLElement;
  private readonly shell: HTMLElement;
  private fadeTimer = 0;
  private hideTimer = 0;

  constructor(shell: HTMLElement) {
    this.shell = shell;
    this.root = el('div', 'scrub-pill num');
    this.root.dataset.state = 'scrub-pill';
    this.root.dataset.visible = '0';
    this.root.setAttribute('aria-hidden', 'true');
    this.root.hidden = true;
  }

  el(): HTMLElement {
    return this.root;
  }

  /** 仅在导出条活动时展示（闲置态数字仍在 44pt 带头里）。 */
  show(text: string): void {
    if (!this.shell.dataset.export || !text) {
      this.hideNow();
      return;
    }
    this.clearTimers();
    this.root.textContent = text;
    this.root.hidden = false;
    this.root.dataset.visible = '1';
    this.root.classList.add('show');
  }

  /** 松手 400ms 后淡出。 */
  release(): void {
    if (this.root.hidden) return;
    this.clearTimers();
    this.fadeTimer = window.setTimeout(() => {
      this.fadeTimer = 0;
      this.root.classList.remove('show');
      this.root.dataset.visible = '0';
      this.hideTimer = window.setTimeout(() => {
        this.hideTimer = 0;
        if (this.root.dataset.visible === '0') this.root.hidden = true;
      }, HIDE_AFTER_FADE_MS);
    }, FADE_DELAY_MS);
  }

  hideNow(): void {
    this.clearTimers();
    this.root.classList.remove('show');
    this.root.dataset.visible = '0';
    this.root.hidden = true;
  }

  destroy(): void {
    this.hideNow();
  }

  private clearTimers(): void {
    if (this.fadeTimer) {
      window.clearTimeout(this.fadeTimer);
      this.fadeTimer = 0;
    }
    if (this.hideTimer) {
      window.clearTimeout(this.hideTimer);
      this.hideTimer = 0;
    }
  }
}
