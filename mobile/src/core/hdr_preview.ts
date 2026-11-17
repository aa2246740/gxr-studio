// 编辑器里的 HDR 预览什么时候算、什么时候丢掉。拖动中不发请求；松手后 300ms 再算。
// 每次 await 之后按当前的照片对象、version、rev、session 和引擎对象复核，不拿事先存下的票对比。

import type { GainMeta, GainTexture, HdrOutputMode } from '../../../frontend/src/hdr_math';
import { displayHeadroom, previewWanted } from './hdr_display';
import { downscaleRgba, PREVIEW_FACTOR, PREVIEW_LONG_EDGE } from './hdr_grid';
import type { HdrChainResult } from './hdr_chain';
import type { HdrFitJob } from './hdr_fit_host';

export const HDR_REFIT_MS = 300;
/** 设计稿上的小灰字。评审要拿掉时改成 false。 */
export const SHOW_HDR_PREVIEW_LABEL = true;

export interface HdrTicket {
  session: number;
  photoId: string;
  version: number;
  rev: number;
}

export interface HdrPhotoSnap {
  id: string;
  version: number;
  rev: number;
  dng: File | null;
  jpg: File | null;
}

export interface HdrEngine {
  setOutput(mode: HdrOutputMode): void;
  readback(): Promise<{ w: number; h: number; data: Uint8Array }>;
  hdrGainCount(): number;
}

export interface HdrFitRequest extends HdrFitJob {
  ticket: HdrTicket;
}

export type HdrFitResult = HdrChainResult;

export function ticketMatches(held: HdrTicket, now: HdrTicket): boolean {
  return held.session === now.session && held.photoId === now.photoId && held.version === now.version && held.rev === now.rev;
}

interface Clock {
  set(fn: () => void, ms: number): number;
  clear(id: number): void;
}

const nativeClock: Clock = {
  set: (fn, ms) => setTimeout(fn, ms) as unknown as number,
  clear: (id) => clearTimeout(id),
};

interface Seen {
  photo: HdrPhotoSnap;
  version: number;
  rev: number;
  session: number;
  engine: HdrEngine;
  frame: string | null;
  geometry: string | null;
  crop: string | null;
}

interface RgbaShot {
  rgba: Uint8Array;
  width: number;
  height: number;
}

/**
 * 打开、松手、换片、关掉。`requests` 只数真正发出的拟合，拖动中的 sync 不加。
 */
export class HdrPreviewController {
  private ticket: HdrTicket | null = null;
  private grid: { gain: GainTexture; meta: GainMeta } | null = null;
  private timer = 0;
  private epoch = 0;
  private runId = 0;
  private applied = false;
  requests = 0;
  showing = false;

  private readonly deps: {
    photo: () => HdrPhotoSnap | null;
    session: () => number;
    dragging: () => boolean;
    holding: () => boolean;
    exportOn: () => boolean;
    display: () => boolean;
    engine: () => HdrEngine | null;
    readDng: (photo: HdrPhotoSnap) => Promise<ArrayBuffer | null>;
    fit: (req: HdrFitRequest) => Promise<HdrFitResult>;
    watchExport?: (fn: () => void) => () => void;
    clock?: Clock;
    onShowing?: () => void;
    stopFit?: () => void;
    frameLayout?: () => string | null;
    geometryJson?: () => string | null;
    cropJson?: () => string | null;
    readBase?: () => Promise<RgbaShot | null>;
  };

  private offExport: (() => void) | null = null;

  constructor(deps: HdrPreviewController['deps']) {
    this.deps = deps;
    this.offExport = deps.watchExport?.(() => this.sync()) ?? null;
  }

  sync(): void {
    const photo = this.deps.photo();
    if (!photo) {
      this.close();
      return;
    }
    const next: HdrTicket = { session: this.deps.session(), photoId: photo.id, version: photo.version, rev: photo.rev };
    const switched = !this.ticket || this.ticket.photoId !== next.photoId || this.ticket.version !== next.version || this.ticket.session !== next.session;
    if (switched) {
      this.epoch += 1;
      this.runId += 1;
      this.clearTimer();
      this.deps.stopFit?.();
      this.release();
      this.ticket = next;
    } else if (this.ticket) {
      this.ticket = next;
    }
    if (this.deps.holding() || !this.wanted(photo)) {
      this.runId += 1;
      this.clearTimer();
      this.deps.stopFit?.();
      this.showSdr();
      return;
    }
    if (this.deps.dragging()) {
      this.runId += 1;
      this.clearTimer();
      return;
    }
    this.arm();
  }

  /** 边框或时间戳改了：马上丢掉正在跑的拟合，按现在的状态重算；不支持就回到 SDR。 */
  invalidate(): void {
    this.runId += 1;
    this.clearTimer();
    this.deps.stopFit?.();
    this.release();
    if (this.deps.photo()) this.sync();
  }

  close(): void {
    this.epoch += 1;
    this.runId += 1;
    this.clearTimer();
    this.deps.stopFit?.();
    this.release();
    this.ticket = null;
    this.offExport?.();
    this.offExport = null;
  }

  paintLabel(host: HTMLElement | null): void {
    if (!host || !SHOW_HDR_PREVIEW_LABEL) return;
    host.querySelector('[data-hdr-preview]')?.remove();
    if (!this.showing) return;
    const label = document.createElement('span');
    label.dataset.hdrPreview = '';
    label.className = 'tag hdr-preview';
    label.textContent = 'HDR 预览';
    host.append(label);
  }

  private wanted(photo: HdrPhotoSnap): boolean {
    return previewWanted(this.deps.display(), photo.dng !== null, this.deps.exportOn());
  }

  private seen(photo: HdrPhotoSnap, engine: HdrEngine): Seen {
    return {
      photo,
      version: photo.version,
      rev: photo.rev,
      session: this.deps.session(),
      engine,
      frame: this.deps.frameLayout?.() ?? null,
      geometry: this.deps.geometryJson?.() ?? null,
      crop: this.deps.cropJson?.() ?? null,
    };
  }

  /** 对着现在的照片、版本、rev、会话和引擎，不比较开跑时抄下来的票。 */
  private current(epoch: number, runId: number, seen: Seen): boolean {
    if (epoch !== this.epoch || runId !== this.runId) return false;
    const photo = this.deps.photo();
    const engine = this.deps.engine();
    if (!photo || !engine) return false;
    if (photo !== seen.photo || photo.version !== seen.version || photo.rev !== seen.rev) return false;
    if (this.deps.session() !== seen.session || engine !== seen.engine) return false;
    if ((this.deps.frameLayout?.() ?? null) !== seen.frame) return false;
    if ((this.deps.geometryJson?.() ?? null) !== seen.geometry) return false;
    if ((this.deps.cropJson?.() ?? null) !== seen.crop) return false;
    if (this.deps.dragging() || this.deps.holding() || !this.wanted(photo)) return false;
    return true;
  }

  private arm(): void {
    this.clearTimer();
    const epoch = this.epoch;
    const runId = ++this.runId;
    const clock = this.deps.clock ?? nativeClock;
    this.timer = clock.set(() => {
      this.timer = 0;
      void this.run(epoch, runId).catch(() => {
        const photo = this.deps.photo();
        const engine = this.deps.engine();
        if (photo && engine && this.current(epoch, runId, this.seen(photo, engine))) this.failCurrent();
      });
    }, HDR_REFIT_MS);
  }

  private async run(epoch: number, runId: number): Promise<void> {
    const photo = this.deps.photo();
    const engine = this.deps.engine();
    if (!photo || !engine) return;
    const seen = this.seen(photo, engine);
    if (!this.current(epoch, runId, seen)) return;
    let shot: { w: number; h: number; data: Uint8Array };
    try {
      shot = await engine.readback();
    } catch {
      if (this.current(epoch, runId, seen)) this.failCurrent();
      return;
    }
    if (!this.current(epoch, runId, seen)) return;
    let dng: ArrayBuffer | null;
    try {
      dng = await this.deps.readDng(photo);
    } catch {
      if (this.current(epoch, runId, seen)) this.failCurrent();
      return;
    }
    if (!this.current(epoch, runId, seen)) return;
    if (!dng) {
      this.failCurrent();
      return;
    }
    let jpg: ArrayBuffer | null = null;
    if (photo.jpg) {
      try {
        jpg = await photo.jpg.arrayBuffer();
      } catch {
        jpg = null;
      }
    }
    if (!this.current(epoch, runId, seen)) return;
    let base: RgbaShot | null = null;
    if (this.deps.readBase) {
      try {
        base = await this.deps.readBase();
      } catch {
        if (this.current(epoch, runId, seen)) this.failCurrent();
        return;
      }
      if (!this.current(epoch, runId, seen)) return;
    }
    const sdr = downscaleRgba(shot.data, shot.w, shot.h, PREVIEW_LONG_EDGE);
    const baseShot = base ? downscaleRgba(base.rgba, base.width, base.height, PREVIEW_LONG_EDGE) : null;
    this.requests += 1;
    let fitted: HdrFitResult;
    try {
      fitted = await this.deps.fit({
        ticket: { session: seen.session, photoId: photo.id, version: photo.version, rev: photo.rev },
        sdr: sdr.rgba,
        width: sdr.width,
        height: sdr.height,
        dng,
        jpg,
        base: baseShot ? baseShot.rgba : null,
        baseWidth: baseShot ? baseShot.width : sdr.width,
        baseHeight: baseShot ? baseShot.height : sdr.height,
        geometryJson: seen.geometry,
        cropJson: seen.crop,
        box: null,
        frameLayoutJson: seen.frame,
        factor: PREVIEW_FACTOR,
      });
    } catch {
      if (this.current(epoch, runId, seen)) this.failCurrent();
      return;
    }
    if (!this.current(epoch, runId, seen)) return;
    if (!fitted.ok) {
      if (fitted.reason === 'stale') return;
      this.failCurrent();
      return;
    }
    this.grid = { gain: { width: fitted.width, height: fitted.height, codes: fitted.codes }, meta: fitted.meta };
    this.apply();
  }

  private apply(): void {
    const engine = this.deps.engine();
    if (!engine || !this.grid || this.deps.holding()) return;
    engine.setOutput({ hdr: true, gain: this.grid.gain, meta: this.grid.meta, headroom: displayHeadroom() });
    this.applied = true;
    this.setShowing(true);
  }

  private failCurrent(): void {
    this.grid = null;
    this.showSdr();
  }

  private showSdr(): void {
    if (this.applied) this.deps.engine()?.setOutput({ hdr: false });
    this.applied = false;
    this.grid = null;
    this.setShowing(false);
  }

  private setShowing(on: boolean): void {
    if (this.showing === on) return;
    this.showing = on;
    this.deps.onShowing?.();
  }

  private release(): void {
    this.grid = null;
    this.showSdr();
  }

  private clearTimer(): void {
    if (!this.timer) return;
    (this.deps.clock ?? nativeClock).clear(this.timer);
    this.timer = 0;
  }
}
