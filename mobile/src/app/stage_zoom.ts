// 舞台手势与缩放（非裁剪模式）：双指捏合缩放、放大后单指平移（不切照片）、双击复原、单击收起/展开单带、长按看原图、未放大时横滑换片。

const MIN_SCALE = 1;
const MAX_SCALE = 5;
const HOLD_MS = 450;
const DOUBLE_TAP_MS = 300;
const DOUBLE_TAP_PX = 36;

export interface StageZoomHooks {
  isCropActive(): boolean;
  onTap(): void;
  onSwipe(dir: -1 | 1): void;
  onHold(on: boolean): void;
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
  private pinchDist0 = 1;
  private pinchMid0: Pt = { x: 0, y: 0 };
  private pinchScale0 = 1;
  private pinchTx0 = 0;
  private pinchTy0 = 0;
  private lastTapAt = 0;
  private lastTapX = 0;
  private lastTapY = 0;
  private destroyed = false;

  constructor(stage: HTMLElement, pv: HTMLElement, hooks: StageZoomHooks) {
    this.stage = stage;
    this.pv = pv;
    this.hooks = hooks;
    this.apply();
    this.bind();
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

  cancel(): void {
    this.clearHoldTimer();
    if (this.held) {
      this.held = false;
      this.hooks.onHold(false);
    }
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
  }

  private bind(): void {
    this.stage.addEventListener('pointerdown', this.onDown);
    this.stage.addEventListener('pointermove', this.onMove);
    this.stage.addEventListener('pointerup', this.onEnd);
    this.stage.addEventListener('pointercancel', this.onEnd);
  }

  private clearHoldTimer(): void {
    if (this.holdTimer) {
      window.clearTimeout(this.holdTimer);
      this.holdTimer = 0;
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
    if (this.held) {
      this.held = false;
      this.hooks.onHold(false);
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
      this.hooks.onHold(true);
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
      this.hooks.onHold(false);
      if (!swiped) return;
    }
    if (ev.type === 'pointercancel') return;

    if (swiped) {
      this.hooks.onSwipe(dx < 0 ? 1 : -1);
      return;
    }

    if (!this.moved) {
      const now = performance.now();
      const isDouble = now - this.lastTapAt < DOUBLE_TAP_MS && Math.hypot(ev.clientX - this.lastTapX, ev.clientY - this.lastTapY) < DOUBLE_TAP_PX;
      if (isDouble) {
        this.lastTapAt = 0;
        if (this.isZoomed()) {
          this.reset();
          return;
        }
      }
      this.lastTapAt = now;
      this.lastTapX = ev.clientX;
      this.lastTapY = ev.clientY;
      if (!this.isZoomed()) {
        this.hooks.onTap();
      }
    }
  };
}
