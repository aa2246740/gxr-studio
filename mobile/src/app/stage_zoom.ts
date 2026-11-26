// 舞台手势与缩放（非裁剪模式）：双指捏合缩放、放大后单指平移（不切照片）、双击复原、单击收起/展开单带、长按看原图、未放大时横滑换片。
// 电脑上（2026-11-26）：Ctrl/⌘ + 滚轮和触控板捏合以指针为中心缩放，放大后滚轮平移，双击在适合和 100% 之间切换，
// + / - / 0 缩放，单击不再收起面板（改按 F），按住 \ 看原图。触屏设备的行为不变。

import { isComputer } from '../lib/save_mode';

const MIN_SCALE = 1;
const MAX_SCALE = 5;
const HOLD_MS = 450;
const DOUBLE_TAP_MS = 300;
const DOUBLE_TAP_PX = 36;
/** 电脑上单击没有要等的动作，双击的间隔放宽到接近系统默认。 */
const DOUBLE_CLICK_MS = 450;
/** 滚轮缩放：倍数 = exp(-deltaY × WHEEL_K)；单次 deltaY 夹在 ±WHEEL_MAX 像素，鼠标一格不会一下跳太多。 */
const WHEEL_K = 0.01;
const WHEEL_MAX = 25;
const LINE_PX = 16;
const KEY_STEP = 1.5;
export const STAGE_DESK_TITLE = '按住照片或按住 \\ 键看原图 · 双击放大到 100%';

export interface StageZoomHooks {
  isCropActive(): boolean;
  onTap(): void;
  onSwipe(dir: -1 | 1): void;
  onHold(on: boolean): void;
  /** 当前照片的像素宽高（双击放大到 100% 用）；不知道时返回 null。 */
  imageSize?(): readonly [number, number] | null;
}

/** Safari 的触控板捏合事件（GestureEvent）：只读用得到的字段。 */
interface GestureLike extends UIEvent {
  scale: number;
  clientX: number;
  clientY: number;
}

let live: StageZoom | null = null;

/** Esc 用：编辑器在全屏看图（面板收起）时退出全屏，返回是否退出了。没有打开的编辑器或不在全屏时什么都不做。 */
export function leaveStageFullView(): boolean {
  return live?.leaveFullView() ?? false;
}

function textTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  if (target instanceof HTMLElement && target.isContentEditable) return true;
  return Boolean(target.closest('input, textarea, select, [role="slider"], .ruler, svg.curve'));
}

interface Pt {
  x: number;
  y: number;
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

export class StageZoom {
  private readonly stage: HTMLElement;
  private readonly pv: HTMLElement;
  private readonly hooks: StageZoomHooks;
  private readonly pointers = new Map<number, Pt>();
  private scale = 1;
  private tx = 0;
  private ty = 0;
  private mode: 'idle' | 'single' | 'pinch' | 'settle' = 'idle';
  private primaryId = -1;
  private startX = 0;
  private startY = 0;
  private startTx = 0;
  private startTy = 0;
  private moved = false;
  private held = false;
  private holdTimer = 0;
  private tapTimer = 0;
  private pinchDist0 = 1;
  private pinchMid0: Pt = { x: 0, y: 0 };
  private pinchScale0 = 1;
  private pinchTx0 = 0;
  private pinchTy0 = 0;
  private lastTapAt = 0;
  private lastTapX = 0;
  private lastTapY = 0;
  private destroyed = false;
  private keyHeld = false;
  private gestureScale0 = 1;

  constructor(stage: HTMLElement, pv: HTMLElement, hooks: StageZoomHooks) {
    this.stage = stage;
    this.pv = pv;
    this.hooks = hooks;
    if (isComputer()) stage.title = STAGE_DESK_TITLE;
    this.apply();
    this.bind();
    live = this;
  }

  zoomScale(): number {
    return this.scale;
  }

  isZoomed(): boolean {
    return this.scale > 1.005;
  }

  reset(): void {
    this.scale = 1;
    this.tx = 0;
    this.ty = 0;
    this.apply();
  }

  /** 以视口坐标 (x, y) 为中心缩放到 `next` 倍：那一点下面的照片位置不动。 */
  zoomAt(next: number, x: number, y: number): void {
    const s0 = this.scale;
    const s1 = clamp(next, MIN_SCALE, MAX_SCALE);
    if (s1 === s0) return;
    const rect = this.pv.getBoundingClientRect();
    // pv 以自己的中心为变换原点：变换后的中心 = 原中心 + 平移。
    const cx = rect.left + rect.width / 2 - this.tx;
    const cy = rect.top + rect.height / 2 - this.ty;
    const ratio = s1 / s0;
    this.scale = s1;
    this.tx = x - cx - (x - cx - this.tx) * ratio;
    this.ty = y - cy - (y - cy - this.ty) * ratio;
    this.apply();
  }

  /** 照片一个像素对一个设备像素时的倍数，夹在允许范围里。 */
  actualScale(): number {
    const size = this.hooks.imageSize?.() ?? null;
    const w = this.pv.clientWidth;
    const h = this.pv.clientHeight;
    if (!size || !(size[0] > 0) || !(size[1] > 0) || !(w > 0) || !(h > 0)) return MAX_SCALE;
    const fit = Math.min(w / size[0], h / size[1]);
    const dpr = window.devicePixelRatio > 0 ? window.devicePixelRatio : 1;
    return clamp(1 / (fit * dpr), MIN_SCALE, MAX_SCALE);
  }

  /** 面板收起（全屏看图）时展开，返回是否展开了。 */
  leaveFullView(): boolean {
    if (this.destroyed || !this.fullView()) return false;
    this.hooks.onTap();
    return true;
  }

  /** `F` 键：收起或展开面板，和手机上单击照片是同一个动作。 */
  toggleFullView(): void {
    if (!this.destroyed && !this.hooks.isCropActive()) this.hooks.onTap();
  }

  private fullView(): boolean {
    return this.stage.closest<HTMLElement>('.screen')?.dataset.collapsed === '1';
  }

  cancel(): void {
    this.clearHoldTimer();
    this.clearTapTimer();
    const holding = this.held || this.keyHeld;
    this.held = this.keyHeld = false;
    if (holding) this.hooks.onHold(false);
    this.pointers.clear();
    this.mode = 'idle';
    this.primaryId = -1;
    this.moved = false;
  }

  destroy(): void {
    this.destroyed = true;
    this.cancel();
    this.stage.removeEventListener('pointerdown', this.onDown);
    this.stage.removeEventListener('pointermove', this.onMove);
    this.stage.removeEventListener('pointerup', this.onEnd);
    this.stage.removeEventListener('pointercancel', this.onEnd);
    this.stage.removeEventListener('wheel', this.onWheel);
    this.stage.removeEventListener('gesturestart', this.onGesture);
    this.stage.removeEventListener('gesturechange', this.onGesture);
    this.stage.removeEventListener('gestureend', this.onGesture);
    document.removeEventListener('keydown', this.onKeyDown);
    document.removeEventListener('keyup', this.onKeyUp);
    window.removeEventListener('blur', this.onBlur);
    if (live === this) live = null;
  }

  private bind(): void {
    this.stage.addEventListener('pointerdown', this.onDown);
    this.stage.addEventListener('pointermove', this.onMove);
    this.stage.addEventListener('pointerup', this.onEnd);
    this.stage.addEventListener('pointercancel', this.onEnd);
    this.stage.addEventListener('wheel', this.onWheel, { passive: false });
    this.stage.addEventListener('gesturestart', this.onGesture);
    this.stage.addEventListener('gesturechange', this.onGesture);
    this.stage.addEventListener('gestureend', this.onGesture);
    document.addEventListener('keydown', this.onKeyDown);
    document.addEventListener('keyup', this.onKeyUp);
    window.addEventListener('blur', this.onBlur);
  }

  private releaseKeyHold(): void {
    if (!this.keyHeld) return;
    this.keyHeld = false;
    if (!this.held) this.hooks.onHold(false);
  }

  /** 舞台中心（视口坐标）：键盘缩放以它为中心。 */
  private stageCenter(): Pt {
    const rect = this.stage.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  }

  private readonly onWheel = (ev: WheelEvent): void => {
    if (this.destroyed || this.hooks.isCropActive()) return;
    const unit = ev.deltaMode === 1 ? LINE_PX : ev.deltaMode === 2 ? this.stage.clientHeight || 400 : 1;
    const dx = ev.deltaX * unit;
    const dy = ev.deltaY * unit;
    if (ev.ctrlKey || ev.metaKey) {
      // Chrome / Edge 把触控板捏合发成带 ctrlKey 的 wheel：拦下，免得浏览器缩放整页。
      ev.preventDefault();
      this.zoomAt(this.scale * Math.exp(-clamp(dy, -WHEEL_MAX, WHEEL_MAX) * WHEEL_K), ev.clientX, ev.clientY);
      return;
    }
    if (!this.isZoomed() || this.pointers.size > 0) return;
    ev.preventDefault();
    this.tx -= dx;
    this.ty -= dy;
    this.apply();
  };

  private readonly onGesture = (raw: Event): void => {
    // Safari 的触控板捏合。触屏的双指捏合走指针事件，这里只管没有触摸点的设备，且没有手指按着时。
    if (this.destroyed || this.hooks.isCropActive() || navigator.maxTouchPoints !== 0 || this.pointers.size > 0) return;
    const ev = raw as GestureLike;
    raw.preventDefault();
    if (raw.type === 'gesturestart') {
      this.gestureScale0 = this.scale;
      return;
    }
    if (raw.type === 'gestureend' || !(ev.scale > 0)) return;
    this.zoomAt(this.gestureScale0 * ev.scale, ev.clientX, ev.clientY);
  };

  private readonly onKeyDown = (ev: KeyboardEvent): void => {
    if (this.destroyed || ev.defaultPrevented || !this.stage.isConnected || this.hooks.isCropActive()) return;
    if (ev.ctrlKey || ev.metaKey || ev.altKey || textTarget(ev.target)) return;
    const key = ev.key;
    if (key === '\\' || ev.code === 'Backslash') {
      if (!isComputer()) return;
      ev.preventDefault();
      if (ev.repeat || this.keyHeld) return;
      this.keyHeld = true;
      if (!this.held) this.hooks.onHold(true);
      return;
    }
    if (key === 'f' || key === 'F') {
      if (!isComputer() || ev.repeat) return;
      ev.preventDefault();
      this.toggleFullView();
      return;
    }
    const c = this.stageCenter();
    if (key === '+' || key === '=') this.zoomAt(this.scale * KEY_STEP, c.x, c.y);
    else if (key === '-' || key === '_') this.zoomAt(this.scale / KEY_STEP, c.x, c.y);
    else if (key === '0') this.reset();
    else return;
    ev.preventDefault();
  };

  private readonly onKeyUp = (ev: KeyboardEvent): void => {
    if (ev.key === '\\' || ev.code === 'Backslash') this.releaseKeyHold();
  };

  private readonly onBlur = (): void => {
    this.releaseKeyHold();
  };

  private clearHoldTimer(): void {
    if (this.holdTimer) {
      window.clearTimeout(this.holdTimer);
      this.holdTimer = 0;
    }
  }

  private clearTapTimer(): void {
    if (this.tapTimer) {
      window.clearTimeout(this.tapTimer);
      this.tapTimer = 0;
    }
  }

  private apply(): void {
    const rect = this.stage.getBoundingClientRect();
    const maxX = Math.max(0, (rect.width * (this.scale - 1)) / 2);
    const maxY = Math.max(0, (rect.height * (this.scale - 1)) / 2);
    if (this.scale <= 1.005) {
      this.scale = 1;
      this.tx = 0;
      this.ty = 0;
      this.pv.style.transform = '';
      this.stage.dataset.zoom = '1';
      return;
    }
    this.tx = clamp(this.tx, -maxX, maxX);
    this.ty = clamp(this.ty, -maxY, maxY);
    this.pv.style.transform = `translate3d(${this.tx.toFixed(1)}px, ${this.ty.toFixed(1)}px, 0) scale(${this.scale.toFixed(4)})`;
    this.stage.dataset.zoom = this.scale.toFixed(2);
  }

  private startPinch(): void {
    const pts = [...this.pointers.values()];
    if (pts.length < 2) return;
    this.clearHoldTimer();
    this.clearTapTimer();
    if (this.held) {
      this.held = false;
      if (!this.keyHeld) this.hooks.onHold(false);
    }
    this.mode = 'pinch';
    this.primaryId = -1;
    this.moved = true;
    this.pinchDist0 = Math.max(10, Math.hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y));
    this.pinchMid0 = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
    this.pinchScale0 = this.scale;
    this.pinchTx0 = this.tx;
    this.pinchTy0 = this.ty;
  }

  private readonly onDown = (ev: PointerEvent): void => {
    if (this.destroyed || this.hooks.isCropActive()) return;
    if (ev.button !== 0 && ev.pointerType === 'mouse') return;
    if (this.pointers.size === 0 && ev.clientX < 24) return;
    this.clearTapTimer();
    this.pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    try {
      this.stage.setPointerCapture(ev.pointerId);
    } catch {
      /* ignore */
    }
    if (this.pointers.size >= 2) {
      this.startPinch();
      return;
    }
    this.mode = 'single';
    this.primaryId = ev.pointerId;
    this.startX = ev.clientX;
    this.startY = ev.clientY;
    this.startTx = this.tx;
    this.startTy = this.ty;
    this.moved = false;
    this.held = false;
    this.clearHoldTimer();
    this.holdTimer = window.setTimeout(() => {
      this.holdTimer = 0;
      if (this.destroyed || this.moved || this.mode !== 'single' || this.hooks.isCropActive()) return;
      this.held = true;
      if (!this.keyHeld) this.hooks.onHold(true);
    }, HOLD_MS);
  };

  private readonly onMove = (ev: PointerEvent): void => {
    if (this.destroyed || this.hooks.isCropActive()) return;
    if (!this.pointers.has(ev.pointerId)) return;
    this.pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });

    if (this.mode === 'pinch' && this.pointers.size >= 2) {
      const pts = [...this.pointers.values()];
      const dist = Math.max(10, Math.hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y));
      const mid = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
      const rect = this.stage.getBoundingClientRect();
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      const nextScale = clamp(this.pinchScale0 * (dist / this.pinchDist0), MIN_SCALE, MAX_SCALE);
      const ratio = nextScale / this.pinchScale0;
      this.scale = nextScale;
      this.tx = mid.x - cx - (this.pinchMid0.x - cx - this.pinchTx0) * ratio;
      this.ty = mid.y - cy - (this.pinchMid0.y - cy - this.pinchTy0) * ratio;
      this.apply();
      return;
    }

    if (this.mode !== 'single' || ev.pointerId !== this.primaryId) return;
    const dx = ev.clientX - this.startX;
    const dy = ev.clientY - this.startY;
    if (Math.hypot(dx, dy) > 8) {
      this.moved = true;
      this.clearHoldTimer();
      this.clearTapTimer();
    }
    if (this.isZoomed() && !this.held && this.moved) {
      this.tx = this.startTx + dx;
      this.ty = this.startTy + dy;
      this.apply();
    }
  };

  private readonly onEnd = (ev: PointerEvent): void => {
    const last = this.pointers.get(ev.pointerId);
    if (!last) return;
    this.pointers.delete(ev.pointerId);
    if (this.stage.hasPointerCapture?.(ev.pointerId)) {
      try {
        this.stage.releasePointerCapture(ev.pointerId);
      } catch {
        /* ignore */
      }
    }
    if (this.destroyed || this.hooks.isCropActive()) {
      this.cancel();
      return;
    }

    if (this.mode === 'pinch' || this.mode === 'settle') {
      if (this.scale < 1.04) this.reset();
      this.mode = this.pointers.size === 0 ? 'idle' : 'settle';
      return;
    }

    if (ev.pointerId !== this.primaryId) return;
    this.primaryId = -1;
    this.mode = 'idle';
    this.clearHoldTimer();

    const endX = ev.clientX === 0 && ev.clientY === 0 ? last.x : ev.clientX;
    const endY = ev.clientX === 0 && ev.clientY === 0 ? last.y : ev.clientY;
    const dx = endX - this.startX;
    const dy = endY - this.startY;
    if (Math.hypot(dx, dy) > 12) this.moved = true;

    const swiped = ev.type === 'pointerup' && !this.isZoomed() && this.moved && Math.abs(dx) > 56 && Math.abs(dx) > Math.abs(dy);
    if (this.held) {
      this.held = false;
      if (!this.keyHeld) this.hooks.onHold(false);
      if (!swiped) return;
    }
    if (ev.type === 'pointercancel') {
      this.clearTapTimer();
      return;
    }

    if (swiped) {
      this.clearTapTimer();
      this.hooks.onSwipe(dx < 0 ? 1 : -1);
      return;
    }

    if (!this.moved) {
      const now = performance.now();
      const mouse = ev.pointerType === 'mouse';
      const computer = isComputer();
      const windowMs = mouse && computer ? DOUBLE_CLICK_MS : DOUBLE_TAP_MS;
      const isDouble = now - this.lastTapAt < windowMs && Math.hypot(ev.clientX - this.lastTapX, ev.clientY - this.lastTapY) < DOUBLE_TAP_PX;
      if (isDouble) {
        this.clearTapTimer();
        this.lastTapAt = 0;
        // 鼠标双击：适合 ↔ 100%（以点击处为中心）；触屏双击仍只复原。
        if (this.isZoomed()) this.reset();
        else if (mouse) this.zoomAt(this.actualScale(), ev.clientX, ev.clientY);
        return;
      }
      this.lastTapAt = now;
      this.lastTapX = ev.clientX;
      this.lastTapY = ev.clientY;
      // 电脑上单击照片不收起面板（改按 F）。
      if (!this.isZoomed() && !computer) {
        this.clearTapTimer();
        this.tapTimer = window.setTimeout(() => {
          this.tapTimer = 0;
          if (!this.destroyed && !this.hooks.isCropActive() && !this.isZoomed()) {
            this.hooks.onTap();
          }
        }, DOUBLE_TAP_MS);
      }
    }
  };
}
