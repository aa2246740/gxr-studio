// 这次会话里的相机配方文件和校准数据，以及按（条目、模型）缓存的烘焙表。
// 读文件、核对、烘焙都在 Worker 里；这里只排队、记结果、通知界面。

import type { Engine } from '../../../frontend/src/index';
import type { Bake, CalibPick, CalibRead, CalibRestored, DatRead, Resolved } from '../core/api';
import type { BakeArgs, CalibCheckArgs } from '../core/protocol';
import { modelIso, TEXT, UNIT_ISOS, type Unit, type UnitPack } from '../lib/calib';
import { DEFAULT_LABEL, DEFAULT_TEXT } from '../lib/default_calib';
import { deviceName } from '../lib/device';
import type { StoredCamFiles } from '../lib/idb';
import { selection, selectionKey, shelves, unitIndex, type Card, type Shelves } from '../lib/recipes';
import { ZIP_LIMITS, type ZipFail } from '../lib/zip';
import { CamFilesController, type Dat, type PhotoDatTarget } from './book_camfiles';
import { whenExportIdle } from './export_pause';

export type { Dat, PhotoDatTarget };

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
  /** `user`：用户导入的（存在 IndexedDB）；`default`：内置的默认校准（每次会话下载、核对一次，不存）。 */
  kind: 'user' | 'default';
}

export type UnitState =
  | { state: 'none' }
  | { state: 'missing'; message: string }
  | { state: 'checking' }
  | { state: 'ready' }
  | { state: 'invalid'; message: string };

function notSavedText(): string {
  return `校准数据没有存进这台 ${deviceName()}：这次能用，下次打开要重新导入。`;
}
const DELETE_FAILED = '删除没有成功，校准数据还在。请再试一次。';

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
  readDat(bytes: ArrayBuffer, library?: ArrayBuffer | null): Promise<DatRead>;
  /** 校准数据 zip：大小检查、读取、核对、存 IndexedDB 都在 Worker 里，主线程不碰整包。 */
  importCalib(file: File, savedAt: number): Promise<CalibRead>;
  restoreCalib(): Promise<CalibRestored>;
  /** `background`：配方卡小图的后台烘焙（见 core/pool.ts，点击发起的请求不排在它后面）。 */
  bake(args: BakeArgs, background?: boolean): Promise<Bake>;
  /** `color_resolve`：配方选择 → 条目（旋钮已经套上）。`library` 是同卡的 GXRFX.BIN。 */
  resolve(dat: ArrayBuffer, recipe: string, library?: ArrayBuffer | null, background?: boolean): Promise<Resolved>;
  /** `color_calib_check`：这个单元、这个 ISO 的 model_for 检查。 */
  calibCheck(args: CalibCheckArgs): Promise<CalibPick>;
  /** 记一个删除时间；比它早的导入不会再写进去。 */
  /** 写成功返回 true；已经有更新的记录、没写进去返回 false；事务失败抛错。 */
  deletePack(at: number): Promise<boolean>;
  /** IndexedDB 里存下的最高版本（导入和删除都算）。新会话的版本从它之上开始。 */
  watermark(): Promise<number>;
  /** 内置的默认校准包（下载和核对都在 Worker 里）。不给（测试里的假 io）就当没有默认校准。 */
  defaultCalib?(): Promise<CalibRead>;
  /** 相机配方文件（GXRCE.DAT / GXRFX.BIN）本地 IndexedDB 持久化；不给时仅内存保存。 */
  loadCamFiles?(): Promise<StoredCamFiles | null>;
  saveCamFiles?(files: StoredCamFiles): Promise<boolean>;
  deleteCamFiles?(at: number): Promise<boolean>;
  camFilesWatermark?(): Promise<number>;
}

export class Book {
  /** 用户导入的那份；没有时用内置的默认校准（`fallback`）。 */
  private user: Pack | null = null;
  private fallback: Pack | null = null;
  /** 默认校准用不了的原因（下载不下来、核对没通过）。只在没有任何校准包时显示。 */
  defaultError: string | null = null;
  private defaultJob: Promise<void> | null = null;
  /** 最近一次导入失败的那一行。导入成功、删除后清掉。 */
  packError: string | null = null;
  packBusy = false;
  private readonly camFiles: CamFilesController;
  /**
   * 每个异步操作提交前核对自己的版本（Sol r1）：选新的 DAT、清空都让 DAT 的版本加一；导入、替换、删除、恢复校准数据
   * 让校准的版本加一。晚到的结果只丢掉，不会把清空的状态复活，也不会盖掉后来的选择。
   */
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
  private readonly io: BookIo;

  constructor(io: BookIo) {
    this.io = io;
    this.camFiles = new CamFilesController(io, {
      onBump: () => this.bump(),
      onChanged: () => this.changed(),
    });
  }

  get dat(): Dat | null { return this.camFiles.dat; }
  set dat(value: Dat | null) { this.camFiles.dat = value; }
  get datError(): string | null { return this.camFiles.datError; }
  set datError(value: string | null) { this.camFiles.datError = value; }
  get datNote(): string | null { return this.camFiles.datNote; }
  set datNote(value: string | null) { this.camFiles.datNote = value; }
  get datBusy(): boolean { return this.camFiles.datBusy; }
  set datBusy(value: boolean) { this.camFiles.datBusy = value; }

  /** 现在用的校准包：用户导入的优先，没有就是默认校准；默认校准也用不了是 null（中性转换）。 */
  get pack(): Pack | null {
    return this.user ?? this.fallback;
  }

  /** 还在等默认校准（读回存下的包、下载、核对）：手上没有任何包，也还没有用不了的原因。没有默认校准时永远是 false。 */
  get loadingDefault(): boolean {
    return !this.pack && this.defaultLoader() !== null && this.defaultError === null;
  }

  /**
   * 下载默认校准的那个函数。没有 io（有的测试只拿 Book 画界面，`new Book()`），或者 io 里没有 `defaultCalib`，
   * 就当没有默认校准：返回 null，不抛。默认校准相关的地方（loadingDefault、calibSettled、useDefault）都只经过这里碰 io。
   */
  private defaultLoader(): (() => Promise<CalibRead>) | null {
    const io = this.io as BookIo | undefined;
    const load = io?.defaultCalib;
    return io && typeof load === 'function' ? () => load.call(io) : null;
  }

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
    // 默认校准落定了（用上了、用不了、或者用户的包到了）：这一轮的等待截止时间作废，下一轮重新算。
    if (!this.loadingDefault) this.settleBy = null;
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

  /** 已经应用到内存（`this.pack`）的最高版本，包括本次会话能用但未存下的校准包。 */
  private appliedAt = 0;

  /** 打开页面时把上次导入的校准数据与记住的相机配方文件读回来并重新核对（并行，不挡首屏）。 */
  async restore(): Promise<void> {
    await Promise.all([this.restoreCalibOnly(), this.camFiles.restore()]);
  }

  private async restoreCalibOnly(): Promise<void> {
    const seq = ++this.packSeq;
    try {
      const read = await this.io.restoreCalib();
      if (!read || seq !== this.packSeq || read.savedAt <= this.appliedAt) return;
      this.stamp = Math.max(this.stamp, read.savedAt);
      if (read.ok) {
        this.appliedAt = read.savedAt;
        this.commit(read, read.name, read.savedAt);
      } else this.packError = this.failText(read);
      this.changed();
    } finally {
      // 没有存下的包（或者读回来核对没过）：用默认校准。读 IndexedDB 出错也一样。
      this.ensureDefault();
    }
  }

  /** 没有用户的包时，确保默认校准在用或者正在下载。 */
  private ensureDefault(): void {
    if (!this.user) this.useDefault().catch(() => undefined);
  }

  /**
   * 没有用户的包时用内置的默认校准。一次会话只下载、核对一次，成功后留在内存里，删除或恢复默认时直接用；
   * 失败（没联网、文件不对）就是中性转换并写明原因。失败以后这次会话不会自己再下载（导入失败也不会）：只有用户删掉
   * 自己导入的包（「恢复默认校准」）时再试一次，这是用户明确要默认校准。读回存下的包时还没下载过，也会开始下载。不抛。
   */
  private useDefault(): Promise<void> {
    const load = this.defaultLoader();
    if (this.fallback || !load) return Promise.resolve();
    if (!this.defaultJob) {
      const job: Promise<void> = this.loadDefault(load).finally(() => {
        if (this.defaultJob === job) this.defaultJob = null;
      });
      this.defaultJob = job;
    }
    return this.defaultJob;
  }

  /** 这一轮等默认校准的截止时间（第一个等的人定下），落定时清掉。所有等的人共用它，不会一个接一个各等 `ms`。 */
  private settleBy: number | null = null;

  /**
   * 还在等默认校准（读回存下的包、下载、核对）时等它落定，最多等到这一轮的截止时间（第一次调用时的现在 + `ms`）。
   * 落定或到时都 resolve，不抛，也不留监听和定时器；这一轮已经到时了就马上 resolve。导出开始前用：别在默认校准落定前
   * 的这几百毫秒里悄悄按中性转换导出，也别让几次点击把等待叠起来。
   */
  calibSettled(ms: number): Promise<void> {
    if (!this.loadingDefault) return Promise.resolve();
    this.settleBy ??= Date.now() + ms;
    const left = this.settleBy - Date.now();
    if (left <= 0) return Promise.resolve();
    return new Promise((resolve) => {
      const finish = (): void => {
        clearTimeout(timer);
        off();
        resolve();
      };
      const timer = setTimeout(finish, left);
      const off = this.onChange(() => {
        if (!this.loadingDefault) finish();
      });
    });
  }

  private async loadDefault(fetchDefault: () => Promise<CalibRead>): Promise<void> {
    this.defaultError = null;
    this.changed();
    try {
      const read = await fetchDefault();
      if (read.ok) {
        this.fallback = { name: DEFAULT_LABEL, savedAt: 0, units: read.units, manifestSha256: read.manifestSha256, files: read.files, kind: 'default' };
        // 默认校准每次会话都是同一份：晚到也照样留下；只有正在用它时（没有用户的包）才算换了校准。
        if (!this.user) this.bump();
      } else this.defaultError = read.detail === 'fetch' ? DEFAULT_TEXT.fetch : DEFAULT_TEXT.bad;
    } catch {
      this.defaultError = DEFAULT_TEXT.fetch;
    } finally {
      this.changed();
    }
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
      this.appliedAt = Math.max(this.appliedAt, savedAt);
      this.commit(read, file.name, savedAt);
      if (!read.saved) {
        this.packError = notSavedText();
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
      // 不在这里碰默认校准：读回存下的包（restore）和删除自己的包（removePack）已经管着；默认校准失败以后，
      // 一次导入失败不该让它重新下载（一次会话只下载一次，见 useDefault）。
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
          this.user = null;
          this.bump();
          this.changed();
        }
        return;
      }
      if (!wrote) throw new Error('tombstone not written');
      this.appliedAt = Math.max(this.appliedAt, at);
      this.user = null;
      this.packError = null;
      this.bump();
    } catch {
      if (seq === this.packSeq) this.packError = DELETE_FAILED;
    } finally {
      if (seq === this.packSeq) {
        this.packBusy = false;
        this.changed();
      }
      // 删掉用户的包就回到默认校准（已经核对过的那份直接用，没有就现在下载）。
      this.ensureDefault();
    }
  }

  private failText(read: Extract<CalibRead, { ok: false }>): string {
    return read.zip ? ZIP_TEXT[read.zip] : read.message ?? TEXT.unreadable;
  }

  private commit(read: Extract<CalibRead, { ok: true }>, name: string, savedAt: number): void {
    this.user = { name, savedAt, units: read.units, manifestSha256: read.manifestSha256, files: read.files, kind: 'user' };
    this.bump();
  }

  /** 选文件时收下最新的 GXRCE.DAT / GXRFX.BIN：支持一起选、分两次选、重载后单补 BIN，以及单文件换掉双文件。 */
  ingestCamFiles(datFile: File | null, datCount: number, binFile: File | null): void {
    this.camFiles.ingest(datFile, datCount, binFile);
  }

  /** 「清空照片，重新选」：保留已记住的相机配方文件，只清本批提示与未配对的半份文件。 */
  clearSessionCamFiles(): void {
    this.camFiles.clearSession();
  }

  /** 这次选上的 GXRCE.DAT（已经挑好最新的那个）。`count` 是一共选了几个。`bin` 是同一次或先前选上的 GXRFX.BIN。 */
  setDat(file: File, count: number, bin?: File | null): Promise<void> {
    return this.camFiles.setDat(file, count, bin);
  }

  /** 忘掉已记住的相机配方文件：等 IndexedDB 墓碑写入确认后再清内存。 */
  forgetDat(): Promise<boolean> {
    return this.camFiles.forget();
  }

  clearDat(): void {
    this.camFiles.clear();
  }

  /** 对指定照片可用的相机配方文件；没有文件或目录不匹配时返回 null。 */
  usableDatFor(photo: PhotoDatTarget): Dat | null {
    return this.camFiles.usableFor(photo);
  }

  /** 对指定照片和单元可用的配方卡目录；没有可用相机配方文件（含目录不匹配）时返回 null。 */
  shelvesFor(photo: PhotoDatTarget, unit: Unit): Shelves | null {
    const dat = this.usableDatFor(photo);
    return dat ? shelves(dat.listing, unit) : null;
  }

  /** 对指定照片和单元的校准状态；照片命中目录不匹配时按无可用相机配方文件处理。 */
  unitStateFor(photo: PhotoDatTarget, unit: Unit): UnitState {
    return this.unitState(unit, this.usableDatFor(photo));
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
  unitState(unit: Unit, usableDat: Dat | null = this.dat): UnitState {
    if (!this.pack) return this.loadingDefault ? { state: 'checking' } : { state: 'none' };
    const row = this.pack.units.find((u) => u.unit === unit);
    if (!row) return { state: 'missing', message: TEXT.unitMissing };
    if (!usableDat) return { state: 'checking' };
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

  private resolveSel(sel: Record<string, unknown>, background = false): Promise<Resolved> {
    const dat = this.dat;
    if (!dat) return Promise.reject(new Error('找不到这个配方。请重新选择。'));
    const id = `${this.generation}|${selectionKey(sel)}`;
    let job = this.resolved.get(id);
    if (!job) {
      job = this.io.resolve(dat.read.bytes.slice(0), JSON.stringify(sel), dat.library ? dat.library.slice(0) : null, background);
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
  async bake(card: Card, unit: Unit, iso: number | null, levels?: Record<string, number>, background = false): Promise<string> {
    if (!this.dat || !card.controls.length) throw new Error('找不到这个配方。请重新选择。');
    const generation = this.generation;
    const model = modelIso(iso);
    const sel = selection(card, unit, levels);
    const got = await this.resolveSel(sel, background);
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
        return this.io.bake({ entry: copy(entry), stock: copy(stock), menu: '', ctx: copy(files.ctx), ring: copy(files.ring), params: '{"step":0.375}' }, background);
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
