// 曲线页。点的模型和手势跟桌面 curve_editor.js 一样：端点只能上下动，中间点夹在相邻点之间，
// 最多 16 个点，横坐标不能重复，双击删中间点。拖动中的草稿写在照片上，预览按帧走；松手才加 rev。
// 触控热区按大约 22px 算，比桌面的 14（曲线坐标）大；点的取舍、上限和端点规则不变。

import { button, el } from './dom';
import {
  CURVE_LABELS,
  curveJson,
  type CurveChannel,
  type CurveMap,
  type CurvePoint,
} from '../lib/export_request';
import type { Photo } from './model';

export const CURVE_MAX = 16;
export const DESKTOP_HIT = 14;
export const IDENTITY_CURVE: readonly CurvePoint[] = [
  [0, 0],
  [255, 255],
];
const FINGER_PX = 22;
const SVG_NS = 'http://www.w3.org/2000/svg';
const COLORS: Record<CurveChannel, string> = {
  master: '#ececec',
  r: '#ff7a70',
  g: '#5cc882',
  b: '#80b6ff',
};

export function touchHit(width: number): number {
  if (!(width > 0)) return DESKTOP_HIT;
  return Math.max(DESKTOP_HIT, (FINGER_PX / width) * 255);
}

export function channelPoints(curve: CurveMap | null | undefined, channel: CurveChannel): CurvePoint[] {
  const src = curve?.[channel] ?? IDENTITY_CURVE;
  return src.map((p) => [p[0], p[1]]);
}

export function toCurvePoint(
  clientX: number,
  clientY: number,
  rect: { left: number; top: number; width: number; height: number },
): CurvePoint | null {
  if (!(rect.width > 0) || !(rect.height > 0)) return null;
  const x = ((clientX - rect.left) / rect.width) * 255;
  const y = 255 - ((clientY - rect.top) / rect.height) * 255;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return [Math.round(Math.min(255, Math.max(0, x))), Math.round(Math.min(255, Math.max(0, y)))];
}

export function nearestIndex(points: readonly CurvePoint[], p: CurvePoint, hit: number): number {
  let best = -1;
  let bestD = hit;
  points.forEach(([px, py], i) => {
    const d = Math.hypot(px - p[0], py - p[1]);
    if (d <= bestD) {
      best = i;
      bestD = d;
    }
  });
  return best;
}

/** 插到按横坐标排好的位置。满了、或横坐标已经有点，就不动。 */
export function tryInsert(points: CurvePoint[], p: CurvePoint): number | null {
  if (points.length >= CURVE_MAX || points.some(([x]) => x === p[0])) return null;
  let i = points.findIndex(([x]) => x > p[0]);
  if (i < 0) i = points.length;
  points.splice(i, 0, [p[0], p[1]]);
  return i;
}

export function clampMoved(points: readonly CurvePoint[], i: number, x: number, y: number): CurvePoint {
  const last = points.length - 1;
  const lo = i === 0 ? 0 : points[i - 1][0] + 1;
  const hi = i === last ? 255 : points[i + 1][0] - 1;
  const nx = i === 0 || i === last ? points[i][0] : Math.min(hi, Math.max(lo, x));
  const ny = Math.min(255, Math.max(0, y));
  return [nx, ny];
}

export function setChannel(curve: CurveMap | null | undefined, channel: CurveChannel, points: readonly CurvePoint[]): CurveMap | null {
  return curveJson({ ...(curve ?? {}), [channel]: points.map((p) => [p[0], p[1]]) });
}

/** 交给 setLook 的曲线。按住看原图时不带曲线；恒等通道省略，全恒等是 null。 */
export function previewCurve(curve: CurveMap | null | undefined, holding: boolean): CurveMap | null {
  if (holding) return null;
  return curveJson(curve);
}

export interface CurvesHost {
  photo(): Photo | null;
  push(): void;
  enterDrag(): void;
  leaveDrag(): void;
  dragging(): boolean;
}

function svgEl(tag: string, attrs: Record<string, string | number>): SVGElement {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}

function sameCurve(a: CurveMap | null | undefined, b: CurveMap | null | undefined): boolean {
  return JSON.stringify(curveJson(a)) === JSON.stringify(curveJson(b));
}

interface Drag {
  pointerId: number;
  i: number;
  dx: number;
  dy: number;
  added: boolean;
  moved: boolean;
}

export class CurvesView {
  private channel: CurveChannel = 'master';
  private readonly host: CurvesHost;
  private readonly panel: HTMLElement;
  private readonly svg: SVGSVGElement;
  private readonly buttons: HTMLButtonElement[] = [];
  private readonly resetOne: HTMLButtonElement;
  private readonly resetAll: HTMLButtonElement;
  private readonly count: HTMLElement;
  private drag: Drag | null = null;
  private working: CurvePoint[] | null = null;
  private dragPhoto: Photo | null = null;
  private committed: CurveMap | null = null;
  private preview: CurveMap | null = null;
  private frame = 0;
  private closed = false;

  constructor(host: CurvesHost) {
    this.host = host;
    this.panel = el('div', 'panel curve-panel');
    const row = el('div', 'row curves');
    const column = el('div', 'col');
    const switches = el('div', 'curve-channels');
    switches.setAttribute('role', 'group');
    switches.setAttribute('aria-label', '曲线通道');
    for (const [key, label] of CURVE_LABELS) {
      const item = button(key === 'master' ? 'curve-btn on' : 'curve-btn', label);
      item.setAttribute('data-channel', key);
      item.style.color = COLORS[key];
      item.addEventListener('click', () => {
        if (this.drag || this.closed) return;
        this.channel = key;
        this.draw();
      });
      this.buttons.push(item);
      switches.append(item);
    }
    this.resetOne = button('btn', '重置此通道');
    this.resetAll = button('btn', '重置全部曲线');
    this.resetOne.addEventListener('click', () => this.reset(false));
    this.resetAll.addEventListener('click', () => this.reset(true));
    const actions = el('div', 'curve-actions');
    actions.append(this.resetOne, this.resetAll);
    this.count = el('span', 'small dim curve-count', `2 / ${CURVE_MAX}`);
    column.append(switches, actions, this.count);
    this.svg = document.createElementNS(SVG_NS, 'svg');
    this.svg.setAttribute('viewBox', '0 0 255 255');
    this.svg.setAttribute('class', 'curve');
    this.svg.setAttribute('role', 'group');
    this.svg.style.touchAction = 'none';
    row.append(column, this.svg);
    this.panel.append(row);
    this.svg.addEventListener('pointerdown', this.onDown, { passive: false });
    this.svg.addEventListener('pointermove', this.onMove);
    this.svg.addEventListener('pointerup', this.onUp);
    this.svg.addEventListener('pointercancel', this.onCancel);
    this.svg.addEventListener('lostpointercapture', this.onLost);
    this.svg.addEventListener('dblclick', this.onDouble);
    this.draw();
  }

  panelEl(): HTMLElement {
    return this.panel;
  }

  /** 切走曲线、换照片或拆掉视图时，没有 pointerup 也要把草稿退回。 */
  cancelDrag(): void {
    if (this.closed || !this.drag) return;
    this.finish(true);
  }

  destroy(): void {
    if (this.closed) return;
    this.cancelDrag();
    this.closed = true;
    this.svg.removeEventListener('pointerdown', this.onDown);
    this.svg.removeEventListener('pointermove', this.onMove);
    this.svg.removeEventListener('pointerup', this.onUp);
    this.svg.removeEventListener('pointercancel', this.onCancel);
    this.svg.removeEventListener('lostpointercapture', this.onLost);
    this.svg.removeEventListener('dblclick', this.onDouble);
  }

  private owns(photo: Photo): boolean {
    return sameCurve(photo.curve, this.preview);
  }

  private releaseCapture(pointerId: number): void {
    try {
      if (this.svg.hasPointerCapture?.(pointerId)) this.svg.releasePointerCapture(pointerId);
    } catch {
      /* 测试替身没有指针捕获。 */
    }
  }

  private onDown = (e: PointerEvent): void => {
    if (this.closed || this.drag || e.button !== 0) return;
    const photo = this.host.photo();
    const rect = this.svg.getBoundingClientRect();
    const p = toCurvePoint(e.clientX, e.clientY, rect);
    if (!photo || !p) return;
    const committed = curveJson(photo.curve);
    const working = channelPoints(committed, this.channel);
    let i = nearestIndex(working, p, touchHit(rect.width));
    let added = false;
    let dx = 0;
    let dy = 0;
    if (i < 0) {
      const at = tryInsert(working, p);
      if (at === null) return;
      i = at;
      added = true;
    } else {
      dx = working[i][0] - p[0];
      dy = working[i][1] - p[1];
    }
    this.committed = committed;
    this.preview = committed;
    this.working = working;
    this.dragPhoto = photo;
    this.drag = { pointerId: e.pointerId, i, dx, dy, added, moved: false };
    this.host.enterDrag();
    if (added) this.write(setChannel(committed, this.channel, working));
    else this.draw();
    try {
      this.svg.setPointerCapture(e.pointerId);
    } catch {
      /* 测试替身没有指针捕获。 */
    }
    e.preventDefault();
  };

  private onMove = (e: PointerEvent): void => {
    if (this.closed || !this.drag || !this.working || !this.dragPhoto || e.pointerId !== this.drag.pointerId) return;
    const rect = this.svg.getBoundingClientRect();
    if (!(rect.width > 0) || !(rect.height > 0)) return;
    const fx = ((e.clientX - rect.left) / rect.width) * 255;
    const fy = 255 - ((e.clientY - rect.top) / rect.height) * 255;
    if (!Number.isFinite(fx) || !Number.isFinite(fy)) return;
    const i = this.drag.i;
    const next = clampMoved(this.working, i, Math.round(fx + this.drag.dx), Math.round(fy + this.drag.dy));
    const cur = this.working[i];
    if (next[0] === cur[0] && next[1] === cur[1]) return;
    this.drag.moved = true;
    this.working[i] = next;
    this.write(setChannel(this.dragPhoto.curve, this.channel, this.working));
  };

  private onUp = (e: PointerEvent): void => {
    if (!this.drag || e.pointerId !== this.drag.pointerId) return;
    this.finish(false);
  };

  private onCancel = (e: PointerEvent): void => {
    if (!this.drag || e.pointerId !== this.drag.pointerId) return;
    this.cancelDrag();
  };

  private onLost = (e: PointerEvent): void => {
    if (!this.drag || e.pointerId !== this.drag.pointerId) return;
    this.cancelDrag();
  };

  private onDouble = (e: MouseEvent): void => {
    if (this.closed || this.drag) return;
    const photo = this.host.photo();
    const p = toCurvePoint(e.clientX, e.clientY, this.svg.getBoundingClientRect());
    if (!photo || !p) return;
    const list = channelPoints(photo.curve, this.channel);
    const i = nearestIndex(list, p, touchHit(this.svg.getBoundingClientRect().width));
    if (i <= 0 || i >= list.length - 1 || list.length <= 2) return;
    list.splice(i, 1);
    this.commit(setChannel(photo.curve, this.channel, list));
  };

  private reset(all: boolean): void {
    if (this.drag || this.closed) return;
    const photo = this.host.photo();
    if (!photo) return;
    this.commit(all ? null : setChannel(photo.curve, this.channel, IDENTITY_CURVE));
  }

  private write(next: CurveMap | null): void {
    const photo = this.dragPhoto;
    if (!photo || this.closed) return;
    photo.curve = next;
    this.preview = next;
    this.draw();
    this.queuePreview(photo);
  }

  private commit(next: CurveMap | null): void {
    const photo = this.host.photo();
    if (!photo || this.closed) return;
    const before = curveJson(photo.curve);
    const after = curveJson(next);
    photo.curve = after;
    if (!sameCurve(before, after)) photo.rev += 1;
    this.draw();
    this.host.push();
  }

  private finish(cancel: boolean): void {
    if (!this.drag || !this.dragPhoto) return;
    const photo = this.dragPhoto;
    const pointerId = this.drag.pointerId;
    const changed = this.drag.added || this.drag.moved;
    this.drag = null;
    this.working = null;
    this.dragPhoto = null;
    this.cancelFrame();
    this.releaseCapture(pointerId);
    const current = this.host.photo() === photo;
    if (!current || cancel || !changed) {
      if (this.owns(photo)) photo.curve = this.committed;
    } else if (this.owns(photo)) {
      photo.rev += 1;
    }
    this.draw();
    if (current) this.host.push();
    if (this.host.dragging()) this.host.leaveDrag();
  }

  private queuePreview(photo: Photo): void {
    if (this.frame || this.closed) return;
    const run = (): void => {
      this.frame = 0;
      if (this.closed || this.host.photo() !== photo) return;
      this.host.push();
    };
    const raf = globalThis.requestAnimationFrame;
    if (typeof raf === 'function') this.frame = raf(run);
    else run();
  }

  private cancelFrame(): void {
    if (!this.frame) return;
    globalThis.cancelAnimationFrame?.(this.frame);
    this.frame = 0;
  }

  private draw(): void {
    if (this.closed) return;
    const photo = this.dragPhoto ?? this.host.photo();
    const list = this.working ?? channelPoints(photo?.curve, this.channel);
    const label = CURVE_LABELS.find(([key]) => key === this.channel)?.[1] ?? '曲线';
    this.svg.setAttribute('data-channel', this.channel);
    this.svg.setAttribute('aria-label', this.channel === 'master' ? label : `${label}曲线`);
    for (const btn of this.buttons) {
      const key = btn.getAttribute('data-channel') as CurveChannel;
      const on = key === this.channel;
      const changed = Boolean(photo?.curve?.[key]);
      btn.classList.toggle('on', on);
      btn.setAttribute('aria-pressed', String(on));
      btn.setAttribute('data-changed', String(changed));
      btn.title = changed ? '已调整' : '未调整';
    }
    this.resetOne.disabled = !photo?.curve?.[this.channel];
    this.resetAll.disabled = !photo?.curve;
    const nodes: SVGElement[] = [
      svgEl('rect', { x: 0, y: 0, width: 255, height: 255, class: 'curve-bg' }),
      svgEl('line', { x1: 0, y1: 255, x2: 255, y2: 0, class: 'curve-diag' }),
    ];
    for (let i = 1; i < 4; i += 1) {
      const g = (255 * i) / 4;
      nodes.push(svgEl('line', { x1: g, y1: 0, x2: g, y2: 255, class: 'curve-grid' }));
      nodes.push(svgEl('line', { x1: 0, y1: g, x2: 255, y2: g, class: 'curve-grid' }));
    }
    for (const [key] of CURVE_LABELS) {
      const pts = photo?.curve?.[key];
      if (key === this.channel || !pts) continue;
      nodes.push(this.line(pts, key, true));
    }
    nodes.push(this.line(list, this.channel, false));
    list.forEach(([x, y], i) => {
      nodes.push(svgEl('circle', { cx: x, cy: 255 - y, r: 8, class: 'curve-point', fill: COLORS[this.channel], 'data-i': i }));
    });
    this.svg.replaceChildren(...nodes);
    this.count.textContent = `${list.length} / ${CURVE_MAX}`;
  }

  private line(points: readonly CurvePoint[], channel: CurveChannel, ghost: boolean): SVGElement {
    return svgEl('polyline', {
      points: points.map(([x, y]) => `${x},${255 - y}`).join(' '),
      class: ghost ? 'curve-line curve-ghost' : 'curve-line',
      'data-channel': channel,
      stroke: COLORS[channel],
      fill: 'none',
    });
  }
}

export function mountCurves(prev: CurvesView | null, host: CurvesHost): CurvesView {
  prev?.destroy();
  return new CurvesView(host);
}
