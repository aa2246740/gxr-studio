// 舞台上的实时边框与时间戳叠加层（照桌面 `web/assets/stage_finish.js`）：
// GPU 画布与无 GPU 的 `still` 仍显示照片本身；边框与时间戳由浏览器画在同一 `.pv` 容器上的 `.pv-deco` 叠加画布。

import type { Engine, Geometry } from '../../../frontend/src/index';
import { rectFromArray } from '../lib/crop';
import { fontsNeeded, type FontKey } from '../lib/fonts';
import type { Deco, FrameLayoutData, FrameSpec, StampSpec } from '../lib/frame_spec';
import { fontReady } from './font_loader';
import { clearBlurCache, primeBlurFrames } from './finish_bg';
import { drawDecor } from './finish_draw';
import { resolveStageDeco } from './frame_state';
import { logoStore } from './logo_store';
import type { Photo } from './model';

const PV_STYLES = ['width', 'height', 'background', '--fx', '--fy', '--fw', '--fh'] as const;
const MEDIA_STYLES = ['position', 'left', 'top', 'width', 'height'] as const;

const rgb = (c: readonly [number, number, number]): string => `rgb(${c[0]}, ${c[1]}, ${c[2]})`;

function fontsReadyKey(frame: FrameSpec, stamp: StampSpec): string {
  const keys: FontKey[] = fontsNeeded(frame.style !== 'none' ? frame : null, stamp.enabled ? stamp : null);
  return keys.map((k) => `${k}:${fontReady(k) ? 1 : 0}`).join(',');
}

function ensureOverlay(pv: HTMLElement): HTMLCanvasElement {
  for (const c of pv.children) {
    if (c instanceof HTMLCanvasElement && c.classList.contains('pv-deco')) return c;
  }
  const canvas = document.createElement('canvas');
  canvas.className = 'pv-deco';
  canvas.setAttribute('aria-hidden', 'true');
  pv.append(canvas);
  return canvas;
}

/** 去掉叠加装饰与行内定位，恢复无边框时的舞台布局（Port of `stage_finish.js:clearDecor`）。 */
export function clearDecor(pv: HTMLElement, media: HTMLElement | null): void {
  for (const c of [...pv.children]) {
    if (c.classList?.contains('pv-deco')) {
      if (c instanceof HTMLCanvasElement) {
        c.width = 0;
        c.height = 0;
        delete c.dataset.key;
      }
      c.remove();
    }
  }
  pv.classList.remove('framed');
  delete pv.dataset.decor;
  for (const prop of PV_STYLES) pv.style.removeProperty(prop);
  if (media) for (const prop of MEDIA_STYLES) media.style.removeProperty(prop);
}

export interface StageFit {
  cssW: number;
  cssH: number;
  s: number;
  W: number;
  H: number;
}

/** 按舞台可用矩形计算整张成片（照片 + 边框）的展示缩放与像素盒子。 */
export function fitDecoInStage(stage: HTMLElement, deco: Deco): StageFit | null {
  const rect = stage.getBoundingClientRect();
  const maxW = Math.max(0, rect.width);
  const maxH = Math.max(0, rect.height);
  if (maxW <= 0 || maxH <= 0 || deco.width <= 0 || deco.height <= 0) return null;
  const s = Math.min(maxW / deco.width, maxH / deco.height);
  const cssW = deco.ew * s;
  const cssH = deco.eh * s;
  const W = deco.frame ? Math.max(1, Math.round(deco.frame.width * s)) : Math.max(1, Math.round(cssW));
  const H = deco.frame ? Math.max(1, Math.round(deco.frame.height * s)) : Math.max(1, Math.round(cssH));
  return { cssW, cssH, s, W, H };
}

/** 把版面和时间戳叠加画到舞台 `.pv` 上（Port of `stage_finish.js:applyDecor`）。 */
export function applyDecor(
  pv: HTMLElement,
  media: HTMLElement,
  fit: StageFit,
  deco: Deco,
  photo: Photo,
  backdrop: { canvas: HTMLCanvasElement; key: string } | null,
  onRedraw: () => void,
  warmBlurFrames: readonly FrameLayoutData[] = [],
  warmActive: () => boolean = () => false,
  onPrimeCancel?: (cancel: () => void) => void,
): void {
  const { cssW, cssH, s, W, H } = fit;
  const frame = deco.frame;
  pv.dataset.decor = '1';
  if (frame) {
    const fx = Math.round(frame.photo[0] * s);
    const fy = Math.round(frame.photo[1] * s);
    pv.classList.add('framed');
    Object.assign(pv.style, {
      width: `${W}px`,
      height: `${H}px`,
      background: rgb(frame.bg),
    });
    pv.style.setProperty('--fx', `${fx}px`);
    pv.style.setProperty('--fy', `${fy}px`);
    pv.style.setProperty('--fw', `${Math.round(cssW)}px`);
    pv.style.setProperty('--fh', `${Math.round(cssH)}px`);
    Object.assign(media.style, {
      position: 'absolute',
      left: `${fx}px`,
      top: `${fy}px`,
      width: `${Math.round(cssW)}px`,
      height: `${Math.round(cssH)}px`,
    });
  } else {
    pv.classList.remove('framed');
    for (const prop of PV_STYLES) pv.style.removeProperty(prop);
    for (const prop of MEDIA_STYLES) media.style.removeProperty(prop);
    Object.assign(pv.style, {
      width: `${W}px`,
      height: `${H}px`,
    });
  }

  const canvas = ensureOverlay(pv);
  const dpr = globalThis.devicePixelRatio || 1;
  const fontsSig = fontsReadyKey(photo.border, photo.stamp);
  const logoSig = logoStore.revision();
  const key = `${deco.key}|${W}x${H}|${dpr}|${fontsSig}|${logoSig}|${backdrop?.key ?? ''}`;
  if (canvas.dataset.key === key) return;

  if (frame?.bgfx?.kind === 'blur' && backdrop?.canvas) {
    const cancel = primeBlurFrames(backdrop.canvas, [frame, ...warmBlurFrames], warmActive);
    onPrimeCancel?.(cancel);
  }

  canvas.dataset.key = key;
  canvas.width = Math.max(1, Math.round(W * dpr));
  canvas.height = Math.max(1, Math.round(H * dpr));
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  drawDecor(ctx, deco, s * dpr, {
    backdrop: backdrop?.canvas ?? null,
    onLogo: () => {
      delete canvas.dataset.key;
      onRedraw();
    },
  });
}

/** 判断拖调整滑块时叠加层是否依赖底图亮度或模糊背景（其余样式拖动时免重绘叠加层）。 */
export function decorDependsOnLook(deco: Deco | null): boolean {
  const frame = deco?.frame;
  if (!frame) return false;
  if (frame.bgfx?.kind === 'blur') return true;
  return frame.items.some((item) => 'auto_ink' in item && Boolean(item.auto_ink));
}

/** 为模糊背景和边框卡片准备 ≤ 480px 的小底图（优先使用可见的 2D `still` 画布或 `photo.bitmap`）。 */
export function buildBackdropCanvas(
  photo: Photo,
  engine: Engine | null,
  _gpuCanvas: HTMLCanvasElement | null,
  stillCanvas: HTMLCanvasElement | null,
  mode: 'raw' | 'jpg' | 'still',
): { canvas: HTMLCanvasElement; key: string } | null {
  const src: HTMLCanvasElement | ImageBitmap | null =
    stillCanvas && !stillCanvas.hidden && stillCanvas.width > 0 ? stillCanvas : photo.bitmap;
  if (!src) return null;
  if ('closed' in src && (src as { closed?: boolean }).closed) return null;
  const cropRect = src === photo.bitmap && photo.crop ? rectFromArray(photo.crop.rect) : null;
  const sx = cropRect ? Math.round(cropRect.x * src.width) : 0;
  const sy = cropRect ? Math.round(cropRect.y * src.height) : 0;
  const sw = cropRect ? Math.max(1, Math.round(cropRect.w * src.width)) : src.width;
  const sh = cropRect ? Math.max(1, Math.round(cropRect.h * src.height)) : src.height;
  if (sw <= 0 || sh <= 0) return null;
  const scale = Math.min(1, 480 / Math.max(sw, sh));
  const w = Math.max(1, Math.round(sw * scale));
  const h = Math.max(1, Math.round(sh * scale));
  const cv = document.createElement('canvas');
  cv.width = w;
  cv.height = h;
  const ctx = cv.getContext('2d');
  if (!ctx) {
    cv.width = 0;
    cv.height = 0;
    return null;
  }
  try {
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(src, sx, sy, sw, sh, 0, 0, w, h);
  } catch {
    cv.width = 0;
    cv.height = 0;
    return null;
  }
  const sig = `${photo.id}:${photo.version}:${photo.rev}:${mode}:${engine?.contentVersion ?? 0}:${w}x${h}`;
  return { canvas: cv, key: sig };
}

const NEUTRAL_GEOMETRY = {
  straighten: 0,
  rotate90: 0,
  flip: false,
  vertical: 0,
  horizontal: 0,
  distortion: 0,
  box: null,
} as const;

export interface StageDecorHost {
  pv: HTMLElement;
  stage: HTMLElement;
  photo: Photo;
  engine: Engine | null;
  gpuCanvas: HTMLCanvasElement | null;
  still: HTMLCanvasElement | null;
  mode: () => 'raw' | 'jpg' | 'still';
  frameTab?: () => boolean;
  cropTab?: () => boolean;
  geometry?: () => Promise<{ geometry: Geometry; cropped: boolean }>;
  isAlive?: () => boolean;
  onMeta?: () => void;
  onBackdrop?: () => void;
  onDecorClear?: () => void;
  onToast?: (msg: string) => void;
}

/** 协调舞台叠加层重绘、尺寸变化观察、拖动节流与模糊背景缓存。 */
export class StageDecorController {
  private destroyed = false;
  private syncToken = 0;
  private backdropGen = 0;
  private lastDeco: Deco | null = null;
  private fallbackBackdrop: { canvas: HTMLCanvasElement; key: string } | null = null;
  private gpuBackdrop: { canvas: HTMLCanvasElement; key: string } | null = null;
  private gpuThumbBusy = false;
  private gpuThumbAgain = false;
  private dragRaf = 0;
  private cancelBlurPrime: (() => void) | null = null;
  private readonly ro: ResizeObserver;

  constructor(private readonly host: StageDecorHost) {
    this.ro = new ResizeObserver(() => {
      if (this.aliveNow() && (this.host.photo.border.style !== 'none' || this.host.photo.stamp.enabled)) {
        void this.sync();
      }
    });
    this.ro.observe(host.stage);
  }

  private aliveNow(): boolean {
    return !this.destroyed && (this.host.isAlive?.() ?? true);
  }

  private dropBackdrops(): void {
    this.cancelBlurPrime?.();
    this.cancelBlurPrime = null;
    if (this.fallbackBackdrop) {
      clearBlurCache(this.fallbackBackdrop.canvas);
      this.fallbackBackdrop.canvas.width = 0;
      this.fallbackBackdrop.canvas.height = 0;
      this.fallbackBackdrop = null;
    }
    if (this.gpuBackdrop) {
      clearBlurCache(this.gpuBackdrop.canvas);
      this.gpuBackdrop.canvas.width = 0;
      this.gpuBackdrop.canvas.height = 0;
      this.gpuBackdrop = null;
    }
  }

  private needsGpuBackdrop(): boolean {
    if (this.host.frameTab?.()) return true;
    if (this.host.photo.border.style === 'blur_fill') return true;
    return decorDependsOnLook(this.lastDeco);
  }

  backdrop(): { canvas: HTMLCanvasElement; key: string } | null {
    if (!this.aliveNow()) return null;
    if (this.gpuBackdrop) return this.gpuBackdrop;
    if (!this.fallbackBackdrop) {
      const { photo, engine, gpuCanvas, still, mode } = this.host;
      this.fallbackBackdrop = buildBackdropCanvas(photo, engine, gpuCanvas, still, mode());
    }
    return this.fallbackBackdrop;
  }

  backdropCanvas(): HTMLCanvasElement | null {
    return this.backdrop()?.canvas ?? null;
  }

  onFrameTabOpened(): void {
    if (this.aliveNow() && !this.gpuBackdrop) void this.refreshGpuBackdrop();
  }

  onCropChanged(): void {
    this.backdropGen += 1;
    this.dropBackdrops();
    if (!this.aliveNow()) return;
    if (this.needsGpuBackdrop() && !this.host.cropTab?.()) void this.refreshGpuBackdrop();
    if (this.host.photo.border.style !== 'none' || this.host.photo.stamp.enabled) void this.sync();
  }

  onLookChanged(dragging: boolean): void {
    this.backdropGen += 1;
    this.dropBackdrops();
    if (!this.aliveNow()) return;
    const needsBackdrop = this.needsGpuBackdrop();
    const hasDecor = this.host.photo.border.style !== 'none' || this.host.photo.stamp.enabled;
    if (!needsBackdrop && !hasDecor) return;
    if (!dragging) {
      if (needsBackdrop) void this.refreshGpuBackdrop();
      if (this.lastDeco) void this.sync();
      return;
    }
    if (!decorDependsOnLook(this.lastDeco)) return;
    if (this.dragRaf) return;
    this.dragRaf = requestAnimationFrame(() => {
      this.dragRaf = 0;
      if (!this.aliveNow()) return;
      if (this.needsGpuBackdrop()) void this.refreshGpuBackdrop();
      void this.sync();
    });
  }

  onMediaReady(): void {
    this.backdropGen += 1;
    this.dropBackdrops();
    if (!this.aliveNow()) return;
    if (this.needsGpuBackdrop()) void this.refreshGpuBackdrop();
    if (this.host.photo.border.style !== 'none' || this.host.photo.stamp.enabled) void this.sync();
  }

  async sync(): Promise<void> {
    if (!this.aliveNow()) return;
    const token = ++this.syncToken;
    const version = this.host.photo.version;
    const { pv, stage, photo, engine, gpuCanvas, still, mode, onMeta, onToast } = this.host;
    const media = mode() === 'still' || !engine ? still : gpuCanvas;
    if ((photo.border.style === 'none' && !photo.stamp.enabled) || this.host.cropTab?.()) {
      this.lastDeco = null;
      clearDecor(pv, media);
      this.host.onDecorClear?.();
      return;
    }

    const hadMeta = Boolean(photo.frameMeta);
    let deco: Deco | null = null;
    try {
      deco = await resolveStageDeco(
        photo,
        engine,
        () => {
          if (this.aliveNow() && photo.version === version) {
            if (!hadMeta && photo.frameMeta) onMeta?.();
            void this.sync();
          }
        },
        onToast,
        () => this.aliveNow() && token === this.syncToken && photo.version === version,
      );
    } catch {
      return;
    }
    if (!this.aliveNow() || token !== this.syncToken || photo.version !== version) return;
    if (!deco || !media) {
      this.lastDeco = null;
      clearDecor(pv, media);
      this.host.onDecorClear?.();
      return;
    }
    this.lastDeco = deco;
    if (!this.gpuBackdrop && !this.gpuThumbBusy && this.needsGpuBackdrop()) {
      void this.refreshGpuBackdrop();
    }
    const fit = fitDecoInStage(stage, deco);
    if (!fit) return;
    applyDecor(
      pv,
      media,
      fit,
      deco,
      photo,
      this.backdrop(),
      () => {
        if (this.aliveNow() && photo.version === version) void this.sync();
      },
      [],
      () => false,
      (cancel) => {
        this.cancelBlurPrime?.();
        this.cancelBlurPrime = cancel;
      },
    );
  }

  private async refreshGpuBackdrop(): Promise<void> {
    const { engine, photo, mode, onBackdrop } = this.host;
    const m = mode();
    if (!this.aliveNow() || !engine || m === 'still') return;
    if (this.gpuThumbBusy) {
      this.gpuThumbAgain = true;
      return;
    }
    this.gpuThumbBusy = true;
    this.gpuThumbAgain = false;
    const gen = this.backdropGen;
    const version = photo.version;
    const source = m === 'raw' ? `raw:${photo.id}` : `jpg:${photo.id}`;
    try {
      const geometry = this.host.geometry ? (await this.host.geometry()).geometry : NEUTRAL_GEOMETRY;
      if (!this.aliveNow() || gen !== this.backdropGen || photo.version !== version) return;
      const out = await engine.thumb({
        source,
        look: {},
        geometry,
        aspect: null,
        maxEdge: 480,
      });
      if (!this.aliveNow() || gen !== this.backdropGen || photo.version !== version || !out) return;
      const cv = document.createElement('canvas');
      cv.width = out.w;
      cv.height = out.h;
      const ctx = cv.getContext('2d');
      if (!ctx) {
        cv.width = 0;
        cv.height = 0;
        return;
      }
      ctx.putImageData(new ImageData(new Uint8ClampedArray(out.data), out.w, out.h), 0, 0);
      if (this.gpuBackdrop) {
        clearBlurCache(this.gpuBackdrop.canvas);
        this.gpuBackdrop.canvas.width = 0;
        this.gpuBackdrop.canvas.height = 0;
      }
      this.gpuBackdrop = {
        canvas: cv,
        key: `${photo.id}:${photo.version}:${photo.rev}:${engine.contentVersion}:${out.w}x${out.h}`,
      };
      onBackdrop?.();
      if (this.lastDeco) void this.sync();
    } catch {
      // GPU 正忙或设备丢失时沿用 2D 底图。
    } finally {
      this.gpuThumbBusy = false;
      if (this.aliveNow() && this.gpuThumbAgain && this.needsGpuBackdrop()) {
        this.gpuThumbAgain = false;
        void this.refreshGpuBackdrop();
      }
    }
  }

  destroy(): void {
    this.destroyed = true;
    this.syncToken += 1;
    this.backdropGen += 1;
    this.ro.disconnect();
    if (this.dragRaf) cancelAnimationFrame(this.dragRaf);
    this.dragRaf = 0;
    this.dropBackdrops();
    this.lastDeco = null;
    const media = this.host.mode() === 'still' || !this.host.engine ? this.host.still : this.host.gpuCanvas;
    clearDecor(this.host.pv, media);
  }
}
