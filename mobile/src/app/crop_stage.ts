// 舞台上的裁剪框叠加层、手势（拖框/捏合/翻片/长按原图）与几何提交。

import { type Engine, type Geometry } from '../../../frontend/src/index';
import { geomCropWrite, geomFinalBox, geomGeometryWrite } from '../core/api';
import { CoreError } from '../core/protocol';
import {
  cropIsPortrait,
  croppedPixelSize,
  fitStraightened,
  geometrySidecar,
  guideShapes,
  loadGuidePrefs,
  ORIENTABLE,
  presetLabel,
  rectFromArray,
  rectToArray,
  sameCropState,
  sideSubset,
  type CanvasDims,
  type CropState,
  type Handle,
  type Orientation,
  type Rect,
} from '../lib/crop';
import { CropGesture } from '../lib/crop_gesture';
import { CropPanel } from './crop_panel';
import { el } from './dom';
import { releaseLooks, type AppApi, type Photo } from './model';

export const DEFAULT_GEOMETRY: Geometry = {
  straighten: 0,
  rotate90: 0,
  flip: false,
  vertical: 0,
  horizontal: 0,
  distortion: 0,
  box: null,
};

const SVG_NS = 'http://www.w3.org/2000/svg';
const HANDLES: Handle[] = ['t', 'b', 'l', 'r', 'tl', 'tr', 'bl', 'br'];

export interface CropStageHost {
  photo(): Photo | null;
  mode(): 'raw' | 'jpg' | 'still';
  active(): boolean;
  sharp(): ImageBitmap | null;
  stillCanvas(): HTMLCanvasElement | null;
  placeholderEl(): HTMLElement | null;
  pvEl(): HTMLElement | null;
  caption(): string;
  holding(): boolean;
  setHolding(on: boolean, label?: string): void;
  setLabel(text: string): void;
  enterDrag(): void;
  leaveDrag(): void;
  dragging(): boolean;
  requestHist(): void;
  swipe(id: string, dir: -1 | 1): void;
  onCommitted(photo: Photo, changed: boolean): void;
  restLabelText(): string;
  resetZoom?(): void;
  onScrub?(text: string | null): void;
}

export class CropStage {
  private cropPanel: CropPanel | null = null;
  private cropTabBtn: HTMLButtonElement | null = null;
  private cropOverlay: HTMLElement | null = null;
  private cropBox: HTMLElement | null = null;
  private cropSizeEl: HTMLElement | null = null;
  private cropGuidesSvg: SVGSVGElement | null = null;
  private readonly gesture = new CropGesture();
  private draftRect: Rect | null = null;
  private draftAngle: number | null = null;
  private straightenBaseRect: Rect | null = null;
  private geoSeq = 0;
  private destroyed = false;
  private exportSize: { photo: Photo; version: number; rev: number; w: number; h: number } | null = null;
  private commitChain: Promise<void> = Promise.resolve();

  constructor(
    private readonly shell: HTMLElement,
    private readonly api: AppApi,
    private readonly engine: Engine | null,
    private readonly host: CropStageHost,
  ) {}

  reset(): void {
    this.draftRect = null;
    this.draftAngle = null;
    this.straightenBaseRect = null;
    this.gesture.cancel();
    this.cropPanel?.destroy();
    this.cropPanel = null;
    this.cropTabBtn = null;
  }

  destroy(): void {
    this.destroyed = true;
    this.geoSeq += 1;
    this.exportSize = null;
    this.reset();
    this.cropOverlay = null;
    this.cropBox = null;
    this.cropSizeEl = null;
    this.cropGuidesSvg = null;
  }

  mountOverlay(pv: HTMLElement): void {
    this.cropOverlay = el('div', 'crop-overlay');
    this.cropOverlay.hidden = true;
    this.cropBox = el('div', 'cbox');
    this.cropGuidesSvg = document.createElementNS(SVG_NS, 'svg');
    this.cropGuidesSvg.setAttribute('class', 'guides');
    this.cropGuidesSvg.setAttribute('preserveAspectRatio', 'none');
    this.cropGuidesSvg.setAttribute('aria-hidden', 'true');
    this.cropBox.append(this.cropGuidesSvg);
    for (const h of HANDLES) {
      const node = el('i', h.length === 2 ? `ch corner ${h}` : `ch edge ${h}`);
      node.dataset.h = h;
      this.cropBox.append(node);
    }
    this.cropSizeEl = el('span', 'csize num bdg', '');
    this.cropBox.append(this.cropSizeEl);
    this.cropOverlay.append(this.cropBox);
    pv.append(this.cropOverlay);
  }

  createPanel(): HTMLElement {
    this.cropPanel = new CropPanel(this.shell, {
      photo: () => this.host.photo(),
      canvas: () => this.photoCanvas(),
      liveStraighten: () => this.liveStraighten(),
      liveCrop: () => this.liveCrop(),
      pickPreset: (preset, orient) => void this.commitPreset(preset, orient),
      pickOrientation: (orient) => void this.commitOrientation(orient),
      guidesChanged: () => this.renderOverlay(),
      beginStraighten: () => {
        if (this.destroyed) return;
        this.straightenBaseRect = rectFromArray(this.liveCrop()?.rect);
        this.host.enterDrag();
      },
      previewStraighten: (angle) => this.onPreviewStraighten(angle),
      endStraighten: () => this.onEndStraighten(),
      cancelStraighten: () => this.onCancelStraighten(),
      resetStraighten: () => void this.commitStraighten(0),
      onScrub: (text) => this.host.onScrub?.(text),
      onExportSize: (target, w, h) => {
        if (this.destroyed || this.host.photo() !== target) return;
        this.exportSize = { photo: target, version: target.version, rev: target.rev, w, h };
        if (this.draftRect === null && this.draftAngle === null && this.cropSizeEl && this.host.active()) {
          this.cropSizeEl.textContent = `${w} × ${h}`;
        }
      },
    });
    return this.cropPanel.panelEl();
  }

  panelHeadEl(): HTMLElement | null {
    return this.cropPanel?.headEl() ?? null;
  }

  attachTabButton(btn: HTMLButtonElement): void {
    this.cropTabBtn = btn;
    this.paintTabDot();
  }

  syncLoaded(): void {
    if (this.destroyed || !this.host.active()) return;
    this.renderOverlay();
    this.cropPanel?.sync();
  }

  syncTabMode(): void {
    if (this.destroyed) return;
    this.gesture.cancel();
    this.draftRect = null;
    this.draftAngle = null;
    this.straightenBaseRect = null;
    if (this.host.active()) {
      this.host.resetZoom?.();
      if (this.cropOverlay) this.cropOverlay.hidden = false;
      this.cropPanel?.sync();
      this.renderOverlay();
    } else {
      if (this.cropOverlay) this.cropOverlay.hidden = true;
      if (!this.host.holding()) this.host.setLabel(this.host.restLabelText());
    }
    void this.applyPreviewGeometry();
  }

  paintStill(bitmap: ImageBitmap | null): void {
    const still = this.host.stillCanvas();
    const placeholder = this.host.placeholderEl();
    if (!still) return;
    if (!bitmap) {
      still.hidden = true;
      return;
    }
    if (placeholder) placeholder.hidden = true;
    still.hidden = false;
    const angle = this.liveStraighten();
    const photo = this.host.photo();
    const crop = !this.host.active() ? photo?.crop : null;
    const rect = crop ? rectFromArray(crop.rect) : null;
    const srcW = bitmap.width;
    const srcH = bitmap.height;
    const frame = photo?.frame;
    const useFrame =
      Boolean(frame && frame.width > 0 && frame.height > 0 && srcW > 0 && srcH > 0) &&
      (frame!.width >= frame!.height) === (srcW >= srcH) &&
      Math.abs((frame!.width / frame!.height) / (srcW / srcH) - 1) <= 0.02;
    const refW = useFrame ? frame!.width : srcW;
    const refH = useFrame ? frame!.height : srcH;
    // 无 GPU 时先画 360px 缩略图再换清图：WebKit 对 resizeWidth 向上取整高度会导致两帧宽高比差约 0.3%（iPad 上跳 2 px）。
    // 按文件头真实尺寸锁住虚拟画布比例，提锐、导出与旋转前后位移均 <= 1 px。
    const targetLong = useFrame ? Math.max(srcW, srcH, Math.min(1024, Math.max(refW, refH))) : Math.max(srcW, srcH);
    const scale = targetLong / Math.max(1, refW, refH);
    const baseW = refW * scale;
    const baseH = refH * scale;
    const bx = rect ? rect.x * baseW : 0;
    const by = rect ? rect.y * baseH : 0;
    const bw = rect ? Math.max(1, Math.round(rect.w * baseW)) : Math.max(1, Math.round(baseW));
    const bh = rect ? Math.max(1, Math.round(rect.h * baseH)) : Math.max(1, Math.round(baseH));
    still.width = bw;
    still.height = bh;
    const ctx = still.getContext('2d');
    if (!ctx) return;
    ctx.clearRect(0, 0, bw, bh);
    if (Math.abs(angle) < 1e-6 && !rect) {
      ctx.drawImage(bitmap, 0, 0, bw, bh);
      return;
    }
    ctx.save();
    ctx.translate(baseW / 2 - bx, baseH / 2 - by);
    if (Math.abs(angle) >= 1e-6) ctx.rotate((angle * Math.PI) / 180);
    ctx.drawImage(bitmap, -baseW / 2, -baseH / 2, baseW, baseH);
    ctx.restore();
  }

  paintTabDot(): void {
    const photo = this.host.photo();
    if (!this.cropTabBtn || !photo) return;
    const c = photo.crop;
    const hasCrop = c !== null && (c.preset !== 'original' || c.rect[2] < 0.999 || c.rect[3] < 0.999 || c.rect[0] > 0.001 || c.rect[1] > 0.001);
    this.cropTabBtn.classList.toggle('edited', hasCrop || photo.geometry.straighten !== 0);
  }

  renderOverlay(): void {
    if (this.destroyed) return;
    const pv = this.host.pvEl();
    const photo = this.host.photo();
    if (!pv || !this.cropOverlay || !this.cropBox || !this.cropSizeEl || !this.cropGuidesSvg) return;
    if (!this.host.active() || !photo) {
      this.cropOverlay.hidden = true;
      return;
    }
    const canvas = this.photoCanvas();
    const pvW = pv.clientWidth;
    const pvH = pv.clientHeight;
    if (pvW <= 0 || pvH <= 0 || canvas.w <= 0 || canvas.h <= 0) return;
    const scale = Math.min(pvW / canvas.w, pvH / canvas.h);
    const imgW = canvas.w * scale;
    const imgH = canvas.h * scale;
    this.cropOverlay.hidden = false;
    this.cropOverlay.style.left = `${(pvW - imgW) / 2}px`;
    this.cropOverlay.style.top = `${(pvH - imgH) / 2}px`;
    this.cropOverlay.style.width = `${imgW}px`;
    this.cropOverlay.style.height = `${imgH}px`;

    const crop = this.liveCrop();
    const rect = rectFromArray(crop?.rect);
    const bx = rect.x * imgW;
    const by = rect.y * imgH;
    const bw = Math.max(2, rect.w * imgW);
    const bh = Math.max(2, rect.h * imgH);
    this.cropBox.style.left = `${bx}px`;
    this.cropBox.style.top = `${by}px`;
    this.cropBox.style.width = `${bw}px`;
    this.cropBox.style.height = `${bh}px`;

    const exact =
      this.draftRect === null &&
      this.draftAngle === null &&
      this.exportSize &&
      this.exportSize.photo === photo &&
      this.exportSize.version === photo.version &&
      this.exportSize.rev === photo.rev
        ? this.exportSize
        : null;
    const px = exact ? { w: exact.w, h: exact.h } : croppedPixelSize(rect, this.badgeCanvas(photo));
    this.cropSizeEl.textContent = `${px.w} × ${px.h}`;

    const prefs = loadGuidePrefs();
    const ar = (rect.w * canvas.w) / Math.max(1e-6, rect.h * canvas.h);
    this.cropGuidesSvg.setAttribute('viewBox', `0 0 ${ar.toFixed(4)} 1`);
    const { lines, path } = guideShapes(prefs.guide, prefs.spiral, ar);
    const frag = document.createDocumentFragment();
    for (const [x1, y1, x2, y2] of lines) {
      const line = document.createElementNS(SVG_NS, 'line');
      line.setAttribute('x1', String(x1));
      line.setAttribute('y1', String(y1));
      line.setAttribute('x2', String(x2));
      line.setAttribute('y2', String(y2));
      frag.append(line);
    }
    if (path) {
      const p = document.createElementNS(SVG_NS, 'path');
      p.setAttribute('d', path);
      frag.append(p);
    }
    this.cropGuidesSvg.replaceChildren(frag);
    if (!this.host.holding()) {
      this.host.setLabel(presetLabel(crop?.preset ?? 'original', rect, canvas));
    }
  }

  async resolveBoxGeometry(photo: Photo): Promise<{ geometry: Geometry; cropped: boolean }> {
    const straighten = photo.geometry.straighten;
    const crop = photo.crop;
    const cropped = crop !== null || straighten !== 0;
    if (!crop && straighten === 0) {
      return { geometry: { ...DEFAULT_GEOMETRY }, cropped: false };
    }
    const layer = this.engine?.layerSize() ?? (photo.frame ? { w: photo.frame.width, h: photo.frame.height } : null);
    if (!layer || layer.w <= 0 || layer.h <= 0) {
      return { geometry: { ...DEFAULT_GEOMETRY, straighten }, cropped };
    }
    try {
      const reply = await geomFinalBox(layer.w, layer.h, geometrySidecar(straighten), crop);
      return { geometry: { ...DEFAULT_GEOMETRY, straighten, box: reply.box }, cropped };
    } catch {
      return { geometry: { ...DEFAULT_GEOMETRY, straighten }, cropped };
    }
  }

  async applyPreviewGeometry(): Promise<void> {
    if (this.destroyed) return;
    const photo = this.host.photo();
    if (!photo) return;
    if (this.host.mode() === 'still') {
      this.paintStill(this.host.sharp() ?? photo.bitmap);
      return;
    }
    if (!this.engine) return;
    const seq = ++this.geoSeq;
    const version = photo.version;
    const session = this.api.session();
    if (this.host.active()) {
      this.engine.setGeometry({ ...DEFAULT_GEOMETRY, straighten: this.liveStraighten(), box: null });
      this.engine.setDisplayAspect(null);
      this.shell.dataset.geoSeq = String(seq);
      return;
    }
    await this.commitChain.catch(() => undefined);
    if (
      this.destroyed ||
      seq !== this.geoSeq ||
      this.api.session() !== session ||
      this.host.photo() !== photo ||
      photo.version !== version ||
      this.host.active()
    ) {
      return;
    }
    const { geometry } = await this.resolveBoxGeometry(photo);
    if (
      this.destroyed ||
      seq !== this.geoSeq ||
      this.api.session() !== session ||
      this.host.photo() !== photo ||
      photo.version !== version ||
      this.host.active()
    ) {
      return;
    }
    this.engine.setGeometry(geometry);
    this.engine.setDisplayAspect(null);
    this.host.requestHist();
    this.shell.dataset.geoSeq = String(seq);
  }

  bindGesture(stage: HTMLElement): void {
    const pointers = new Map<number, { x: number; y: number }>();
    let startX = 0;
    let startY = 0;
    let primary = -1;
    let moved = false;
    let held = false;
    let cropActive = false;
    let timer = 0;

    const norm = (clientX: number, clientY: number): { x: number; y: number } => {
      const ov = this.cropOverlay?.getBoundingClientRect();
      if (!ov || ov.width <= 0 || ov.height <= 0) return { x: 0.5, y: 0.5 };
      return { x: (clientX - ov.left) / ov.width, y: (clientY - ov.top) / ov.height };
    };

    stage.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      const handleEl = this.host.active() ? (event.target as HTMLElement | null)?.closest<HTMLElement>('.ch') ?? null : null;
      if (!handleEl && event.clientX < 24) return;
      const photo = this.host.photo();
      if (!photo) return;

      if (this.host.active()) {
        this.cropPanel?.showError(null);
        const pt = norm(event.clientX, event.clientY);
        pointers.set(event.pointerId, pt);
        try {
          stage.setPointerCapture(event.pointerId);
        } catch {
          /* ignore */
        }
        const crop = this.liveCrop();
        const canvas = this.photoCanvas();
        const preset = crop?.preset ?? 'original';
        if (pointers.size === 2) {
          window.clearTimeout(timer);
          timer = 0;
          if (held) {
            held = false;
            this.host.setHolding(false);
          }
          const [p1, p2] = [...pointers.values()];
          this.gesture.startPinch(p1, p2, crop, canvas, preset);
          cropActive = true;
          moved = true;
          return;
        }
        if (pointers.size > 2) return;
        cropActive = true;
        primary = event.pointerId;
        startX = event.clientX;
        startY = event.clientY;
        moved = false;
        held = false;
        if (handleEl?.dataset.h) {
          // 拖角或拖边：不允许长按对比原图。
          this.gesture.startResize(handleEl.dataset.h as Handle, pt, crop, canvas, preset);
        } else {
          this.gesture.startMove(pt, crop, canvas);
          timer = window.setTimeout(() => {
            timer = 0;
            if (moved || !this.host.photo() || pointers.size !== 1) return;
            held = true;
            this.host.setHolding(true, this.host.caption());
          }, 450);
        }
        return;
      }
    });

    stage.addEventListener('pointermove', (event) => {
      if (!cropActive) return;
      if (!pointers.has(event.pointerId) || !this.host.photo()) return;
      const pt = norm(event.clientX, event.clientY);
      pointers.set(event.pointerId, pt);
      if (event.pointerId === primary && Math.hypot(event.clientX - startX, event.clientY - startY) > 4) {
        moved = true;
        if (timer) {
          window.clearTimeout(timer);
          timer = 0;
        }
      }
      const canvas = this.photoCanvas();
      const angle = this.liveStraighten();
      if (this.gesture.mode === 'pinch' && pointers.size >= 2) {
        const [p1, p2] = [...pointers.values()];
        this.draftRect = this.gesture.pinch(p1, p2, canvas, angle);
        this.renderOverlay();
      } else if (this.gesture.mode === 'move' || this.gesture.mode === 'resize') {
        this.draftRect = this.gesture.move(pt, canvas, angle);
        this.renderOverlay();
      }
    });

    const endPointer = (event: PointerEvent) => {
      if (!cropActive) return;
      if (!pointers.has(event.pointerId)) return;
      pointers.delete(event.pointerId);
      if (stage.hasPointerCapture?.(event.pointerId)) {
        try {
          stage.releasePointerCapture(event.pointerId);
        } catch {
          /* ignore */
        }
      }
      if (pointers.size === 0 && timer) {
        window.clearTimeout(timer);
        timer = 0;
      }
      if (held && pointers.size === 0) {
        held = false;
        this.host.setHolding(false);
      }
      if (this.gesture.mode === 'pinch' && pointers.size < 2) {
        const next = this.gesture.end();
        if (pointers.size === 0) {
          cropActive = false;
          primary = -1;
        }
        if (event.type === 'pointerup' && next && moved) void this.commitCrop(next);
        else {
          this.draftRect = null;
          this.renderOverlay();
        }
        return;
      }
      if (pointers.size === 0) {
        cropActive = false;
        primary = -1;
        const next = this.gesture.end();
        if (event.type === 'pointerup' && next && moved) void this.commitCrop(next);
        else {
          this.draftRect = null;
          this.renderOverlay();
        }
      }
    };

    stage.addEventListener('pointerup', endPointer);
    stage.addEventListener('pointercancel', endPointer);
  }

  private photoCanvas(): CanvasDims {
    const layer = this.engine?.layerSize();
    if (layer && layer.w > 0 && layer.h > 0) return layer;
    const photo = this.host.photo();
    const frame = photo?.frame;
    if (frame && frame.width > 0 && frame.height > 0) return { w: frame.width, h: frame.height };
    const bmp = this.host.sharp() ?? photo?.bitmap;
    if (bmp && bmp.width > 0 && bmp.height > 0) return { w: bmp.width, h: bmp.height };
    return { w: 1, h: 1 };
  }

  private badgeCanvas(photo: Photo): CanvasDims {
    const frame = photo.frame;
    if (frame && frame.width > 0 && frame.height > 0) return { w: frame.width, h: frame.height };
    return this.photoCanvas();
  }

  private liveStraighten(): number {
    return this.draftAngle ?? this.host.photo()?.geometry.straighten ?? 0;
  }

  private liveCrop(): CropState | null {
    const photo = this.host.photo();
    if (!photo) return null;
    if (this.draftRect) {
      return {
        preset: photo.crop?.preset ?? 'original',
        rect: rectToArray(this.draftRect),
        angle_deg: this.liveStraighten(),
      };
    }
    return photo.crop;
  }

  private onPreviewStraighten(angle: number): void {
    if (this.destroyed) return;
    const photo = this.host.photo();
    if (!photo) return;
    const clean = Math.min(45, Math.max(-45, Math.round(angle * 10) / 10));
    this.draftAngle = clean;
    const baseRect = this.straightenBaseRect ?? rectFromArray(photo.crop?.rect);
    this.draftRect = fitStraightened(baseRect, this.photoCanvas(), clean).rect;
    if (this.engine && this.host.mode() !== 'still') {
      this.engine.setGeometry({ ...DEFAULT_GEOMETRY, straighten: clean, box: null });
      this.engine.setDisplayAspect(null);
    } else if (this.host.mode() === 'still') {
      this.paintStill(this.host.sharp() ?? photo.bitmap);
    }
    this.cropPanel?.syncAngleOnly();
    this.renderOverlay();
  }

  private onEndStraighten(): void {
    if (this.destroyed) return;
    const angle = this.draftAngle;
    this.straightenBaseRect = null;
    if (this.host.dragging()) this.host.leaveDrag();
    if (angle !== null) void this.commitStraighten(angle);
  }

  private onCancelStraighten(): void {
    if (this.destroyed) return;
    this.draftAngle = null;
    this.draftRect = null;
    this.straightenBaseRect = null;
    if (this.host.dragging()) this.host.leaveDrag();
    this.cropPanel?.syncAngleOnly();
    this.renderOverlay();
    void this.applyPreviewGeometry();
  }

  private isModelCurrent(photo: Photo, version: number, session: number, rev?: number): boolean {
    return (
      this.api.session() === session &&
      this.api.photo(photo.id) === photo &&
      photo.version === version &&
      (rev === undefined || photo.rev === rev)
    );
  }

  private enqueueCommit(photo: Photo, fn: (version: number, session: number, sentRev: number) => Promise<void>): Promise<void> {
    const version = photo.version;
    const session = this.api.session();
    const run = this.commitChain.then(async () => {
      if (!this.isModelCurrent(photo, version, session)) return;
      await fn(version, session, photo.rev);
    });
    this.commitChain = run.catch(() => undefined);
    return run;
  }

  private async commitPreset(preset: string, orient?: Orientation): Promise<void> {
    const photo = this.host.photo();
    if (!photo || this.destroyed) return;
    await this.enqueueCommit(photo, async (version, session, sentRev) => {
      if (preset === 'original' && photo.crop === null && photo.geometry.straighten === 0) {
        if (!this.destroyed && this.host.photo() === photo) {
          this.draftRect = null;
          this.cropPanel?.sync();
          this.renderOverlay();
        }
        return;
      }
      const orientation = ORIENTABLE.has(preset) ? (orient ?? 'landscape') : undefined;
      const req: Record<string, unknown> = { preset, rect: null };
      if (orientation) req.orientation = orientation;
      let reply: Awaited<ReturnType<typeof geomCropWrite>>;
      try {
        reply = await geomCropWrite(sideSubset(photo), req);
      } catch (err) {
        this.handleCommitError(photo, version, session, sentRev, err);
        return;
      }
      this.applyCommitReply(photo, version, session, sentRev, reply);
    });
  }

  private async commitOrientation(orient: Orientation): Promise<void> {
    const photo = this.host.photo();
    if (!photo || this.destroyed) return;
    const preset = photo.crop?.preset ?? 'original';
    if (!ORIENTABLE.has(preset)) return;
    await this.enqueueCommit(photo, async (version, session, sentRev) => {
      let reply: Awaited<ReturnType<typeof geomCropWrite>>;
      try {
        reply = await geomCropWrite(sideSubset(photo), { preset, rect: null, orientation: orient });
      } catch (err) {
        this.handleCommitError(photo, version, session, sentRev, err);
        return;
      }
      this.applyCommitReply(photo, version, session, sentRev, reply);
    });
  }

  private async commitStraighten(angle: number): Promise<void> {
    const photo = this.host.photo();
    if (!photo || this.destroyed) return;
    const clean = Math.round(angle * 10) / 10;
    await this.enqueueCommit(photo, async (version, session, sentRev) => {
      if (clean === photo.geometry.straighten) {
        if (!this.destroyed && this.host.photo() === photo) {
          this.draftAngle = null;
          this.draftRect = null;
          this.cropPanel?.sync();
          this.renderOverlay();
          void this.applyPreviewGeometry();
        }
        return;
      }
      let reply: Awaited<ReturnType<typeof geomGeometryWrite>>;
      try {
        reply = await geomGeometryWrite(sideSubset(photo), geometrySidecar(clean));
      } catch (err) {
        this.handleCommitError(photo, version, session, sentRev, err);
        return;
      }
      this.applyCommitReply(photo, version, session, sentRev, reply);
    });
  }

  private async commitCrop(rect: [number, number, number, number]): Promise<void> {
    const photo = this.host.photo();
    if (!photo || this.destroyed) return;
    const canvas = this.photoCanvas();
    await this.enqueueCommit(photo, async (version, session, sentRev) => {
      const preset = photo.crop?.preset ?? 'original';
      const orientation = ORIENTABLE.has(preset) ? (cropIsPortrait(rectFromArray(rect), canvas) ? 'portrait' : 'landscape') : undefined;
      const req: Record<string, unknown> = { preset, rect };
      if (orientation) req.orientation = orientation;
      let reply: Awaited<ReturnType<typeof geomCropWrite>>;
      try {
        reply = await geomCropWrite(sideSubset(photo), req);
      } catch (err) {
        this.handleCommitError(photo, version, session, sentRev, err);
        return;
      }
      this.applyCommitReply(photo, version, session, sentRev, reply);
    });
  }

  private applyCommitReply(
    photo: Photo,
    version: number,
    session: number,
    sentRev: number,
    reply: Awaited<ReturnType<typeof geomCropWrite>>,
  ): void {
    if (!this.isModelCurrent(photo, version, session, sentRev)) {
      if (!this.destroyed && this.host.photo() === photo) {
        this.draftRect = null;
        this.draftAngle = null;
        this.renderOverlay();
      }
      return;
    }
    const nextAngle = reply.geometry?.straighten ?? 0;
    const nextCrop = reply.crop;
    const changed = nextAngle !== photo.geometry.straighten || !sameCropState(photo.crop, nextCrop);
    if (changed) {
      photo.geometry = { straighten: nextAngle };
      photo.crop = nextCrop;
      photo.rev += 1;
      photo.neutral?.bitmap.close();
      photo.neutral = null;
      releaseLooks(photo);
    }
    if (this.destroyed || this.host.photo() !== photo) return;
    this.draftRect = null;
    this.draftAngle = null;
    this.cropPanel?.showError(null);
    this.paintTabDot();
    this.cropPanel?.sync();
    this.renderOverlay();
    void this.applyPreviewGeometry();
    this.host.onCommitted(photo, changed);
  }

  private handleCommitError(photo: Photo, version: number, session: number, sentRev: number, err: unknown): void {
    if (!this.isModelCurrent(photo, version, session, sentRev)) return;
    if (this.destroyed || this.host.photo() !== photo) return;
    this.draftRect = null;
    this.draftAngle = null;
    const message = err instanceof CoreError ? err.message : err instanceof Error ? err.message : '裁剪无效';
    this.cropPanel?.showError(message);
    this.cropPanel?.sync();
    this.renderOverlay();
    void this.applyPreviewGeometry();
  }
}
