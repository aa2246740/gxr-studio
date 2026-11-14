// 这次会话里的相机配方文件和校准数据，以及按（条目、模型）缓存的烘焙表。
// 读文件、核对、烘焙都在 Worker 里；这里只排队、记结果、通知界面。

import type { Engine } from '../../../frontend/src/index';
import type { Bake, CalibRead, CalibRestored, DatRead } from '../core/api';
import type { BakeArgs } from '../core/protocol';
import { modelIso, sha256, TEXT, UNIT_ISOS, type Unit, type UnitPack } from '../lib/calib';
import { bakeParams, unitIndex, type Card, type DatSummary, type Menu } from '../lib/recipes';
import { ZIP_LIMITS, type ZipFail } from '../lib/zip';

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
  summary: DatSummary;
  menu: Menu;
}

export type UnitState =
  | { state: 'none' }
  | { state: 'missing'; message: string }
  | { state: 'checking' }
  | { state: 'ready' }
  | { state: 'invalid'; message: string };

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
  /** 记一个删除时间；比它早的导入不会再写进去。 */
  deletePack(at: number): Promise<void>;
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

  /** 打开页面时把上次导入的校准数据读回来，并像桌面每次用之前那样重新核对（在 Worker 里）。 */
  async restore(): Promise<void> {
    const seq = ++this.packSeq;
    const read = await this.io.restoreCalib();
    if (!read || seq !== this.packSeq) return;
    this.stamp = Math.max(this.stamp, read.savedAt);
    if (read.ok) this.commit(read, read.name, read.savedAt);
    else this.packError = this.failText(read);
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
    const savedAt = this.nextStamp();
    this.packBusy = true;
    this.packError = null;
    this.changed();
    try {
      const read = await this.io.importCalib(file, savedAt);
      if (seq !== this.packSeq) return false;
      if (!read.ok) {
        this.packError = this.failText(read);
        return false;
      }
      this.commit(read, file.name, savedAt);
      if (!read.saved) this.packError = '校准数据存不进这台 iPhone。请检查存储空间后再试。';
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

  async removePack(): Promise<void> {
    this.packSeq += 1;
    this.packBusy = false;
    this.pack = null;
    this.packError = null;
    this.bump();
    try {
      await this.io.deletePack(this.nextStamp());
    } finally {
      this.changed();
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
      const summary = JSON.parse(read.summary) as DatSummary;
      const menu = JSON.parse(read.menu) as Menu;
      if (read.entries.length !== 24 * 552) throw new Error('条目长度不合格');
      this.dat = { name: file.name, read, summary, menu };
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

  private files(unit: Unit, iso: 200 | 1600): { ctx: Uint8Array; ring: Uint8Array } | null {
    const row = this.pack?.units.find((u) => u.unit === unit);
    const pair = row?.models[String(iso)];
    const ctx = pair ? this.pack?.files.get(pair.ctx) : undefined;
    const ring = pair ? this.pack?.files.get(pair.ring) : undefined;
    return ctx && ring ? { ctx, ring } : null;
  }

  /**
   * 这个单元能不能用校准模型预测（`calib.unit_status` + `model_for`）。第一次问时在后台核对：
   * 原厂 04 的指纹，再用两个 ISO 分支各建一次模型。核对完会通知界面。
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

  private async checkUnit(unit: Unit, row: UnitPack, generation: number): Promise<UnitState> {
    const stock = this.entry(unit, 4);
    if (!stock || !this.dat) return { state: 'invalid', message: TEXT.unreadable };
    if ((await sha256(stock.subarray(115, 211))) !== row.stockSha256) return { state: 'invalid', message: TEXT.stock };
    for (const iso of [200, 1600] as const) {
      const files = this.files(unit, iso);
      if (!files || generation !== this.generation) return { state: 'invalid', message: TEXT.filesMissing };
      try {
        await this.io.bake({ entry: copy(stock), stock: copy(stock), menu: '', ctx: copy(files.ctx), ring: copy(files.ring), params: '{}' });
      } catch {
        return { state: 'invalid', message: TEXT.unreadable };
      }
    }
    return { state: 'ready' };
  }

  /** 实拍校准过的 ISO（给界面写「50mm · ISO 200、1600」）。 */
  isos(unit: Unit): number[] {
    return UNIT_ISOS[unit];
  }

  /** 一张卡在这张照片上的烘焙表的键（显卡里的 bake key）。缓存按（单元、模型、底子 sha256、档位）。 */
  bakeKey(card: Card, unit: Unit, iso: number | null): string | null {
    if (!card.levels || !card.entrySha256 || card.pending) return null;
    return `bake:${this.generation}:${unit}:${modelIso(iso)}:${card.entrySha256}:${card.levels.join(',')}`;
  }

  /** 做好（或拿缓存）这张卡的烘焙表，返回它的身份键（还没装进显卡，用之前调 gpuKey）。失败抛核心的原话。 */
  async bake(card: Card, unit: Unit, iso: number | null): Promise<string> {
    const key = this.bakeKey(card, unit, iso);
    const params = bakeParams(card, unit);
    if (!key || !params || !this.dat) throw new Error(card.error ?? '找不到这个配方。请重新选择。');
    let job = this.bakes.get(key);
    if (!job) {
      const base = this.entry(unit, card.slot);
      const stock = this.entry(unit, 4);
      const files = this.files(unit, modelIso(iso));
      if (!base || !stock || !files) throw new Error(TEXT.filesMissing);
      job = this.io.bake({ entry: copy(base), stock: copy(stock), menu: this.dat.read.menu, ctx: copy(files.ctx), ring: copy(files.ring), params });
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
