// 这次会话里的相机配方文件和校准数据，以及按（条目、模型）缓存的烘焙表。
// 读文件、核对、烘焙都在 Worker 里；这里只排队、记结果、通知界面。

import type { Engine } from '../../../frontend/src/index';
import { bakeIn, readCalib, readDat, type Bake, type DatRead } from '../core/api';
import { modelIso, sha256, TEXT, UNIT_ISOS, type Unit, type UnitPack } from '../lib/calib';
import { deletePack, loadPack, savePack } from '../lib/idb';
import { bakeParams, unitIndex, type Card, type DatSummary, type Menu } from '../lib/recipes';
import type { ZipFail } from '../lib/zip';

/**
 * 做好的烘焙表在 JS 里最多留几张。一张约 3.8 MB（683×683 的增益网格），一个单元一个模型 8 个内置加几个用户配方，
 * 两个模型也装得下；更早的丢掉，要用时再做（显卡那边自己最多留 64 张）。
 */
const KEEP_BAKES = 24;

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

const ZIP_TEXT: Record<ZipFail, string> = {
  not_zip: '这个文件不是校准数据。请选在 Mac 上导出的「GXR 校准数据」zip 文件。',
  compressed: '这个 zip 被压缩过，读不了。请直接用 Mac 上导出的原文件，不要重新压缩。',
  too_big: '这个文件太大，不是校准数据。请选在 Mac 上导出的「GXR 校准数据」zip 文件。',
  broken: TEXT.unreadable,
};

/** 读文件字节的办法（Worker 里读完转移过来；主线程不调 File.arrayBuffer）。 */
export type ReadFile = (file: File) => Promise<ArrayBuffer | null>;

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
  /** 相机配方文件和校准数据的组合；换了任何一个就加一，旧的检查和烘焙结果作废。 */
  private generation = 0;
  private readonly units = new Map<Unit, { generation: number; state: UnitState }>();
  private readonly bakes = new Map<string, Promise<Bake>>();
  private readonly done = new Map<string, Bake>();
  private readonly listeners = new Set<() => void>();
  private engine: Engine | null = null;

  constructor(private readonly readFile: ReadFile) {}

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
  }

  /** 打开页面时把上次导入的校准数据读回来，并像桌面每次用之前那样重新核对。 */
  async restore(): Promise<void> {
    const stored = await loadPack();
    if (!stored) return;
    const out = await this.check(stored.zip.slice(0), stored.name, stored.savedAt);
    if (!out.ok) this.packError = out.message;
    this.changed();
  }

  async importPack(file: File): Promise<boolean> {
    this.packBusy = true;
    this.packError = null;
    this.changed();
    try {
      const bytes = await this.readFile(file);
      if (!bytes) {
        this.packError = '读不了这个文件。请重新选一次。';
        return false;
      }
      const keep = bytes.slice(0);
      const savedAt = Date.now();
      const out = await this.check(bytes, file.name, savedAt);
      if (!out.ok) {
        this.packError = out.message;
        return false;
      }
      try {
        await savePack({ zip: keep, name: file.name, savedAt });
      } catch {
        this.packError = '校准数据存不进这台 iPhone。请检查存储空间后再试。';
      }
      return true;
    } catch (err) {
      this.packError = err instanceof Error && err.message ? err.message : TEXT.unreadable;
      return false;
    } finally {
      this.packBusy = false;
      this.changed();
    }
  }

  async removePack(): Promise<void> {
    this.pack = null;
    this.packError = null;
    this.bump();
    try {
      await deletePack();
    } finally {
      this.changed();
    }
  }

  private async check(zip: ArrayBuffer, name: string, savedAt: number): Promise<{ ok: true } | { ok: false; message: string }> {
    const read = await readCalib(zip);
    if (!read.ok) return { ok: false, message: read.zip ? ZIP_TEXT[read.zip] : read.message ?? TEXT.unreadable };
    this.pack = { name, savedAt, units: read.units, manifestSha256: read.manifestSha256, files: read.files };
    this.bump();
    return { ok: true };
  }

  /** 这次选上的 GXRCE.DAT（已经挑好最新的那个）。`count` 是一共选了几个。 */
  async setDat(file: File, count: number): Promise<void> {
    this.datBusy = true;
    this.datError = null;
    this.datNote = count > 1 ? `选了 ${count} 个 GXRCE.DAT，用的是修改时间最新的那个。` : null;
    this.changed();
    try {
      const bytes = await this.readFile(file);
      if (!bytes) {
        this.datError = '读不了这个相机配方文件。请重新选一次 GXRCE.DAT。';
        return;
      }
      const read = await readDat(bytes);
      const summary = JSON.parse(read.summary) as DatSummary;
      const menu = JSON.parse(read.menu) as Menu;
      if (read.entries.length !== 24 * 552) throw new Error('条目长度不合格');
      this.dat = { name: file.name, read, summary, menu };
      this.bump();
    } catch (err) {
      this.datError = `无法读取相机配方文件：${err instanceof Error ? err.message : String(err)}`;
    } finally {
      this.datBusy = false;
      this.changed();
    }
  }

  clearDat(): void {
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
        await bakeIn({ entry: copy(stock), stock: copy(stock), menu: '', ctx: copy(files.ctx), ring: copy(files.ring), params: '{}' });
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

  /** 做好（或拿缓存）这张卡的烘焙表，装进显卡，返回键。失败抛核心的原话。 */
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
      job = bakeIn({ entry: copy(base), stock: copy(stock), menu: this.dat.read.menu, ctx: copy(files.ctx), ring: copy(files.ring), params });
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
    const made = await job;
    this.install(key, made);
    return key;
  }

  /** 已经做好的就直接装进显卡（显卡最多留 64 张，被挤掉的再装一次）。没做好返回 false。 */
  ready(key: string): boolean {
    const made = this.done.get(key);
    if (!made) return false;
    this.keep(key, made);
    this.install(key, made);
    return true;
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

  private install(key: string, made: Bake): void {
    if (!this.engine || this.engine.hasBake(key)) return;
    this.engine.addBake(key, made.meta, made.data);
    if (import.meta.env.DEV) recordBake(key, made);
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
