// 配方卡小图（photo.looks）的全局上限：按字节记，最近最少用的先放掉，正在打开的那张照片的卡先不放。
// 每张卡只留现在用的那一张：动旋钮装上新的，旧的立刻关掉。
// 换相机配方文件或校准数据时全部清掉（旧的烘焙表画出来的小图不能再用）。
//
// 上限怎么定的（HOW.md：页面约 2 GiB 就会被系统刷新，一次只处理一张全尺寸 DNG 加一张相邻的）：
// 一张小图长边 192，约 192×128×4 = 96 KiB。32 MiB 约 340 张，够最近 30 来张照片各自的内置 8 个加用户配方，
// 只占页面预算的 1.6%。其余留给两份 DNG（各约 18 MB）、显卡源纹理、烘焙表（JS 16 张 + 显卡 24 张约 150 MiB）、
// 四个 WASM 堆和缩略图网格。

import type { Photo } from './model';

export const LOOK_CACHE_BYTES = 32 << 20;

interface Entry {
  photo: Photo;
  key: string;
  /** 哪张卡的小图（同一张卡换了旋钮，旧的那张就关掉）。 */
  card: string | null;
  bitmap: ImageBitmap;
  bytes: number;
}

function sizeOf(bitmap: ImageBitmap): number {
  return Math.max(1, bitmap.width) * Math.max(1, bitmap.height) * 4;
}

export class LookCache {
  /** 插入顺序就是最近使用的顺序：最久没用的在最前面。 */
  private readonly entries = new Map<string, Entry>();
  private total = 0;
  private open: Photo | null = null;
  /** 每张照片每张卡现在用的那一张（键）。动旋钮装上新的，旧的立刻关掉，历史档位不会越攒越多（Sol r3）。 */
  private readonly current = new Map<string, string>();
  private clears = 0;

  constructor(private readonly limit = LOOK_CACHE_BYTES) {}

  private id(photo: Photo, key: string): string {
    return `${photo.id}\u0000${key}`;
  }

  /** 编辑器打开了哪张（null：没有打开的）。这张的卡不淘汰。 */
  setOpen(photo: Photo | null): void {
    this.open = photo;
    this.trim();
  }

  /** 这张照片这个版本的小图；拿到就算用过一次。 */
  get(photo: Photo, key: string): ImageBitmap | null {
    const look = photo.looks.get(key);
    if (!look || look.version !== photo.version) return null;
    const id = this.id(photo, key);
    const entry = this.entries.get(id);
    if (entry) {
      this.entries.delete(id);
      this.entries.set(id, entry);
    }
    return look.bitmap;
  }

  put(photo: Photo, key: string, version: number, bitmap: ImageBitmap, card: string | null = null): void {
    this.remove(photo, key);
    if (card !== null) {
      const slot = this.id(photo, card);
      const old = this.current.get(slot);
      if (old !== undefined && old !== key) this.remove(photo, old);
      this.current.set(slot, key);
    }
    photo.looks.set(key, { version, bitmap });
    const entry: Entry = { photo, key, card, bitmap, bytes: sizeOf(bitmap) };
    this.entries.set(this.id(photo, key), entry);
    this.total += entry.bytes;
    this.trim();
  }

  private remove(photo: Photo, key: string): void {
    const id = this.id(photo, key);
    const entry = this.entries.get(id);
    if (entry) {
      this.entries.delete(id);
      this.total -= entry.bytes;
      if (entry.card !== null && this.current.get(this.id(photo, entry.card)) === key) this.current.delete(this.id(photo, entry.card));
    }
    const look = photo.looks.get(key);
    if (look) {
      look.bitmap.close();
      photo.looks.delete(key);
    }
  }

  /** 放掉一张照片的全部小图（换版本、去掉、清空时）。 */
  forget(photo: Photo): void {
    for (const key of [...photo.looks.keys()]) this.remove(photo, key);
  }

  /** 换了相机配方文件或校准数据：全部放掉。 */
  clear(): void {
    for (const entry of [...this.entries.values()]) this.remove(entry.photo, entry.key);
    this.entries.clear();
    this.current.clear();
    this.total = 0;
    this.clears += 1;
  }

  /**
   * 先放别的照片最久没用的；还超就连打开那张也放（每张卡最多一张，正常远到不了上限）。上限任何时候都成立。
   */
  private trim(): void {
    if (this.total <= this.limit) return;
    for (const entry of [...this.entries.values()]) {
      if (this.total <= this.limit) return;
      if (this.open && entry.photo === this.open) continue;
      this.remove(entry.photo, entry.key);
    }
    for (const entry of [...this.entries.values()]) {
      if (this.total <= this.limit) return;
      this.remove(entry.photo, entry.key);
    }
  }

  stats(): { entries: number; bytes: number; limit: number; clears: number } {
    return { entries: this.entries.size, bytes: this.total, limit: this.limit, clears: this.clears };
  }
}

/** 整个页面一份。 */
export const lookCache = new LookCache();
