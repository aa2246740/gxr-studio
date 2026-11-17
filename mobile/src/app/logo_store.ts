// 手机本地自定义标志目录（存在 IndexedDB `logos` 表）：读取、添加、重命名、删除，和给 Canvas2D 直接用的离屏画布缓存。

import { prepareLogo } from '../core/api';
import type { LogoInfo } from '../lib/frame_spec';
import { deleteStoredLogo, loadAllLogos, putStoredLogo, renameStoredLogo, type StoredLogo } from '../lib/idb';
import { cleanLogoName, LOGO_ERR, LOGO_LIMITS, MISSING_LOGO_LINE, packLogoEntries } from '../lib/logo_key';

export const MISSING_LOGO_TOAST = MISSING_LOGO_LINE;

function toInfo(row: StoredLogo): LogoInfo {
  return {
    id: row.id,
    name: row.name,
    w: row.w,
    h: row.h,
    coverage: row.coverage,
    opaque: row.opaque,
    lum: row.lum,
  };
}

function makeId(): string {
  const rand = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID().replace(/-/g, '').slice(0, 10)
    : Math.random().toString(36).slice(2, 12);
  return `logo_${Date.now().toString(36)}_${rand}`;
}

function buildCanvas(row: StoredLogo): HTMLCanvasElement | null {
  if (typeof document === 'undefined') return null;
  const cv = document.createElement('canvas');
  cv.width = row.w;
  cv.height = row.h;
  const ctx = cv.getContext('2d');
  if (!ctx) return null;
  const rgba = new Uint8ClampedArray(row.rgba);
  if (rgba.length !== row.w * row.h * 4) return null;
  ctx.putImageData(new ImageData(rgba, row.w, row.h), 0, 0);
  return cv;
}

export class LogoStore {
  private rows: StoredLogo[] = [];
  private byId = new Map<string, StoredLogo>();
  private canvases = new Map<string, HTMLCanvasElement>();
  private isLoaded = false;
  private loading: Promise<LogoInfo[]> | null = null;
  private rev = 0;
  private listeners = new Set<() => void>();

  loaded(): boolean {
    return this.isLoaded;
  }

  revision(): number {
    return this.rev;
  }

  list(): LogoInfo[] {
    return this.rows.map(toInfo);
  }

  info(id: string): LogoInfo | null {
    const row = this.byId.get(id);
    return row ? toInfo(row) : null;
  }

  packLogos(ids: Iterable<string>): { logoIndex: string; logoBlob: ArrayBuffer | null; infos: LogoInfo[] } {
    const seen = new Set<string>();
    const entries: Array<{ id: string; w: number; h: number; rgba: ArrayBuffer }> = [];
    const infos: LogoInfo[] = [];
    for (const id of ids) {
      if (!id || id === 'ricoh' || id === 'none' || seen.has(id)) continue;
      seen.add(id);
      const row = this.byId.get(id);
      if (!row) continue;
      entries.push({ id: row.id, w: row.w, h: row.h, rgba: row.rgba });
      infos.push(toInfo(row));
    }
    const { logoIndex, logoBlob } = packLogoEntries(entries);
    return {
      logoIndex,
      logoBlob: logoBlob.byteLength ? (logoBlob.buffer as ArrayBuffer) : null,
      infos,
    };
  }

  onChange(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private emit(): void {
    this.rev += 1;
    for (const fn of [...this.listeners]) fn();
  }

  async ensureLoaded(): Promise<LogoInfo[]> {
    if (this.isLoaded) return this.list();
    if (this.loading) return this.loading;
    this.loading = loadAllLogos()
      .then((rows) => {
        this.rows = rows;
        this.byId = new Map(rows.map((row) => [row.id, row]));
        this.isLoaded = true;
        this.loading = null;
        this.emit();
        return this.list();
      })
      .catch(() => {
        this.loading = null;
        this.isLoaded = true;
        return this.list();
      });
    return this.loading;
  }

  canvas(id: string, onReady?: (() => void) | null): HTMLCanvasElement | null {
    const existing = this.canvases.get(id);
    if (existing) return existing;
    const row = this.byId.get(id);
    if (!row) {
      if (!this.isLoaded) {
        void this.ensureLoaded().then(() => {
          if (this.byId.has(id)) onReady?.();
        });
      }
      return null;
    }
    const cv = buildCanvas(row);
    if (cv) this.canvases.set(id, cv);
    return cv;
  }

  async add(
    file: File,
    options: { name: string; keyWhite: boolean; tol: number },
    abortCtrl?: (() => boolean) | AbortSignal,
  ): Promise<{ ok: true; logo: LogoInfo } | { ok: false; error: string }> {
    const signal = typeof abortCtrl === 'function' ? undefined : abortCtrl;
    const isAborted = (): boolean => (typeof abortCtrl === 'function' ? abortCtrl() : Boolean(abortCtrl?.aborted));
    if (isAborted()) return { ok: false, error: '已取消。' };
    await this.ensureLoaded();
    if (isAborted()) return { ok: false, error: '已取消。' };
    if (this.rows.length >= LOGO_LIMITS.maxLogos) {
      return { ok: false, error: LOGO_ERR.limit };
    }
    const prepared = await prepareLogo(file, options);
    if (isAborted()) return { ok: false, error: '已取消。' };
    if (!prepared.ok) return { ok: false, error: prepared.error };
    const stored: StoredLogo = {
      id: makeId(),
      name: prepared.name,
      w: prepared.w,
      h: prepared.h,
      coverage: prepared.coverage,
      opaque: prepared.opaque,
      lum: prepared.lum,
      rgba: prepared.rgba,
      createdAt: Date.now(),
    };
    let wrote = false;
    try {
      wrote = await putStoredLogo(stored, LOGO_LIMITS.maxLogos, signal);
    } catch {
      return { ok: false, error: isAborted() ? '已取消。' : '保存标志失败，请重试。' };
    }
    if (!wrote) return { ok: false, error: isAborted() ? '已取消。' : LOGO_ERR.limit };
    this.rows.push(stored);
    this.byId.set(stored.id, stored);
    const cv = buildCanvas(stored);
    if (cv) this.canvases.set(stored.id, cv);
    this.emit();
    return { ok: true, logo: toInfo(stored) };
  }

  async rename(
    id: string,
    rawName: string,
    abortCtrl?: (() => boolean) | AbortSignal,
  ): Promise<{ ok: true; name: string } | { ok: false; error: string }> {
    const signal = typeof abortCtrl === 'function' ? undefined : abortCtrl;
    const isAborted = (): boolean => (typeof abortCtrl === 'function' ? abortCtrl() : Boolean(abortCtrl?.aborted));
    const trimmed = rawName.trim();
    if (!trimmed) return { ok: false, error: LOGO_ERR.badName };
    if (isAborted()) return { ok: false, error: '已取消。' };
    await this.ensureLoaded();
    if (isAborted()) return { ok: false, error: '已取消。' };
    const next = cleanLogoName(trimmed);
    let wrote = false;
    try {
      wrote = await renameStoredLogo(id, next, signal);
    } catch {
      return { ok: false, error: isAborted() ? '已取消。' : '保存标志名称失败，请重试。' };
    }
    if (!wrote) return { ok: false, error: isAborted() ? '已取消。' : '找不到这个标志。' };
    const row = this.byId.get(id);
    if (row) {
      row.name = next;
      this.emit();
    }
    return { ok: true, name: next };
  }

  async remove(
    id: string,
    abortCtrl?: (() => boolean) | AbortSignal,
  ): Promise<{ ok: true } | { ok: false; error: string }> {
    const signal = typeof abortCtrl === 'function' ? undefined : abortCtrl;
    const isAborted = (): boolean => (typeof abortCtrl === 'function' ? abortCtrl() : Boolean(abortCtrl?.aborted));
    if (isAborted()) return { ok: false, error: '已取消。' };
    await this.ensureLoaded();
    if (isAborted()) return { ok: false, error: '已取消。' };
    let wrote = false;
    try {
      wrote = await deleteStoredLogo(id, signal);
    } catch {
      return { ok: false, error: isAborted() ? '已取消。' : '删除标志失败，请重试。' };
    }
    if (!wrote) return { ok: false, error: '已取消。' };
    this.rows = this.rows.filter((row) => row.id !== id);
    this.byId.delete(id);
    const oldCv = this.canvases.get(id);
    if (oldCv) {
      oldCv.width = 0;
      oldCv.height = 0;
      this.canvases.delete(id);
    }
    this.emit();
    return { ok: true };
  }
}

export const logoStore = new LogoStore();
