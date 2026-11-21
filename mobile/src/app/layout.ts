// 响应式版式判定与 iPad 直方图默认开关记忆：手机竖屏 / 手机横屏 / iPad 横屏 / iPad 竖屏共用一套代码。

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

/** 按视口尺寸与朝向判定版式：短边 >= 700 pt 为 iPad（否则为手机，含 iPad 窄分屏/侧拉）。 */
export function classifyLayout(width: number, height: number): LayoutMode {
  const w = Number.isFinite(width) && width > 0 ? width : 390;
  const h = Number.isFinite(height) && height > 0 ? height : 844;
  if (Math.min(w, h) >= 700) {
    return w > h ? 'ipad-landscape' : 'ipad-portrait';
  }
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
