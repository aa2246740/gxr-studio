// 固定 128pt 单带容器与浮窗直方图定位：记住收起状态；舞台状态标签放在单带顶部，不盖在照片上。

import { el } from './dom';
import type { BandTool } from './editor_bar';

const COLLAPSED_KEY = 'gxr.mobile.band_collapsed';

export function loadBandCollapsed(): boolean {
  try {
    return window.localStorage?.getItem(COLLAPSED_KEY) === '1';
  } catch {
    return false;
  }
}

export function saveBandCollapsed(collapsed: boolean): void {
  try {
    window.localStorage?.setItem(COLLAPSED_KEY, collapsed ? '1' : '0');
  } catch {
    /* ignore */
  }
}

export class EditorBand {
  private readonly root: HTMLElement;
  private readonly head: HTMLElement;
  private readonly labelSlot: HTMLElement;
  private readonly rightSlot: HTMLElement;
  private readonly body: HTMLElement;
  private readonly panels = new Map<BandTool, HTMLElement>();
  private readonly heads = new Map<BandTool, HTMLElement>();
  private activeTool: BandTool = 'recipe';
  private collapsed: boolean;

  constructor() {
    this.collapsed = loadBandCollapsed();
    this.root = el('section', 'band');
    this.root.dataset.tool = this.activeTool;
    this.root.dataset.collapsed = this.collapsed ? '1' : '0';

    this.head = el('div', 'band-hd');
    this.labelSlot = el('div', 'lbl');
    this.rightSlot = el('div', 'band-hd-right');
    this.head.append(this.labelSlot, this.rightSlot);

    this.body = el('div', 'band-body');
    this.root.append(this.head, this.body);
  }

  el(): HTMLElement {
    return this.root;
  }

  labelEl(): HTMLElement {
    return this.labelSlot;
  }

  tool(): BandTool {
    return this.activeTool;
  }

  isCollapsed(): boolean {
    return this.collapsed;
  }

  register(tool: BandTool, panel: HTMLElement, headRight?: HTMLElement): void {
    panel.hidden = tool !== this.activeTool;
    this.panels.set(tool, panel);
    this.body.append(panel);
    if (headRight) {
      headRight.hidden = tool !== this.activeTool;
      this.heads.set(tool, headRight);
      this.rightSlot.append(headRight);
    }
  }

  setTool(tool: BandTool): void {
    this.activeTool = tool;
    this.root.dataset.tool = tool;
    for (const [id, panel] of this.panels) {
      panel.hidden = id !== tool;
    }
    for (const [id, ctrl] of this.heads) {
      ctrl.hidden = id !== tool;
    }
  }

  setCompactHead(compact: boolean): void {
    this.root.dataset.compactHd = compact ? '1' : '0';
  }

  setCollapsed(collapsed: boolean): void {
    this.collapsed = collapsed;
    this.root.dataset.collapsed = collapsed ? '1' : '0';
    saveBandCollapsed(collapsed);
  }

  toggleCollapsed(): boolean {
    this.setCollapsed(!this.collapsed);
    return this.collapsed;
  }
}

/** 计算浮窗直方图在舞台里的位置：有上下黑边时优先放在下黑边，否则放在右下角（避开左上天空）。 */
export function positionFloatHist(
  stage: HTMLElement,
  pv: HTMLElement,
  histFloat: HTMLElement,
  photoSize?: { w: number; h: number } | null,
): void {
  if (histFloat.hidden) return;
  const sRect = stage.getBoundingClientRect();
  const pvRect = pv.getBoundingClientRect();
  let bottomGap = 0;
  let topGap = 0;
  if (sRect.width > 0 && sRect.height > 0 && pvRect.width > 0 && pvRect.height > 0) {
    const nw = photoSize && photoSize.w > 0 ? photoSize.w : pvRect.width;
    const nh = photoSize && photoSize.h > 0 ? photoSize.h : pvRect.height;
    const scale = Math.min(pvRect.width / nw, pvRect.height / nh);
    const drawH = nh * scale;
    const drawTop = pvRect.top + (pvRect.height - drawH) / 2;
    topGap = Math.max(0, drawTop - sRect.top);
    bottomGap = Math.max(0, sRect.bottom - (drawTop + drawH));
  }
  histFloat.style.left = 'auto';
  histFloat.style.right = '10px';
  if (bottomGap >= 66) {
    histFloat.style.top = 'auto';
    histFloat.style.bottom = `${Math.max(8, Math.round((bottomGap - 56) / 2))}px`;
    histFloat.dataset.place = 'letterbox-bottom';
  } else if (topGap >= 66) {
    histFloat.style.bottom = 'auto';
    histFloat.style.top = `${Math.max(8, Math.round((topGap - 56) / 2))}px`;
    histFloat.dataset.place = 'letterbox-top';
  } else {
    histFloat.style.top = 'auto';
    histFloat.style.bottom = '10px';
    histFloat.dataset.place = 'corner-br';
  }
}
