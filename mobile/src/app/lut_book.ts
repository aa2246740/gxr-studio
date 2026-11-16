// 用户 LUT 目录（元数据在内存，.cube 字节与完整目录只在 Worker / IndexedDB 里）。
// 每次导入、改名、改输入色彩空间或删除都带着单调递增版本写 IndexedDB；删除先确认写完再清内存。

import { deleteLut, importLutFile, patchLut, readLutBlob, restoreLutLibrary, type LutImportReply, type LutLibraryMeta } from '../core/api';
import type { LutPatchFields } from '../lib/idb';
import {
  LUT_ERRORS,
  MAX_CUBE_BYTES,
  classifyLutImports,
  cleanLutLabel,
  deletedLutToken,
  ignoredFilesMessage,
  lutRenderToken,
  lutTargetSpec,
  type GamutId,
  type LutRecord,
  type RangeKind,
  type SpaceId,
} from '../lib/lut';

export interface LutBookIo {
  importLut(file: File, filename: string, version: number): Promise<LutImportReply>;
  restore(): Promise<LutLibraryMeta>;
  patch(cubeId: string, patch: LutPatchFields, version: number): Promise<{ ok: true; record: LutRecord } | { ok: false; reason: 'not_found' | 'stale' }>;
  remove(cubeId: string, version: number): Promise<boolean>;
  readBlob(cubeId: string): Promise<ArrayBuffer | null>;
}

const defaultIo: LutBookIo = {
  importLut: importLutFile,
  restore: restoreLutLibrary,
  patch: patchLut,
  remove: deleteLut,
  readBlob: readLutBlob,
};

export interface LutImportOutcome {
  added: LutRecord[];
  duplicates: number;
  ignored: number;
  errors: string[];
  message: string;
}

export interface LutSpaceUpdate {
  input_space: SpaceId;
  input_gamut?: GamutId | null;
  output_gamut?: GamutId | null;
  input_range?: RangeKind;
  output_range?: RangeKind;
}

export class LutBook {
  private records: LutRecord[] = [];
  private byId = new Map<string, LutRecord>();
  private totalBytes = 0;
  private stamp = 0;
  private restored = false;
  private restoreTask: Promise<void> | null = null;
  private noticeSeq = 0;
  private noticeText = '';
  private readonly listeners = new Set<() => void>();

  constructor(private readonly io: LutBookIo = defaultIo) {}

  onChange(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private changed(): void {
    for (const fn of [...this.listeners]) fn();
  }

  lastNotice(): { seq: number; text: string } {
    return { seq: this.noticeSeq, text: this.noticeText };
  }

  private nextVersion(): number {
    this.stamp = Math.max(Date.now(), this.stamp + 1);
    return this.stamp;
  }

  list(): readonly LutRecord[] {
    return this.records;
  }

  get(cubeId: string | null | undefined): LutRecord | undefined {
    return cubeId ? this.byId.get(cubeId) : undefined;
  }

  bytes(): number {
    return this.totalBytes;
  }

  /** 这条 LUT 此刻对应的渲染/缓存令牌；已删除给 `:deleted` 摘要，不会碰撞旧缓存。 */
  renderToken(cubeId: string): string {
    const rec = this.byId.get(cubeId);
    return rec ? lutRenderToken(rec) : deletedLutToken(cubeId);
  }

  restore(): Promise<void> {
    if (this.restored) return Promise.resolve();
    this.restoreTask ??= this.io
      .restore()
      .then((meta) => {
        this.stamp = Math.max(this.stamp, meta.watermark);
        // 合并恢复前新导入的记录（若用户在 restore 完成前已经操作过）。
        const merged = new Map<string, LutRecord>();
        for (const r of meta.records) merged.set(r.cube_id, r);
        for (const r of this.records) merged.set(r.cube_id, r);
        this.records = [...merged.values()];
        this.byId = new Map(this.records.map((r) => [r.cube_id, r]));
        this.totalBytes = this.records.reduce((s, r) => s + r.bytes, 0);
        this.restored = true;
        this.changed();
      })
      .catch(() => {
        this.restored = true;
      });
    return this.restoreTask;
  }

  async importFiles(files: readonly File[]): Promise<LutImportOutcome> {
    await this.restore();
    const { cubes, ignored } = classifyLutImports(files);
    const added: LutRecord[] = [];
    const errors: string[] = [];
    let duplicates = 0;

    for (const item of cubes) {
      if (item.file.size > MAX_CUBE_BYTES) {
        errors.push(`${item.name}：${LUT_ERRORS.cube_too_big}`);
        continue;
      }
      const res = await this.io.importLut(item.file, item.name, this.nextVersion());
      if (!res.ok) {
        if (res.message) errors.push(`${item.name}：${res.message}`);
        continue;
      }
      this.totalBytes = res.totalBytes;
      if (res.duplicate) {
        duplicates += 1;
        if (!this.byId.has(res.record.cube_id)) {
          this.records.push(res.record);
          this.byId.set(res.record.cube_id, res.record);
        }
      } else {
        this.upsert(res.record);
        added.push(res.record);
      }
    }

    const parts: string[] = [];
    if (added.length) parts.push(added.length === 1 ? `已导入「${added[0].name}」` : `已导入 ${added.length} 个 LUT`);
    if (duplicates) parts.push(duplicates === 1 && !added.length && !errors.length ? LUT_ERRORS.duplicate : `${duplicates} 个${LUT_ERRORS.duplicate}`);
    if (ignored) parts.push(ignoredFilesMessage(ignored));
    if (errors.length) parts.push(errors[0]);
    const message = parts.join(' ');
    if (message) {
      this.noticeSeq += 1;
      this.noticeText = message;
    }

    if (added.length || duplicates || ignored || errors.length) this.changed();

    return {
      added,
      duplicates,
      ignored,
      errors,
      message,
    };
  }

  async rename(cubeId: string, nextName: string): Promise<{ ok: true; record: LutRecord } | { ok: false; message: string }> {
    await this.restore();
    let cleaned: string;
    try {
      cleaned = cleanLutLabel(nextName);
    } catch (err) {
      return { ok: false, message: err instanceof Error ? err.message : LUT_ERRORS.bad_name };
    }
    const res = await this.io.patch(cubeId, { name: cleaned }, this.nextVersion());
    if (!res.ok) {
      return { ok: false, message: res.reason === 'not_found' ? LUT_ERRORS.cube_not_found : '保存未生效，请重试。' };
    }
    this.upsert(res.record);
    this.changed();
    return { ok: true, record: res.record };
  }

  async setSpace(cubeId: string, update: LutSpaceUpdate): Promise<{ ok: true; record: LutRecord } | { ok: false; message: string }> {
    await this.restore();
    const current = this.byId.get(cubeId);
    if (!current) return { ok: false, message: LUT_ERRORS.cube_not_found };
    const patch: LutPatchFields = {
      input_space: update.input_space,
      output_space: 'srgb',
      input_gamut: update.input_gamut ?? current.input_gamut ?? null,
      output_gamut: update.output_gamut !== undefined ? update.output_gamut : current.output_gamut ?? null,
      input_range: update.input_range ?? current.input_range ?? 'full',
      output_range: update.output_range ?? current.output_range ?? 'full',
      input_space_manual: true,
      input_space_state: 'manual',
    };
    const res = await this.io.patch(cubeId, patch, this.nextVersion());
    if (!res.ok) {
      return { ok: false, message: res.reason === 'not_found' ? LUT_ERRORS.cube_not_found : '保存未生效，请重试。' };
    }
    this.upsert(res.record);
    this.changed();
    return { ok: true, record: res.record };
  }

  /** 先在 IndexedDB 写删除，确认删掉才清内存并通知订阅者（Sol r2 原则）。 */
  async remove(cubeId: string): Promise<boolean> {
    await this.restore();
    const deleted = await this.io.remove(cubeId, this.nextVersion());
    if (!deleted) return false;
    this.records = this.records.filter((r) => r.cube_id !== cubeId);
    this.byId.delete(cubeId);
    this.totalBytes = this.records.reduce((s, r) => s + r.bytes, 0);
    this.changed();
    return true;
  }

  /** 在测试或导出开始前按当前引用定格一组 LUT 的字节和目标色彩空间。 */
  async exportSnapshot(cubeIds: readonly string[]): Promise<Record<string, { cube: ArrayBuffer; space: string }>> {
    await this.restore();
    const out: Record<string, { cube: ArrayBuffer; space: string }> = {};
    for (const id of new Set(cubeIds)) {
      const rec = this.byId.get(id);
      if (!rec) continue;
      const blob = await this.io.readBlob(id);
      if (!blob) continue;
      const spec = lutTargetSpec(rec);
      out[id] = { cube: blob, space: spec ? JSON.stringify(spec) : '' };
    }
    return out;
  }

  private upsert(record: LutRecord): void {
    const idx = this.records.findIndex((r) => r.cube_id === record.cube_id);
    if (idx >= 0) this.records[idx] = record;
    else this.records.push(record);
    this.byId.set(record.cube_id, record);
    this.totalBytes = this.records.reduce((s, r) => s + r.bytes, 0);
  }
}
