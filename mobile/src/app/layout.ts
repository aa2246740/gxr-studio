// 响应式版式判定与 iPad 直方图默认开关记忆：手机竖屏 / 手机横屏 / iPad 横屏 / iPad 竖屏共用一套代码。

import { isComputer } from '../lib/save_mode';

export type LayoutMode = 'phone-portrait' | 'phone-landscape' | 'ipad-landscape' | 'ipad-portrait';
export type IpadSize = '13' | '11';

const IPAD_HIST_KEY = 'gxr.mobile.ipad_hist';

function store(): Storage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

/** 电脑上又宽又矮的窗口（笔记本浏览器、放大到 125–150 %）仍用 iPad/电脑版式的最小宽高（desktop-layout L2）。实测：
 * 高 590 时侧边面板还有 180 px、配方面板不滚动能看到 3 个控件、舞台 360 px；宽 900 时舞台还有 540 px 宽。再小就回到手机版式。 */
export const COMPUTER_SHORT_MIN_WIDTH = 900;
export const COMPUTER_SHORT_MIN_HEIGHT = 590;

/**
 * 按视口尺寸与朝向判定版式：短边 >= 700 pt 为 iPad（否则为手机，含 iPad 窄分屏/侧拉）。
 * 电脑（没有触摸点、指针精确）上横向窗口宽 >= 900、高 >= 590 也用 iPad 横屏版式；有触摸的设备照旧。
 */
export function classifyLayout(width: number, height: number, computer: boolean = isComputer()): LayoutMode {
  const w = Number.isFinite(width) && width > 0 ? width : 390;
  const h = Number.isFinite(height) && height > 0 ? height : 844;
  if (Math.min(w, h) >= 700) {
    return w > h ? 'ipad-landscape' : 'ipad-portrait';
  }
  if (computer && w > h && w >= COMPUTER_SHORT_MIN_WIDTH && h >= COMPUTER_SHORT_MIN_HEIGHT) return 'ipad-landscape';
  return w > h ? 'phone-landscape' : 'phone-portrait';
}

export function isIpadLayout(mode: LayoutMode): boolean {
  return mode === 'ipad-landscape' || mode === 'ipad-portrait';
}

/** 区分 13 寸（1366×1024 / 1024×1366）与 11 寸及以下（1180×820 等），匹配样稿面板与底条尺寸。 */
export function ipadSizeOf(width: number, height: number): IpadSize {
  const longEdge = Math.max(width, height);
  const shortEdge = Math.min(width, height);
  return longEdge >= 1240 && shortEdge >= 900 ? '13' : '11';
}

/** 图库网格列数：手机保持 3 列；iPad 按宽度分列，使单张缩略图落在 150–200 pt（13" 竖 6 / 横 8，11" 竖 5 / 横 7）。 */
export function gridColumns(width: number, ipad: boolean): number {
  if (!ipad) return 3;
  const w = Number.isFinite(width) && width > 0 ? width : 700;
  return Math.max(4, Math.floor((w + 2) / 160));
}

export interface CellLabelInfo {
  dng: boolean;
  jpg: boolean;
  diptych?: boolean;
  bad?: boolean;
  unread?: boolean;
}

/** 图库缩略图按钮的无障碍名称（仅含序号、类型与状态，绝不含文件名或拍摄时间）。 */
export function cellAriaLabel(pos: number | undefined, info: CellLabelInfo, edited: boolean, selected: boolean): string {
  const parts: string[] = [pos ? `第 ${pos} 张` : '照片'];
  if (info.diptych) parts.push('半格双联');
  else if (info.dng && info.jpg) parts.push('RAW+JPG');
  else if (info.dng) parts.push('RAW');
  else if (info.jpg) parts.push('JPG');
  if (info.bad) parts.push('读不了');
  else if (info.unread) parts.push('未读完');
  if (edited) parts.push('已编辑');
  if (selected) parts.push('已选中');
  return parts.join('，');
}

/** iPad 直方图默认打开；手动关闭后记住为 '0'。 */
export function loadIpadHist(): boolean {
  return store()?.getItem(IPAD_HIST_KEY) !== '0';
}

export function saveIpadHist(on: boolean): void {
  try {
    store()?.setItem(IPAD_HIST_KEY, on ? '1' : '0');
  } catch {
    /* 隐私模式或存储满时仅本次生效 */
  }
}

export function viewportDims(shell: HTMLElement): { width: number; height: number } {
  const width = shell.clientWidth || window.innerWidth || 390;
  const height = shell.clientHeight || window.innerHeight || 844;
  return { width, height };
}

export interface LayoutWatch {
  mode(): LayoutMode;
  ipadSize(): IpadSize;
  destroy(): void;
}

/** 监听壳尺寸变化并把 data-layout / data-ipad-size 同步到 .app，变化时回调编辑器重排。 */
export function watchViewportLayout(
  shell: HTMLElement,
  onChange: (mode: LayoutMode, ipadSize: IpadSize) => void,
): LayoutWatch {
  const initial = viewportDims(shell);
  let currentMode = classifyLayout(initial.width, initial.height);
  let currentSize = ipadSizeOf(initial.width, initial.height);
  shell.dataset.layout = currentMode;
  shell.dataset.ipadSize = currentSize;

  const check = () => {
    const dims = viewportDims(shell);
    const nextMode = classifyLayout(dims.width, dims.height);
    const nextSize = ipadSizeOf(dims.width, dims.height);
    if (nextMode === currentMode && nextSize === currentSize) return;
    currentMode = nextMode;
    currentSize = nextSize;
    shell.dataset.layout = currentMode;
    shell.dataset.ipadSize = currentSize;
    onChange(currentMode, currentSize);
  };

  window.addEventListener('resize', check);
  let observer: ResizeObserver | null = null;
  if (typeof ResizeObserver === 'function') {
    observer = new ResizeObserver(() => check());
    observer.observe(shell);
  }

  return {
    mode: () => currentMode,
    ipadSize: () => currentSize,
    destroy: () => {
      window.removeEventListener('resize', check);
      observer?.disconnect();
      observer = null;
    },
  };
}
