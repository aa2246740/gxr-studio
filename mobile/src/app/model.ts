// 这一次打开里的照片。文件句柄留着，字节只在正在看的那一张上。

import type { GapMinutes, Order } from '../lib/group';

export type PhotoStatus = 'pending' | 'ready' | 'bad' | 'gone';

export interface Knobs {
  exposure_ev: number;
  contrast: number;
  highlight: number;
  shadow: number;
  black_point: number;
  warm: number;
  tint: number;
  saturation: number;
  vibrance: number;
  vignette: number;
}

export function zeroKnobs(): Knobs {
  return {
    exposure_ev: 0,
    contrast: 0,
    highlight: 0,
    shadow: 0,
    black_point: 0,
    warm: 0,
    tint: 0,
    saturation: 0,
    vibrance: 0,
    vignette: 0,
  };
}

export interface Photo {
  id: string;
  stem: string;
  dng: File | null;
  jpg: File | null;
  lastModified: number;
  captureMs: number | null;
  status: PhotoStatus;
  bitmap: ImageBitmap | null;
  knobs: Knobs;
}

export function photoMs(photo: Photo): number {
  return photo.captureMs ?? photo.lastModified;
}

export interface AppApi {
  photos(): Photo[];
  photo(id: string): Photo | undefined;
  order(): Order;
  gap(): GapMinutes;
  ignored(): number;
  hasDat(): boolean;
  loading(): { done: number; total: number };
  setOrder(order: Order): void;
  setGap(gap: GapMinutes): void;
  open(id: string): void;
  remove(id: string): void;
  clear(): void;
  addPhotos(): void;
  showHelp(): void;
  back(): void;
  swipe(id: string, dir: -1 | 1): void;
  markBad(id: string): void;
  markGone(id: string): void;
  /** 下一张的 DNG 字节。离开编辑器就丢掉，最多留一份。 */
  takeWarm(id: string): ArrayBuffer | null;
  keepWarm(id: string, bytes: ArrayBuffer): void;
  gpu(): boolean;
  /** 编辑器里没有显卡或只有 JPG 时，按屏幕宽度在 Worker 里解一张清楚的。 */
  still(photo: Photo, width: number): Promise<ImageBitmap | null>;
  watch(kind: 'scroll' | 'drag', on: boolean): void;
}
