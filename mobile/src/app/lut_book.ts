// 用户 LUT 目录（元数据在内存，.cube 字节与完整目录只在 Worker / IndexedDB 里）。
// 每次导入、改名、改输入色彩空间或删除都带着单调递增版本写 IndexedDB；删除先确认写完再清内存。

import { deleteLut, importLutFile, patchLut, readLutBatchSnapshot, readLutBlob, restoreLutLibrary, type LutImportReply, type LutLibraryMeta } from '../core/api';
import type { LutPatchFields, LutSnapshotRow } from '../lib/idb';
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
  snapshotBatch?(cubeIds: readonly string[]): Promise<LutSnapshotRow[]>;
}

const defaultIo: LutBookIo = {
  importLut: importLutFile,
  restore: restoreLutLibrary,
  patch: patchLut,
  remove: deleteLut,
  readBlob: readLutBlob,
  snapshotBatch: readLutBatchSnapshot,
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
  output_space?: SpaceId;
  input_gamut?: GamutId | null;
  output_gamut?: GamutId | null;
  input_range?: RangeKind;
  output_range?: RangeKind;
}

function errorText(err: unknown, fallback: string): string {
  const raw = err instanceof Error ? err.message : typeof err === 'string' ? err : '';
  return raw.trim() ? raw.trim() : fallback;
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
  private readonly io: LutBookIo;
  private readonly listeners = new Set<() => void>();
  private readonly snapshotBarriers = new Set<Promise<void>>();

  constructor(io: LutBookIo = defaultIo) {
    this.io = io;
  }

  private async waitForSnapshots(): Promise<void> {
    while (this.snapshotBarriers.size > 0) {
      await Promise.all([...this.snapshotBarriers]);
    }
  }

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
      let res: LutImportReply;
      try {
        res = await this.io.importLut(item.file, item.name, this.nextVersion());
      } catch (err) {
        errors.push(`${item.name}：${errorText(err, LUT_ERRORS.cube_unreadable)}`);
        continue;
      }
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
    await this.waitForSnapshots();
    let res: { ok: true; record: LutRecord } | { ok: false; reason: 'not_found' | 'stale' };
    try {
      res = await this.io.patch(cubeId, { name: cleaned }, this.nextVersion());
    } catch {
      return { ok: false, message: '保存未生效，请重试。' };
    }
    if (!res.ok) {
      return { ok: false, message: res.reason === 'not_found' ? LUT_ERRORS.cube_not_found : '保存未生效，请重试。' };
    }
    this.upsert(res.record);
    this.changed();
    return { ok: true, record: res.record };
  }

  async setSpace(cubeId: string, update: LutSpaceUpdate): Promise<{ ok: true; record: LutRecord } | { ok: false; message: string }> {
    await this.restore();
    await this.waitForSnapshots();
    const current = this.byId.get(cubeId);
    if (!current) return { ok: false, message: LUT_ERRORS.cube_not_found };
    const patch: LutPatchFields = {
      input_space: update.input_space,
      output_space: update.output_space ?? current.output_space ?? 'srgb',
      input_gamut: update.input_gamut !== undefined ? update.input_gamut : current.input_gamut ?? null,
      output_gamut: update.output_gamut !== undefined ? update.output_gamut : current.output_gamut ?? null,
      input_range: update.input_range ?? current.input_range ?? 'full',
      output_range: update.output_range ?? current.output_range ?? 'full',
      input_space_manual: true,
      input_space_state: 'manual',
    };
    let res: { ok: true; record: LutRecord } | { ok: false; reason: 'not_found' | 'stale' };
    try {
      res = await this.io.patch(cubeId, patch, this.nextVersion());
    } catch {
      return { ok: false, message: '保存未生效，请重试。' };
    }
    if (!res.ok) {
      return { ok: false, message: res.reason === 'not_found' ? LUT_ERRORS.cube_not_found : '保存未生效，请重试。' };
    }
    this.upsert(res.record);
    this.changed();
    return { ok: true, record: res.record };
  }

  /** 先在 IndexedDB 写删除，确认删掉才清内存并通知订阅者；事务失败时不动内存并返回错误信息。 */
  async removeChecked(cubeId: string): Promise<{ ok: true } | { ok: false; message: string }> {
    await this.restore();
    await this.waitForSnapshots();
    let deleted = false;
    try {
      deleted = await this.io.remove(cubeId, this.nextVersion());
    } catch {
      return { ok: false, message: '删除没有成功，请再试一次。' };
    }
    if (!deleted) return { ok: false, message: '删除没有成功，请再试一次。' };
    this.records = this.records.filter((r) => r.cube_id !== cubeId);
    this.byId.delete(cubeId);
    this.totalBytes = this.records.reduce((s, r) => s + r.bytes, 0);
    this.changed();
    return { ok: true };
  }

  /** 先在 IndexedDB 写删除，确认删掉才清内存并通知订阅者（Sol r2 原则）。 */
  async remove(cubeId: string): Promise<boolean> {
    const res = await this.removeChecked(cubeId);
    return res.ok;
  }

  /** 在测试或导出开始前同步定格一组 LUT 的目标色彩空间，并用单事务批量快照读取原始字节（P2-02），同时登记写屏障阻止后续改动/删除抢跑。 */
  exportSnapshot(cubeIds: readonly string[]): Promise<Record<string, { cube: ArrayBuffer; space: string }>> {
    const unique = [...new Set(cubeIds)];
    let task: Promise<Record<string, { cube: ArrayBuffer; space: string }>>;
    try {
      task = !this.restored ? this.restore().then(() => this.captureSnapshotSync(unique)) : this.captureSnapshotSync(unique);
    } catch (err) {
      task = Promise.reject(err);
    }
    let barrier!: Promise<void>;
    barrier = task.then(
      () => {
        this.snapshotBarriers.delete(barrier);
      },
      () => {
        this.snapshotBarriers.delete(barrier);
      },
    );
    this.snapshotBarriers.add(barrier);
    return task;
  }

  private captureSnapshotSync(uniqueIds: readonly string[]): Promise<Record<string, { cube: ArrayBuffer; space: string }>> {
    const frozenSpecs = new Map<string, string>();
    for (const id of uniqueIds) {
      const rec = this.byId.get(id);
      if (!rec) continue;
      const spec = lutTargetSpec(rec);
      frozenSpecs.set(id, spec ? JSON.stringify(spec) : '');
    }
    const ids = [...frozenSpecs.keys()];
    if (!ids.length) return Promise.resolve({});

    if (this.io.snapshotBatch) {
      return this.io.snapshotBatch(ids).then((rows) => {
        const out: Record<string, { cube: ArrayBuffer; space: string }> = {};
        for (const row of rows) {
          const specStr = frozenSpecs.get(row.cubeId);
          if (specStr === undefined || !row.blob) continue;
          out[row.cubeId] = { cube: row.blob, space: specStr };
        }
        return out;
      });
    }

    // 同步发起全部 readBlob 请求（兼容仅提供 readBlob 的测试桩），确保在任意 await 前锁定全部读取。
    const reads = ids.map((id) => ({
      id,
      spec: frozenSpecs.get(id) ?? '',
      blobTask: this.io.readBlob(id),
    }));
    return Promise.all(
      reads.map(async (item) => {
        const blob = await item.blobTask;
        return blob ? ([item.id, { cube: blob, space: item.spec }] as const) : null;
      }),
    ).then((entries) => {
      const out: Record<string, { cube: ArrayBuffer; space: string }> = {};
      for (const entry of entries) {
        if (entry) out[entry[0]] = entry[1];
      }
      return out;
    });
  }

  private upsert(record: LutRecord): void {
    const idx = this.records.findIndex((r) => r.cube_id === record.cube_id);
    if (idx >= 0) this.records[idx] = record;
    else this.records.push(record);
    this.byId.set(record.cube_id, record);
    this.totalBytes = this.records.reduce((s, r) => s + r.bytes, 0);
  }
}
