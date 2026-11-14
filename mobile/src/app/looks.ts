// 配方卡小图（photo.looks）的全局上限：按字节记，最近最少用的先放掉，正在打开的那张照片的卡不放。
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

  put(photo: Photo, key: string, version: number, bitmap: ImageBitmap): void {
    this.remove(photo, key);
    photo.looks.set(key, { version, bitmap });
    const entry: Entry = { photo, key, bitmap, bytes: sizeOf(bitmap) };
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
    this.total = 0;
    this.clears += 1;
  }

  private trim(): void {
    if (this.total <= this.limit) return;
    for (const entry of [...this.entries.values()]) {
      if (this.total <= this.limit) return;
      if (this.open && entry.photo === this.open) continue;
      this.remove(entry.photo, entry.key);
    }
  }

  stats(): { entries: number; bytes: number; limit: number; clears: number } {
    return { entries: this.entries.size, bytes: this.total, limit: this.limit, clears: this.clears };
  }
}

/** 整个页面一份。 */
export const lookCache = new LookCache();
