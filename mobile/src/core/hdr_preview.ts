// 编辑器里的 HDR 预览什么时候算、什么时候丢掉。拖动中不发请求；松手后 300ms 再算。
// 晚到的结果对不上（会话、照片、版本）就丢掉，不碰引擎。

import type { GainMeta, GainTexture, HdrOutputMode } from '../../../frontend/src/hdr_math';
import { displayHeadroom, previewWanted } from './hdr_display';

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

export interface HdrFitRequest {
  ticket: HdrTicket;
  rgba: Uint8Array;
  width: number;
  height: number;
  dng: ArrayBuffer;
  jpg: ArrayBuffer | null;
}

export type HdrFitResult =
  | { ok: true; width: number; height: number; codes: Uint8Array; meta: GainMeta }
  | { ok: false; reason: string };

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
      this.release();
      this.ticket = next;
    } else if (this.ticket) {
      this.ticket = next;
    }
    if (this.deps.holding() || !this.wanted(photo)) {
      this.runId += 1;
      this.clearTimer();
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

  close(): void {
    this.epoch += 1;
    this.runId += 1;
    this.clearTimer();
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

  private arm(): void {
    this.clearTimer();
    const epoch = this.epoch;
    const runId = ++this.runId;
    const ticket = this.ticket;
    if (!ticket) return;
    const clock = this.deps.clock ?? nativeClock;
    this.timer = clock.set(() => {
      this.timer = 0;
      void this.run(epoch, runId, ticket);
    }, HDR_REFIT_MS);
  }

  private live(epoch: number, runId: number, ticket: HdrTicket): boolean {
    return epoch === this.epoch && runId === this.runId && this.ticket !== null && ticketMatches(ticket, this.ticket);
  }

  private async run(epoch: number, runId: number, ticket: HdrTicket): Promise<void> {
    const photo = this.deps.photo();
    const engine = this.deps.engine();
    if (!photo || !engine || !this.live(epoch, runId, ticket)) return;
    if (this.deps.dragging() || this.deps.holding() || !this.wanted(photo)) return;
    let rgba: Uint8Array;
    let width: number;
    let height: number;
    try {
      const shot = await engine.readback();
      rgba = shot.data;
      width = shot.w;
      height = shot.h;
    } catch {
      return;
    }
    if (!this.live(epoch, runId, ticket)) return;
    const dng = await this.deps.readDng(photo);
    if (!dng || !this.live(epoch, runId, ticket)) return;
    let jpg: ArrayBuffer | null = null;
    if (photo.jpg) {
      try {
        jpg = await photo.jpg.arrayBuffer();
      } catch {
        jpg = null;
      }
    }
    if (!this.live(epoch, runId, ticket) || this.deps.dragging() || this.deps.holding()) return;
    this.requests += 1;
    const fitted = await this.deps.fit({ ticket, rgba, width, height, dng, jpg });
    if (!this.live(epoch, runId, ticket) || this.deps.holding()) return;
    if (!fitted.ok) {
      this.grid = null;
      this.showSdr();
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
    this.showing = true;
  }

  private showSdr(): void {
    if (this.applied) this.deps.engine()?.setOutput({ hdr: false });
    this.applied = false;
    this.showing = false;
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
