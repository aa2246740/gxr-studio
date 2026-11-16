// 这次会话里的相机配方文件和校准数据，以及按（条目、模型）缓存的烘焙表。
// 读文件、核对、烘焙都在 Worker 里；这里只排队、记结果、通知界面。

import type { Engine } from '../../../frontend/src/index';
import type { Bake, CalibPick, CalibRead, CalibRestored, DatRead, Resolved } from '../core/api';
import type { BakeArgs, CalibCheckArgs } from '../core/protocol';
import { modelIso, TEXT, UNIT_ISOS, type Unit, type UnitPack } from '../lib/calib';
import { selection, selectionKey, unitIndex, type Card, type Listing } from '../lib/recipes';
import { ZIP_LIMITS, type ZipFail } from '../lib/zip';
import { whenExportIdle } from './export_pause';

/**
 * 烘焙表一张约 3.8 MB（683×683 的增益网格）。JS 里最多留 16 张，显卡里最多 24 张（Sol r1 的建议，等真机量过再调）：
 * 显卡这边用 24 个固定的键轮流装，同一个键再装一次引擎会销毁旧的那份，所以引擎里我们的表永远不超过 24 张。
 * 舞台正在用的那张不会被换掉。
 */
const KEEP_BAKES = 16;
const GPU_SLOTS = 24;

export interface Pack {
  name: string;
  savedAt: number;
  units: UnitPack[];
  manifestSha256: string;
  files: Map<string, Uint8Array>;
}

export interface Dat {
  name: string;
  read: DatRead;
  /** 代数（旧版相机的旋钮只认 ±5）。 */
  generation: number | null;
  /** `color_listing` 的配方和旋钮。 */
  listing: Listing;
}

export type UnitState =
  | { state: 'none' }
  | { state: 'missing'; message: string }
  | { state: 'checking' }
  | { state: 'ready' }
  | { state: 'invalid'; message: string };

const NOT_SAVED = '校准数据没有存进这台 iPhone：这次能用，下次打开要重新导入。';
const DELETE_FAILED = '删除没有成功，校准数据还在。请再试一次。';

/** 相机配方文件的上限：真的 DAT 不到 1 MB，超过这个一定不是，先拒绝再读。 */
const MAX_DAT_BYTES = 16 << 20;

const ZIP_TEXT: Record<ZipFail, string> = {
  not_zip: '这个文件不是校准数据。请选在 Mac 上导出的「GXR 校准数据」zip 文件。',
  compressed: '这个 zip 被压缩过，读不了。请直接用 Mac 上导出的原文件，不要重新压缩。',
  too_big: '这个文件太大，不是校准数据。请选在 Mac 上导出的「GXR 校准数据」zip 文件。',
  broken: TEXT.unreadable,
};

/**
 * Book 碰到的所有异步边界：读文件、Worker 里的解析和核对、烘焙、IndexedDB。App 传真的进来；
 * 测试换成可控的回复，专门造晚到、乱序的时序。
 */
export interface BookIo {
  /** 读文件字节（Worker 里读完转移过来；主线程不调 File.arrayBuffer）。 */
  readFile(file: File): Promise<ArrayBuffer | null>;
  readDat(bytes: ArrayBuffer): Promise<DatRead>;
  /** 校准数据 zip：大小检查、读取、核对、存 IndexedDB 都在 Worker 里，主线程不碰整包。 */
  importCalib(file: File, savedAt: number): Promise<CalibRead>;
  restoreCalib(): Promise<CalibRestored>;
  bake(args: BakeArgs): Promise<Bake>;
  /** `color_resolve`：配方选择 → 条目（旋钮已经套上）。 */
  resolve(dat: ArrayBuffer, recipe: string): Promise<Resolved>;
  /** `color_calib_check`：这个单元、这个 ISO 的 model_for 检查。 */
  calibCheck(args: CalibCheckArgs): Promise<CalibPick>;
  /** 记一个删除时间；比它早的导入不会再写进去。 */
  /** 写成功返回 true；已经有更新的记录、没写进去返回 false；事务失败抛错。 */
  deletePack(at: number): Promise<boolean>;
  /** IndexedDB 里存下的最高版本（导入和删除都算）。新会话的版本从它之上开始。 */
  watermark(): Promise<number>;
}

export class Book {
  pack: Pack | null = null;
  /** 最近一次导入失败的那一行。导入成功、删除后清掉。 */
  packError: string | null = null;
  packBusy = false;
  dat: Dat | null = null;
  datError: string | null = null;
  /** 选了几个 GXRCE.DAT 时说明用的是哪个。 */
  datNote: string | null = null;
  datBusy = false;
  /**
   * 每个异步操作提交前核对自己的版本（Sol r1）：选新的 DAT、清空都让 DAT 的版本加一；导入、替换、删除、恢复校准数据
   * 让校准的版本加一。晚到的结果只丢掉，不会把清空的状态复活，也不会盖掉后来的选择。
   */
  private datSeq = 0;
  private packSeq = 0;
  /** 导入和删除的时间戳只增不减（IndexedDB 里按它判断谁更新）。 */
  private stamp = 0;
  /** 相机配方文件和校准数据的组合；换了任何一个就加一，旧的检查和烘焙结果作废。 */
  private generation = 0;
  private readonly units = new Map<Unit, { generation: number; state: UnitState }>();
  private readonly bakes = new Map<string, Promise<Bake>>();
  private readonly done = new Map<string, Bake>();
  /** 烘焙表（按身份键）现在装在显卡的哪个固定键上，以及每个固定键上装的是谁；`slotUse` 是最近用过的顺序。 */
  private readonly slotOf = new Map<string, string>();
  private readonly ownerOf = new Map<string, string>();
  private readonly slotUse: string[] = [];
  /** 舞台正在用的那张（身份键）。它的显卡键不会被别的表占掉。 */
  private pinned = '';
  private readonly listeners = new Set<() => void>();
  private engine: Engine | null = null;

  constructor(private readonly io: BookIo) {}

  /** 相机配方文件和校准数据的组合版本；变了就说明旧的烘焙表（和用它画的小图）都作废了。 */
  generationId(): number {
    return this.generation;
  }

  attach(engine: Engine | null): void {
    this.engine = engine;
  }

  onChange(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private changed(): void {
    for (const listener of [...this.listeners]) listener();
  }

  private bump(): void {
    this.generation += 1;
    this.units.clear();
    this.bakes.clear();
    this.done.clear();
    this.resolved.clear();
    this.keyOfSel.clear();
    this.picks.clear();
    // 舞台还在用的那个显卡键先留着（换了相机配方文件或校准数据，新表做好之前照片不变），别的都可以重用。
    const kept = this.slotOf.get(this.pinned);
    this.slotOf.clear();
    this.ownerOf.clear();
    this.slotUse.length = 0;
    if (kept) {
      this.ownerOf.set(kept, this.pinned);
      this.slotUse.push(kept);
    }
  }

  private nextStamp(): number {
    this.stamp = Math.max(Date.now(), this.stamp + 1);
    return this.stamp;
  }

  /** 第一次要版本号时先读一次存下的水位，之后的版本都比它大（时钟回拨也一样）。读不到就按 0。 */
  private floor: Promise<void> | null = null;

  private async stampAbove(): Promise<number> {
    this.floor ??= this.io.watermark().then(
      (mark) => {
        this.stamp = Math.max(this.stamp, mark);
      },
      () => undefined,
    );
    await this.floor;
    return this.nextStamp();
  }

  /** 已经应用到 `this.pack` 的最高持久化版本（IndexedDB 真正提交过的导入或删除墓碑）。 */
  private appliedAt = 0;

  /** 打开页面时把上次导入的校准数据读回来，并像桌面每次用之前那样重新核对（在 Worker 里）。 */
  async restore(): Promise<void> {
    const seq = ++this.packSeq;
    const read = await this.io.restoreCalib();
    if (!read || seq !== this.packSeq || read.savedAt <= this.appliedAt) return;
    this.stamp = Math.max(this.stamp, read.savedAt);
    if (read.ok) {
      this.appliedAt = read.savedAt;
      this.commit(read, read.name, read.savedAt);
    } else this.packError = this.failText(read);
    this.changed();
  }

  async importPack(file: File): Promise<boolean> {
    // 先看大小：选错一个很大的文件，一个字节都不读（Sol r1）。
    if (file.size > ZIP_LIMITS.bytes) {
      this.packSeq += 1;
      this.packBusy = false;
      this.packError = ZIP_TEXT.too_big;
      this.changed();
      return false;
    }
    const seq = ++this.packSeq;
    this.packBusy = true;
    this.packError = null;
    this.changed();
    try {
      const savedAt = await this.stampAbove();
      if (seq !== this.packSeq) return false;
      const read = await this.io.importCalib(file, savedAt);
      if (seq !== this.packSeq) {
        if (read.ok && read.saved && savedAt > this.appliedAt) {
          this.appliedAt = savedAt;
          this.commit(read, file.name, savedAt);
          this.changed();
        }
        return false;
      }
      if (!read.ok) {
        this.packError = this.failText(read);
        return false;
      }
      // 核对过了，这次会话里能用；但只有真的存进去了才算导入成功，否则照实说（Sol r2）。
      if (read.saved) this.appliedAt = Math.max(this.appliedAt, savedAt);
      this.commit(read, file.name, savedAt);
      if (!read.saved) {
        this.packError = NOT_SAVED;
        return false;
      }
      return true;
    } catch (err) {
      if (seq === this.packSeq) this.packError = err instanceof Error && err.message ? err.message : TEXT.unreadable;
      return false;
    } finally {
      if (seq === this.packSeq) {
        this.packBusy = false;
        this.changed();
      }
    }
  }

  /**
   * 删除：先让校准的版本加一（取消还在路上的导入），等 IndexedDB 确认墓碑写进去了才清空；
   * 失败（AbortError 之类，或者已经有更新的记录）就保留原来的包，写一行「删除没有成功」，可以再点一次（Sol r2）。
   */
  async removePack(): Promise<void> {
    const seq = ++this.packSeq;
    this.packBusy = true;
    this.packError = null;
    this.changed();
    try {
      const at = await this.stampAbove();
      if (seq !== this.packSeq) return;
      const wrote = await this.io.deletePack(at);
      if (seq !== this.packSeq) {
        if (wrote && at > this.appliedAt) {
          this.appliedAt = at;
          this.pack = null;
          this.bump();
          this.changed();
        }
        return;
      }
      if (!wrote) throw new Error('tombstone not written');
      this.appliedAt = Math.max(this.appliedAt, at);
      this.pack = null;
      this.packError = null;
      this.bump();
    } catch {
      if (seq === this.packSeq) this.packError = DELETE_FAILED;
    } finally {
      if (seq === this.packSeq) {
        this.packBusy = false;
        this.changed();
      }
    }
  }

  private failText(read: Extract<CalibRead, { ok: false }>): string {
    return read.zip ? ZIP_TEXT[read.zip] : read.message ?? TEXT.unreadable;
  }

  private commit(read: Extract<CalibRead, { ok: true }>, name: string, savedAt: number): void {
    this.pack = { name, savedAt, units: read.units, manifestSha256: read.manifestSha256, files: read.files };
    this.bump();
  }

  /** 这次选上的 GXRCE.DAT（已经挑好最新的那个）。`count` 是一共选了几个。 */
  async setDat(file: File, count: number): Promise<void> {
    const seq = ++this.datSeq;
    this.datNote = count > 1 ? `选了 ${count} 个 GXRCE.DAT，用的是修改时间最新的那个。` : null;
    if (file.size > MAX_DAT_BYTES) {
      this.datBusy = false;
      this.datError = '这个文件太大，不是相机配方文件。请重新选一次 GXRCE.DAT。';
      this.changed();
      return;
    }
    this.datBusy = true;
    this.datError = null;
    this.changed();
    try {
      const bytes = await this.io.readFile(file);
      if (seq !== this.datSeq) return;
      if (!bytes) {
        this.datError = '读不了这个相机配方文件。请重新选一次 GXRCE.DAT。';
        return;
      }
      const read = await this.io.readDat(bytes);
      if (seq !== this.datSeq) return;
      const summary = JSON.parse(read.summary) as { generation?: unknown };
      const listing = JSON.parse(read.listing) as Listing;
      if (read.entries.length !== 24 * 552) throw new Error('条目长度不合格');
      const generation = typeof summary.generation === 'number' ? summary.generation : null;
      this.dat = { name: file.name, read, generation, listing };
      this.bump();
    } catch (err) {
      if (seq === this.datSeq) this.datError = `无法读取相机配方文件：${err instanceof Error ? err.message : String(err)}`;
    } finally {
      if (seq === this.datSeq) {
        this.datBusy = false;
        this.changed();
      }
    }
  }

  clearDat(): void {
    this.datSeq += 1;
    this.datBusy = false;
    this.dat = null;
    this.datError = null;
    this.datNote = null;
    this.bump();
    this.changed();
  }

  private entry(unit: Unit, slot: number): Uint8Array | null {
    if (!this.dat || slot < 1 || slot > 8) return null;
    const at = (unitIndex(unit) * 8 + slot - 1) * 552;
    return this.dat.read.entries.subarray(at, at + 552);
  }

  /** 这个单元两个 ISO 分支 model_for 选出来的 ctx、ring（核对通过之后才有）。 */
  private readonly picks = new Map<Unit, { generation: number; names: Record<number, { ctx: string; ring: string }> }>();

  private files(unit: Unit, iso: 200 | 1600): { ctx: Uint8Array; ring: Uint8Array } | null {
    const pick = this.picks.get(unit);
    const pair = pick && pick.generation === this.generation ? pick.names[iso] : undefined;
    const ctx = pair ? this.pack?.files.get(pair.ctx) : undefined;
    const ring = pair ? this.pack?.files.get(pair.ring) : undefined;
    return ctx && ring ? { ctx, ring } : null;
  }

  /**
   * 这个单元能不能用校准模型预测。第一次问时在后台交给核心的 `color_calib_check`（桌面 model_for 的同一段检查：
   * 版本、单元、原厂 04 指纹、文件清单、SHA-256，再建模型），两个 ISO 分支各一次。核对完会通知界面。
   */
  unitState(unit: Unit): UnitState {
    if (!this.pack) return { state: 'none' };
    const row = this.pack.units.find((u) => u.unit === unit);
    if (!row) return { state: 'missing', message: TEXT.unitMissing };
    if (!this.dat) return { state: 'checking' };
    const known = this.units.get(unit);
    if (known && known.generation === this.generation) return known.state;
    const generation = this.generation;
    this.units.set(unit, { generation, state: { state: 'checking' } });
    void this.checkUnit(unit, row, generation).then((state) => {
      if (generation !== this.generation) return;
      this.units.set(unit, { generation, state });
      this.changed();
    });
    return { state: 'checking' };
  }

  private async checkUnit(unit: Unit, _row: UnitPack, generation: number): Promise<UnitState> {
    const stock = this.entry(unit, 4);
    const pack = this.pack;
    const manifest = pack?.files.get('manifest.json');
    if (!stock || !pack || !manifest) return { state: 'invalid', message: TEXT.unreadable };
    const names = [...pack.files.keys()].filter((name) => name !== 'manifest.json');
    const found: Record<number, { ctx: string; ring: string }> = {};
    for (const iso of [200, 1600] as const) {
      try {
        const pick = await this.io.calibCheck({
          manifest: copy(manifest),
          names,
          blobs: names.map((name) => copy(pack.files.get(name) ?? new Uint8Array())),
          stock: copy(stock),
          unit,
          iso,
        });
        found[pick.model_iso] = { ctx: pick.ctx, ring: pick.ring };
      } catch (err) {
        // 文案就是桌面 unit_status 的：「缺少」开头的算没有这个单元，其余算失效。
        const message = err instanceof Error && err.message ? err.message : TEXT.unreadable;
        return message.startsWith('缺少') && !message.startsWith('缺少校准文件') ? { state: 'missing', message } : { state: 'invalid', message };
      }
      if (generation !== this.generation) return { state: 'checking' };
    }
    this.picks.set(unit, { generation, names: found });
    return { state: 'ready' };
  }

  /** 实拍校准过的 ISO（给界面写「50mm · ISO 200、1600」）。 */
  isos(unit: Unit): number[] {
    return UNIT_ISOS[unit];
  }

  /** 配方选择 → 条目，按（这一份 DAT 和校准、选择）缓存。 */
  private readonly resolved = new Map<string, Promise<Resolved>>();
  /** （模型、选择）→ 烘焙表的键；resolve 过一次以后才知道（条目 sha256 要 resolve 才有）。 */
  private readonly keyOfSel = new Map<string, string>();

  private resolveSel(sel: Record<string, unknown>): Promise<Resolved> {
    const dat = this.dat;
    if (!dat) return Promise.reject(new Error('找不到这个配方。请重新选择。'));
    const id = `${this.generation}|${selectionKey(sel)}`;
    let job = this.resolved.get(id);
    if (!job) {
      job = this.io.resolve(dat.read.bytes.slice(0), JSON.stringify(sel));
      this.resolved.set(id, job);
      job.catch(() => {
        if (this.resolved.get(id) === job) this.resolved.delete(id);
      });
    }
    return job;
  }

  /**
   * 一张卡（带这些档位）在这张照片上的烘焙表的键；还没 resolve 过返回 null。缓存按（单元、模型、条目 sha256）：
   * 不同的选择 resolve 出同一个条目时共用一张表。
   */
  bakeKey(card: Card, unit: Unit, iso: number | null, levels?: Record<string, number>): string | null {
    return this.keyOfSel.get(`${this.generation}|${modelIso(iso)}|${selectionKey(selection(card, unit, levels))}`) ?? null;
  }

  /**
   * 做好（或拿缓存）这张卡的烘焙表，返回它的键（还没装进显卡，用之前调 gpuKey）。先 `color_resolve` 拿到旋钮套好的
   * 条目（和桌面 CameraData.resolve 一样，Looks、用户配方、r5 记录都行），再 `color_bake`。失败抛核心的原话。
   */
  async bake(card: Card, unit: Unit, iso: number | null, levels?: Record<string, number>): Promise<string> {
    if (!this.dat || !card.controls.length) throw new Error('找不到这个配方。请重新选择。');
    const generation = this.generation;
    const model = modelIso(iso);
    const sel = selection(card, unit, levels);
    const got = await this.resolveSel(sel);
    if (generation !== this.generation) throw new Error('相机配方文件或校准数据换了');
    const key = `bake:${generation}:${unit}:${model}:${got.entrySha256}`;
    this.keyOfSel.set(`${generation}|${model}|${selectionKey(sel)}`, key);
    let job = this.bakes.get(key);
    if (!job) {
      const stock = this.entry(unit, 4);
      const files = this.files(unit, model);
      if (!stock || !files) throw new Error(TEXT.filesMissing);
      // 导出跑着时新的烘焙先等它停（UI-M3）；等完再核对一次相机配方文件和校准有没有换。
      const entry = got.entry;
      job = whenExportIdle().then(() => {
        if (generation !== this.generation) throw new Error('相机配方文件或校准数据换了');
        return this.io.bake({ entry: copy(entry), stock: copy(stock), menu: '', ctx: copy(files.ctx), ring: copy(files.ring), params: '{"step":0.375}' });
      });
      this.bakes.set(key, job);
      job.then(
        (made) => {
          if (this.bakes.get(key) === job) this.keep(key, made);
        },
        () => {
          if (this.bakes.get(key) === job) this.bakes.delete(key);
        },
      );
    }
    await job;
    return key;
  }

  /** 已经做好、拿得到显卡键就是 true。 */
  ready(key: string): boolean {
    return this.gpuKey(key) !== null;
  }

  /**
   * 这张表在显卡里的键；还没装就现在装（占一个空的或最久没用的固定键，不动舞台那张）。JS 里已经没有了返回 null。
   * 拿到键以后要在同一轮里用掉（setLook、thumb 提交命令都是同步的），之后它可能被别的表占用。
   */
  gpuKey(key: string): string | null {
    const slot = this.slotOf.get(key);
    if (slot && this.ownerOf.get(slot) === key && this.engine?.hasBake(slot)) {
      this.touch(slot);
      const made = this.done.get(key);
      if (made) this.keep(key, made);
      return slot;
    }
    const made = this.done.get(key);
    if (!made || !this.engine) return null;
    this.keep(key, made);
    const next = this.freeSlot();
    if (!next) return null;
    const old = this.ownerOf.get(next);
    if (old) this.slotOf.delete(old);
    this.engine.addBake(next, made.meta, made.data);
    this.slotOf.set(key, next);
    this.ownerOf.set(next, key);
    this.touch(next);
    if (import.meta.env.DEV) recordBake(key, made);
    return next;
  }

  /** 舞台换成这张了：它的显卡键从此不让出去。 */
  pin(key: string): void {
    this.pinned = key;
  }

  private touch(slot: string): void {
    const at = this.slotUse.indexOf(slot);
    if (at >= 0) this.slotUse.splice(at, 1);
    this.slotUse.push(slot);
  }

  private freeSlot(): string | null {
    if (this.slotUse.length < GPU_SLOTS) {
      for (let i = 0; i < GPU_SLOTS; i++) {
        const slot = `recipe-gpu-${i}`;
        if (!this.slotUse.includes(slot)) return slot;
      }
    }
    return this.slotUse.find((slot) => this.ownerOf.get(slot) !== this.pinned) ?? null;
  }

  /** 记下一张做好的表，按最近用过的顺序；超过上限丢最久没用的。 */
  private keep(key: string, made: Bake): void {
    this.done.delete(key);
    this.done.set(key, made);
    while (this.done.size > KEEP_BAKES) {
      const oldest = this.done.keys().next().value;
      if (oldest === undefined) break;
      this.done.delete(oldest);
      this.bakes.delete(oldest);
    }
  }

}

function copy(view: Uint8Array): ArrayBuffer {
  const out = new Uint8Array(view.length);
  out.set(view);
  return out.buffer;
}

/** 只在开发服务器里：测试要拿 UI 交给显卡的烘焙表字节，和桌面的逐字节比。 */
function recordBake(key: string, made: Bake): void {
  const scope = window as unknown as { __gxrBakes?: Map<string, Bake> };
  scope.__gxrBakes ??= new Map();
  scope.__gxrBakes.set(key, made);
}
