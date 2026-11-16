// 裁剪手势状态机：拖框移动、拖角/边缩放（锁定比例或自由）、双指捏合缩放。纯函数/纯类，不碰 DOM，node:test 直接测。

import {
  clamp,
  fitStraightened,
  insideStraightened,
  lockRatio,
  moveRect,
  rectFromArray,
  rectToArray,
  resizeRect,
  type CanvasDims,
  type CropState,
  type Handle,
  type Rect,
} from './crop';

const MIN_SIDE = 0.02;

export interface Point {
  x: number;
  y: number;
}

export type GestureMode = 'idle' | 'move' | 'resize' | 'pinch';

/**
 * 以拖拽时固定的锚点为中心做二分收缩，保证缩放时对角/对边不漂移，同时不超出拉直后的原画面、不破坏锁定比例。
 * 锚点本身若已越界（极少见，如刚拉直完尚未重收），退回 `fitStraightened`。
 */
function clampResized(start: Rect, raw: Rect, handle: Handle, canvas: CanvasDims, angleDeg: number): Rect {
  if (insideStraightened(raw, canvas, angleDeg)) return raw;
  const anchorAt = (k: number): Rect => {
    const w = raw.w * k;
    const h = raw.h * k;
    if (handle === 'l') return { x: start.x + start.w - w, y: start.y + start.h / 2 - h / 2, w, h };
    if (handle === 'r') return { x: start.x, y: start.y + start.h / 2 - h / 2, w, h };
    if (handle === 't') return { x: start.x + start.w / 2 - w / 2, y: start.y + start.h - h, w, h };
    if (handle === 'b') return { x: start.x + start.w / 2 - w / 2, y: start.y, w, h };
    const left = handle.includes('l');
    const top = handle.includes('t');
    const ax = left ? start.x + start.w : start.x;
    const ay = top ? start.y + start.h : start.y;
    return { x: left ? ax - w : ax, y: top ? ay - h : ay, w, h };
  };
  const minK = Math.max(MIN_SIDE / Math.max(1e-9, raw.w), MIN_SIDE / Math.max(1e-9, raw.h));
  if (minK <= 1 && insideStraightened(anchorAt(minK), canvas, angleDeg)) {
    let lo = minK;
    let hi = 1;
    for (let i = 0; i < 28; i += 1) {
      const mid = (lo + hi) / 2;
      if (insideStraightened(anchorAt(mid), canvas, angleDeg)) lo = mid;
      else hi = mid;
    }
    return anchorAt(lo);
  }
  return fitStraightened(raw, canvas, angleDeg).rect;
}

export class CropGesture {
  private currentMode: GestureMode = 'idle';
  private startRect: Rect = { x: 0, y: 0, w: 1, h: 1 };
  private rect: Rect = { x: 0, y: 0, w: 1, h: 1 };
  private handle: Handle | null = null;
  private lock: number | null = null;
  private startPointer: Point = { x: 0, y: 0 };
  private startDist = 0;

  get mode(): GestureMode {
    return this.currentMode;
  }

  get current(): Rect {
    return { ...this.rect };
  }

  startMove(pointer: Point, crop: CropState | null, canvas: CanvasDims): void {
    const start = crop ? rectFromArray(crop.rect) : { x: 0, y: 0, w: 1, h: 1 };
    this.currentMode = 'move';
    this.startRect = start;
    this.rect = { ...start };
    this.handle = null;
    this.lock = null;
    this.startPointer = { ...pointer };
    void canvas;
  }

  startResize(handle: Handle, pointer: Point, crop: CropState | null, canvas: CanvasDims, preset: string): void {
    const start = crop ? rectFromArray(crop.rect) : { x: 0, y: 0, w: 1, h: 1 };
    this.currentMode = 'resize';
    this.startRect = start;
    this.rect = { ...start };
    this.handle = handle;
    this.lock = lockRatio(preset, start, canvas);
    this.startPointer = { ...pointer };
  }

  startPinch(p1: Point, p2: Point, crop: CropState | null, canvas: CanvasDims, preset: string): void {
    const start = crop ? rectFromArray(crop.rect) : { x: 0, y: 0, w: 1, h: 1 };
    this.currentMode = 'pinch';
    this.startRect = start;
    this.rect = { ...start };
    this.handle = null;
    this.lock = lockRatio(preset, start, canvas);
    this.startDist = Math.max(1e-4, Math.hypot(p2.x - p1.x, p2.y - p1.y));
  }

  move(pointer: Point, canvas: CanvasDims, angleDeg: number): Rect {
    if (this.currentMode === 'move') {
      const next = moveRect(this.startRect, pointer.x - this.startPointer.x, pointer.y - this.startPointer.y);
      this.rect = fitStraightened(next, canvas, angleDeg).rect;
      return { ...this.rect };
    }
    if (this.currentMode === 'resize' && this.handle) {
      const raw = resizeRect(this.startRect, this.handle, pointer, canvas, this.lock);
      this.rect = clampResized(this.startRect, raw, this.handle, canvas, angleDeg);
      return { ...this.rect };
    }
    return { ...this.rect };
  }

  pinch(p1: Point, p2: Point, canvas: CanvasDims, angleDeg: number): Rect {
    if (this.currentMode !== 'pinch') return { ...this.rect };
    const dist = Math.max(1e-4, Math.hypot(p2.x - p1.x, p2.y - p1.y));
    const scale = clamp(dist / this.startDist, 0.1, 8);
    const minScale = Math.max(MIN_SIDE / Math.max(1e-9, this.startRect.w), MIN_SIDE / Math.max(1e-9, this.startRect.h));
    const factor = Math.max(minScale, scale);
    const w = this.startRect.w * factor;
    const h = this.startRect.h * factor;
    const cx = this.startRect.x + this.startRect.w / 2;
    const cy = this.startRect.y + this.startRect.h / 2;
    this.rect = fitStraightened({ x: cx - w / 2, y: cy - h / 2, w, h }, canvas, angleDeg).rect;
    return { ...this.rect };
  }

  end(): [number, number, number, number] | null {
    if (this.currentMode === 'idle') return null;
    const out = rectToArray(this.rect);
    this.currentMode = 'idle';
    this.handle = null;
    return out;
  }

  cancel(): void {
    this.currentMode = 'idle';
    this.handle = null;
  }
}
