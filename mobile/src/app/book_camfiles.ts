// 相机配方文件（GXRCE.DAT / GXRFX.BIN）的内存状态、Worker 核对、IndexedDB 持久化与版本时序控制。
// 从 book.ts 拆出以保持单文件 <= 800 行；所有外部行为与 Book 原有 DAT/BIN 语义一致。

import type { DatRead } from '../core/api';
import { describeCamFilesMismatch, extractLibraryCatalogId, sha256Hex } from '../lib/camfiles';
import { mergeCopyright } from '../lib/exif';
import type { StoredCamFiles } from '../lib/idb';
import { parseRecipeTag } from '../lib/recipe_tags';
import { cameraRecipeErrorOf, type CameraRecipeCache, type Choice, type Listing } from '../lib/recipes';

export interface PhotoDatTarget {
  dng: File | null;
  diptych?: boolean;
  choice: Choice | null;
  recipeOff?: boolean;
  jpgMeta?: { copyright?: string | null } | null;
  dngMeta?: { copyright?: string | null } | null;
  dngCopyright?: string | null;
  cameraRecipe?: CameraRecipeCache | null;
}

/** 相机配方文件的上限：真的 DAT 不到 1 MB，超过这个一定不是，先拒绝再读。 */
const MAX_DAT_BYTES = 16 << 20;
const FORGET_DAT_FAILED = '忘掉没有成功，相机配方文件还在。请再试一次。';
/** 只选了 GXRFX.BIN、也没有可以配上的 GXRCE.DAT（只选 DAT 时核心说「缺少 GXRFX.BIN…」，两边对得上）。 */
export const BIN_ONLY_ERROR = '还差 GXRCE.DAT，请把 GXRCE.DAT 和 GXRFX.BIN 一起选';
/** 核心对缺 BIN 的双文件卡 DAT 说的话：这时留着这个 DAT，等用户补选 BIN。 */
const MISSING_BIN = '缺少 GXRFX.BIN';

export interface Dat {
  name: string;
  read: DatRead;
  /** 代数（旧版相机的旋钮只认 ±5）。 */
  generation: number | null;
  /** `color_listing` 的配方和旋钮。 */
  listing: Listing;
  /** 同卡的 GXRFX.BIN。单文件卡是 null。 */
  library: ArrayBuffer | null;
  /** GXRFX.BIN 的 SHA-256；单文件卡是 null。 */
  libraryHash: string | null;
  /** GXRFX.BIN[84:89] 的 10 位十六进制封存目录标识；单文件卡是 null。 */
  libraryCatalogId: string | null;
  /** 存进 IndexedDB 的时间戳；仅内存会话时是 null。 */
  savedAt: number | null;
}

export interface CamFilesIo {
  readFile(file: File): Promise<ArrayBuffer | null>;
  readDat(bytes: ArrayBuffer, library?: ArrayBuffer | null): Promise<DatRead>;
  loadCamFiles?(): Promise<StoredCamFiles | null>;
  saveCamFiles?(files: StoredCamFiles): Promise<boolean>;
  deleteCamFiles?(at: number): Promise<boolean>;
  camFilesWatermark?(): Promise<number>;
}

export interface CamFilesHost {
  onBump(): void;
  onChanged(): void;
}

export class CamFilesController {
  dat: Dat | null = null;
  datError: string | null = null;
  datNote: string | null = null;
  datBusy = false;

  private datSeq = 0;
  private camStamp = 0;
  private camFloor: Promise<void> | null = null;
  private datAppliedAt = 0;
  private pendingDat: { file: File; count: number } | null = null;
  private pendingBin: File | null = null;
  private readonly io: CamFilesIo;
  private readonly host: CamFilesHost;

  constructor(io: CamFilesIo, host: CamFilesHost) {
    this.io = io;
    this.host = host;
  }

  private async stampAbove(): Promise<number> {
    const readWm = this.io?.camFilesWatermark;
    if (typeof readWm === 'function') {
      this.camFloor ??= readWm.call(this.io).then(
        (mark) => { this.camStamp = Math.max(this.camStamp, mark); },
        () => undefined,
      );
      await this.camFloor;
    }
    this.camStamp = Math.max(Date.now(), this.camStamp + 1);
    return this.camStamp;
  }

  private async buildDat(name: string, read: DatRead, savedAt: number | null): Promise<Dat> {
    const summary = JSON.parse(read.summary) as { generation?: unknown };
    const listing = JSON.parse(read.listing) as Listing;
    if (read.entries.length !== 24 * 552) throw new Error('条目长度不合格');
    const generation = typeof summary.generation === 'number' ? summary.generation : null;
    const library = read.library ?? null;
    const libraryCatalogId = extractLibraryCatalogId(library);
    const libraryHash = library ? await sha256Hex(library) : null;
    return { name, read, generation, listing, library, libraryHash, libraryCatalogId, savedAt };
  }

  private commit(dat: Dat, savedAt: number | null): void {
    this.pendingDat = null;
    this.pendingBin = null;
    this.dat = dat;
    this.datError = null;
    if (savedAt !== null) this.datAppliedAt = Math.max(this.datAppliedAt, savedAt);
    this.host.onBump();
  }

  /** 打开页面时从 IndexedDB 读回上次记住的相机配方文件并重新跑核对，不挡首屏。 */
  async restore(): Promise<void> {
    const load = this.io?.loadCamFiles;
    if (typeof load !== 'function' || this.datSeq > 0) return;
    const seq = ++this.datSeq;
    this.datBusy = true;
    this.host.onChanged();
    let stored: StoredCamFiles | null = null;
    try {
      stored = await load.call(this.io);
      if (!stored || seq !== this.datSeq || stored.savedAt <= this.datAppliedAt) return;
      this.camStamp = Math.max(this.camStamp, stored.savedAt);
      const read = await this.io.readDat(stored.dat.slice(0), stored.library ? stored.library.slice(0) : null);
      if (seq !== this.datSeq || stored.savedAt <= this.datAppliedAt) return;
      const built = await this.buildDat(stored.datName, read, stored.savedAt);
      if (seq !== this.datSeq || stored.savedAt <= this.datAppliedAt) return;
      this.commit(built, stored.savedAt);
    } catch {
      // 记住的字节损坏或不兼容：核对拒收并写入墓碑清掉损坏记录，下次启动不再重复解析。
      if (stored && seq === this.datSeq && stored.savedAt > this.datAppliedAt) {
        const del = this.io?.deleteCamFiles;
        if (typeof del === 'function') {
          try {
            const at = await this.stampAbove();
            if (seq === this.datSeq && stored.savedAt > this.datAppliedAt) {
              const wrote = await del.call(this.io, at);
              if (wrote && seq === this.datSeq) this.datAppliedAt = Math.max(this.datAppliedAt, at);
            }
          } catch {
            // IndexedDB 写墓碑失败不挡首屏，保持内存为空。
          }
        }
      }
    } finally {
      if (seq === this.datSeq) {
        this.datBusy = false;
        this.host.onChanged();
      }
    }
  }

  /** 对指定照片返回可用的相机配方文件；没有文件或拍摄目录不匹配时返回 null。 */
  usableFor(photo: PhotoDatTarget): Dat | null {
    const dat = this.dat;
    if (!dat) return null;
    if (!photo.dng || photo.diptych || photo.choice !== null || photo.recipeOff) return dat;
    if (cameraRecipeErrorOf(photo, dat)?.code === 'camera_recipe_catalog_missing') return null;
    const copyright = mergeCopyright(photo.jpgMeta?.copyright, photo.dngMeta?.copyright ?? photo.dngCopyright);
    if (copyright && copyright.startsWith('GXR1 ') && describeCamFilesMismatch(parseRecipeTag(copyright), dat)) {
      return null;
    }
    return dat;
  }

  /** 选文件时收下最新的 GXRCE.DAT / GXRFX.BIN：支持一起选、分两次选、重载后单补 BIN，以及单文件换掉双文件。 */
  ingest(datFile: File | null, datCount: number, binFile: File | null): void {
    if (!datFile && !binFile) return;
    if (datFile) {
      this.pendingDat = { file: datFile, count: datCount };
      if (binFile) this.pendingBin = binFile;
      void this.setDat(datFile, datCount, this.pendingBin).catch(() => undefined);
      return;
    }
    this.pendingBin = binFile;
    if (this.pendingDat) {
      void this.setDat(this.pendingDat.file, this.pendingDat.count, binFile).catch(() => undefined);
    } else if (this.dat && binFile) {
      void this.setDatBytes(this.dat.name, this.dat.read.bytes.slice(0), 1, binFile).catch(() => undefined);
    } else if (binFile) {
      // 只有 BIN：说还差 DAT，换掉之前那条错误。BIN 照旧留着，之后单补 DAT 也能配上。
      this.datError = BIN_ONLY_ERROR;
      this.host.onChanged();
    }
  }

  /** 这个 DAT 本身不能用（太大、读不了、核心拒收且不是缺 BIN）：不再留着等补选 BIN。 */
  private dropPendingDat(file: File): void {
    if (this.pendingDat?.file === file) this.pendingDat = null;
  }

  /** 「清空照片，重新选」：保留已记住的相机配方文件，只清本批提示与未配对的半份文件。 */
  clearSession(): void {
    this.datNote = null;
    if (!this.dat) {
      this.pendingDat = null;
      this.pendingBin = null;
      this.datError = null;
    }
    this.host.onChanged();
  }

  async setDat(file: File, count: number, bin?: File | null): Promise<void> {
    const seq = ++this.datSeq;
    this.datNote = count > 1 ? `选了 ${count} 个 GXRCE.DAT，用的是修改时间最新的那个。` : null;
    if (file.size > MAX_DAT_BYTES) {
      this.dropPendingDat(file);
      this.datBusy = false;
      this.datError = '这个文件太大，不是相机配方文件。请重新选一次 GXRCE.DAT。';
      this.host.onChanged();
      return;
    }
    if (bin && bin.size > MAX_DAT_BYTES) {
      this.datBusy = false;
      this.datError = '这个文件太大，不是相机配方文件。请重新选一次 GXRFX.BIN。';
      this.host.onChanged();
      return;
    }
    this.datBusy = true;
    this.datError = null;
    this.host.onChanged();
    try {
      const bytes = await this.io.readFile(file);
      if (seq !== this.datSeq) return;
      if (!bytes) {
        this.dropPendingDat(file);
        this.datError = '读不了这个相机配方文件。请重新选一次 GXRCE.DAT。';
        return;
      }
      await this.finishSetDat(seq, file.name, bytes, bin ?? null);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      // 这次带了 BIN 而失败：坏的是 BIN（不匹配或损坏），DAT 留着等下一份 BIN；只有单独的 DAT 被拒才丢掉。
      if (seq === this.datSeq && !bin && !message.startsWith(MISSING_BIN)) this.dropPendingDat(file);
      if (seq === this.datSeq) this.datError = `无法读取相机配方文件：${message}`;
    } finally {
      if (seq === this.datSeq) {
        this.datBusy = false;
        this.host.onChanged();
      }
    }
  }

  private async setDatBytes(name: string, bytes: ArrayBuffer, count: number, bin: File): Promise<void> {
    const seq = ++this.datSeq;
    this.datNote = count > 1 ? `选了 ${count} 个 GXRCE.DAT，用的是修改时间最新的那个。` : null;
    if (bin.size > MAX_DAT_BYTES) {
      this.datBusy = false;
      this.datError = '这个文件太大，不是相机配方文件。请重新选一次 GXRFX.BIN。';
      this.host.onChanged();
      return;
    }
    this.datBusy = true;
    this.datError = null;
    this.host.onChanged();
    try {
      await this.finishSetDat(seq, name, bytes, bin);
    } catch (err) {
      if (seq === this.datSeq) this.datError = `无法读取相机配方文件：${err instanceof Error ? err.message : String(err)}`;
    } finally {
      if (seq === this.datSeq) {
        this.datBusy = false;
        this.host.onChanged();
      }
    }
  }

  private async finishSetDat(seq: number, name: string, bytes: ArrayBuffer, bin: File | null): Promise<void> {
    let library: ArrayBuffer | null = null;
    if (bin) {
      library = await this.io.readFile(bin);
      if (seq !== this.datSeq) return;
      if (!library) {
        this.datError = '读不了这个相机配方文件。请重新选一次 GXRFX.BIN。';
        return;
      }
    }
    const read = await this.io.readDat(bytes, library);
    if (seq !== this.datSeq) return;
    const built = await this.buildDat(name, read, null);
    if (seq !== this.datSeq) return;
    const save = this.io?.saveCamFiles;
    if (typeof save === 'function') {
      const savedAt = await this.stampAbove();
      if (seq !== this.datSeq) return;
      let saved = false;
      try {
        saved = await save.call(this.io, {
          dat: read.bytes.slice(0),
          datName: name,
          library: built.library ? built.library.slice(0) : null,
          generation: built.generation,
          libraryHash: built.libraryHash,
          catalogId: built.libraryCatalogId,
          savedAt,
        });
      } catch {
        saved = false;
      }
      if (seq !== this.datSeq) {
        if (saved && savedAt > this.datAppliedAt) {
          this.commit({ ...built, savedAt }, savedAt);
          this.host.onChanged();
        }
        return;
      }
      this.commit({ ...built, savedAt: saved ? savedAt : null }, saved ? savedAt : null);
      return;
    }
    this.commit(built, null);
  }

  /** 忘掉已记住的相机配方文件：等 IndexedDB 墓碑写入确认后再清内存。 */
  async forget(): Promise<boolean> {
    const seq = ++this.datSeq;
    this.pendingDat = null;
    this.pendingBin = null;
    this.datBusy = true;
    this.datError = null;
    this.host.onChanged();
    try {
      const at = await this.stampAbove();
      if (seq !== this.datSeq) return false;
      const del = this.io?.deleteCamFiles;
      const wrote = typeof del === 'function' ? await del.call(this.io, at) : true;
      if (seq !== this.datSeq) {
        if (wrote && at > this.datAppliedAt) {
          this.datAppliedAt = at;
          this.dat = null;
          this.datNote = null;
          this.host.onBump();
          this.host.onChanged();
        }
        return false;
      }
      if (!wrote) throw new Error('tombstone not written');
      this.datAppliedAt = Math.max(this.datAppliedAt, at);
      this.dat = null;
      this.datNote = null;
      this.datError = null;
      this.host.onBump();
      return true;
    } catch {
      if (seq === this.datSeq) this.datError = FORGET_DAT_FAILED;
      return false;
    } finally {
      if (seq === this.datSeq) {
        this.datBusy = false;
        this.host.onChanged();
      }
    }
  }

  clear(): void {
    this.datSeq += 1;
    this.pendingDat = null;
    this.pendingBin = null;
    this.datBusy = false;
    this.dat = null;
    this.datError = null;
    this.datNote = null;
    this.host.onBump();
    this.host.onChanged();
  }
}
