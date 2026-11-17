// iPad 用窗口宽度分三档，不看机型。窄窗口（分屏、滑过）回到手机版。

export const PAD_MIN = 700;
export const COLUMNS_MIN = 1000;
/** 2026-11-13 真机探测：maxTextureDimension2D 是 16384。预览画布不超过它。 */
export const TEXTURE_LIMIT = 16384;

export const GESTURE_HINT = '左右滑动换一张，按住看原图。';

export type LayoutMode = 'phone' | 'pad' | 'columns';

/**
 * 宽度够 1000 且比高度更宽才是三栏。13 寸竖屏是 1032×1376，宽度也过了 1000，
 * 但设计稿把这一档当成放大的手机；只看宽度会把它收成三栏。
 */
export function layoutMode(width: number, height = 0): LayoutMode {
  if (!(width >= PAD_MIN)) return 'phone';
  const wide = !(height > 0) || width > height;
  if (width >= COLUMNS_MIN && wide) return 'columns';
  return 'pad';
}

/** 格子列数看这一栏的实际宽度。不到 700 仍是 3 列（手机和三栏左栏）；竖屏更宽才加列。 */
export function gridColumns(width: number): number {
  if (!(width >= 700)) return 3;
  if (width >= 900) return 5;
  return 4;
}

export function watchLayout(shell: HTMLElement, onChange: (mode: LayoutMode, prev: LayoutMode) => void): () => void {
  let mode: LayoutMode = 'phone';
  let seen = '';
  const read = (): void => {
    const width = shell.clientWidth || document.documentElement.clientWidth || window.innerWidth;
    const height = shell.clientHeight || window.innerHeight;
    const next = layoutMode(width, height);
    const key = `${width}x${height}`;
    if (next === mode && shell.dataset.layout === next && key === seen) return;
    const prev = mode;
    mode = next;
    seen = key;
    shell.dataset.layout = next;
    onChange(next, prev);
  };
  const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(() => read()) : null;
  observer?.observe(shell);
  window.addEventListener('orientationchange', read);
  read();
  return () => {
    observer?.disconnect();
    window.removeEventListener('orientationchange', read);
  };
}

/** 网格和编辑器各有一个固定宿主。手机只把其中一个放进 .view；三栏两个都放。 */
export class ColumnFrame {
  readonly gridHost: HTMLElement;
  readonly editorHost: HTMLElement;
  mode: 'single' | 'split' = 'single';
  private readonly view: HTMLElement;

  constructor(view: HTMLElement) {
    this.view = view;
    this.gridHost = document.createElement('div');
    this.gridHost.className = 'slot-grid';
    this.editorHost = document.createElement('div');
    this.editorHost.className = 'slot-editor';
  }

  showSingle(which: 'grid' | 'editor'): void {
    this.mode = 'single';
    const host = which === 'grid' ? this.gridHost : this.editorHost;
    const other = which === 'grid' ? this.editorHost : this.gridHost;
    other.remove();
    if (this.view.firstElementChild === host && this.view.childElementCount === 1) return;
    this.view.replaceChildren(host);
  }

  showSplit(): void {
    this.mode = 'split';
    const kids = [...this.view.children];
    if (kids.length === 2 && kids[0] === this.gridHost && kids[1] === this.editorHost) return;
    this.view.replaceChildren(this.gridHost, this.editorHost);
  }

  clear(): void {
    this.mode = 'single';
    this.gridHost.remove();
    this.editorHost.remove();
  }
}

/** 三栏中间那句手势说明。手机上不插入，避免改现有 DOM。 */
export function ensureGestureHint(host: HTMLElement): void {
  const screen = host.querySelector('.screen.editor');
  if (!screen || screen.querySelector('.gesture')) return;
  const hint = document.createElement('p');
  hint.className = 'gesture';
  hint.textContent = GESTURE_HINT;
  const sheet = screen.querySelector('.sheet');
  if (sheet) screen.insertBefore(hint, sheet);
  else screen.append(hint);
}

/** 画布 CSS 像素 × 设备像素比不超过纹理上限。没超就不动样式。 */
export function clampPreviewCanvas(canvas: HTMLCanvasElement, limit = TEXTURE_LIMIT): void {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (!(dpr > 0) || !Number.isFinite(dpr) || (w * dpr <= limit && h * dpr <= limit)) {
    if (canvas.style.maxWidth || canvas.style.maxHeight) {
      canvas.style.maxWidth = '';
      canvas.style.maxHeight = '';
    }
    return;
  }
  const maxCss = Math.max(1, Math.floor(limit / dpr));
  canvas.style.maxWidth = `${maxCss}px`;
  canvas.style.maxHeight = `${maxCss}px`;
}
