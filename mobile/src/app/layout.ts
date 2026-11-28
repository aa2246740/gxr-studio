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

/** 27 寸级大屏（机主 2026-11-28）：网页窗口宽 >= 2000、高 >= 1100 才放大网格缩略图、右边栏和角标/标尺的字。
 * 和 ipad.css 末尾那段 @media 用同一组数；手机、iPad 和小窗口不变。 */
export const BIG_SCREEN_MIN_WIDTH = 2000;
export const BIG_SCREEN_MIN_HEIGHT = 1100;
export const BIG_SCREEN_QUERY = `(min-width: ${BIG_SCREEN_MIN_WIDTH}px) and (min-height: ${BIG_SCREEN_MIN_HEIGHT}px)`;

/** 给了宽高就按宽高判；没给就问浏览器（和 CSS 的 @media 是同一个答案）。没有窗口（Node 单测、Worker）时不是大屏。 */
export function isBigScreen(width?: number, height?: number): boolean {
  if (width !== undefined && height !== undefined) return width >= BIG_SCREEN_MIN_WIDTH && height >= BIG_SCREEN_MIN_HEIGHT;
  return typeof matchMedia === 'function' && matchMedia(BIG_SCREEN_QUERY).matches;
}

/** 大屏网格每格约 214–238 px：宽 2000 起 9 列，2560 宽 11 列约 231 px。 */
const BIG_GRID_STEP = 216;

/** 图库网格列数：手机保持 3 列；iPad 按宽度分列，使单张缩略图落在 150–200 pt（13" 竖 6 / 横 8，11" 竖 5 / 横 7）。大屏每格约 214–238。 */
export function gridColumns(width: number, ipad: boolean, big: boolean = isBigScreen()): number {
  if (!ipad) return 3;
  const w = Number.isFinite(width) && width > 0 ? width : 700;
  if (big) return Math.max(4, Math.floor((w + 2) / BIG_GRID_STEP));
  return Math.max(4, Math.floor((w + 2) / 160));
}

/** 网格缩略图解码宽度。小版式一直是 360（只给宽）。 */
export const GRID_THUMB_WIDTH = 360;

/** 解缩略图要盖住的盒子（设备像素）。height 为 0 时只按宽解。 */
export interface ThumbBox {
  width: number;
  height: number;
}

/**
 * 大屏按格子（宽 × 高，高是宽的 2/3 再加 2，见 grid.ts）× 设备像素比（最多 2 倍）解，宽不比 360 小；
 * Worker 再按照片比例把宽放到盖得住格高（object-fit: cover）。小版式 { 360, 0 }。
 */
export function gridThumbBox(viewWidth: number, dpr: number, big: boolean): ThumbBox {
  if (!big || !Number.isFinite(viewWidth) || viewWidth <= 0) return { width: GRID_THUMB_WIDTH, height: 0 };
  // 网格按滚动区宽分列，比窗口窄一条滚动条：按少算 24 px 分列，列数只会少不会多，格子只会估大。
  const cols = gridColumns(viewWidth - 24, true, true);
  const cell = (viewWidth - (cols - 1) * 2) / cols;
  const scale = Number.isFinite(dpr) && dpr > 1 ? Math.min(2, dpr) : 1;
  return { width: Math.max(GRID_THUMB_WIDTH, Math.ceil(cell * scale)), height: Math.ceil((cell * (2 / 3) + 2) * scale) };
}

/** 现在这扇窗口的网格缩略图盒子（没有窗口时 { 360, 0 }）。 */
export function currentGridThumbBox(): ThumbBox {
  if (typeof window === 'undefined') return { width: GRID_THUMB_WIDTH, height: 0 };
  return gridThumbBox(window.innerWidth, window.devicePixelRatio, isBigScreen());
}

/**
 * 配方卡小图的长边。小版式一直是 192；大屏按 13" 横屏配方卡（ipad.css 的 132×88，object-fit: cover）× 设备像素比（最多 2 倍）烘焙。
 * 用 136 不用 132：相机 3:2 实际是 1.506:1，132 宽解出来高只有 87.7，盖不住 88 的格高。
 */
export const CARD_BAKE_EDGE = 192;
const BIG_CARD_WIDTH = 136;

export function cardBakeEdge(dpr: number, big: boolean): number {
  if (!big) return CARD_BAKE_EDGE;
  const scale = Number.isFinite(dpr) && dpr > 1 ? Math.min(2, dpr) : 1;
  return Math.max(CARD_BAKE_EDGE, Math.ceil(BIG_CARD_WIDTH * scale));
}

/** 现在这扇窗口的配方卡小图长边（没有窗口时 192）。 */
export function currentCardBakeEdge(): number {
  if (typeof window === 'undefined') return CARD_BAKE_EDGE;
  return cardBakeEdge(window.devicePixelRatio, isBigScreen());
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

/** 横向滚动的配方条和胶片条：电脑上普通的竖向滚轮也让它们左右滚（P2-6）。 */
const WHEEL_STRIPS = '.strip, .ipad-filmstrip';
const wheelShells = new WeakSet<HTMLElement>();

/** 只在条子真能往那个方向滚时才接管（preventDefault）；横向滚动、捏合缩放（ctrlKey）和触摸设备照旧。 */
function stripWheel(event: WheelEvent): void {
  if (event.ctrlKey || Math.abs(event.deltaY) <= Math.abs(event.deltaX) || !isComputer()) return;
  const strip = event.target instanceof Element ? event.target.closest<HTMLElement>(WHEEL_STRIPS) : null;
  if (!strip) return;
  const max = strip.scrollWidth - strip.clientWidth;
  const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? strip.clientWidth : 1);
  if (max <= 0 || (delta < 0 && strip.scrollLeft <= 0) || (delta > 0 && strip.scrollLeft >= max - 1)) return;
  event.preventDefault();
  strip.scrollLeft += delta;
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
  // 每个壳只挂一次（开始页和编辑器都会调这里），壳一直在，不用拆。
  if (!wheelShells.has(shell)) {
    wheelShells.add(shell);
    shell.addEventListener('wheel', stripWheel, { passive: false });
  }
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
