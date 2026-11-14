// 这一次打开里的照片。文件句柄留着，字节只在正在看的那一张上。

import type { GapMinutes, Order } from '../lib/group';

/** 缩略图这一步的状态。文件读不读得了、显卡里有没有源，是另外两件事（gone、cached）。 */
export type PhotoStatus = 'pending' | 'ready' | 'bad';

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
  /** 每换一次 File（重新选了同名照片）加一。异步结果回来时先核对它，过期的丢掉。 */
  version: number;
  dng: File | null;
  jpg: File | null;
  lastModified: number;
  captureMs: number | null;
  status: PhotoStatus;
  /** 这个版本的文件读不了（卡拔了）。重新选同一张会换版本、清掉它。 */
  gone: boolean;
  /** 显卡里已有这张的完整源，是哪个版本。等于 version 时不用再读卡就能打开、调整。 */
  cached: number | null;
  /** WASM 描述通过的版本。没有显卡时，据此不用再读卡就能打开。 */
  checked: number | null;
  bitmap: ImageBitmap | null;
  knobs: Knobs;
}

/** stale：读的时候照片被重新选过，结果作废。 */
export type FullRead = { ok: true; bytes: ArrayBuffer } | { ok: false; gone: boolean; stale: boolean };

/** 点得开：缩略图好了，并且文件还读得了，或者这个版本的完整源已经在手里。 */
export function openable(photo: Photo): boolean {
  if (photo.status !== 'ready') return false;
  return !photo.gone || photo.cached === photo.version || photo.checked === photo.version;
}

/** 卡拔了、又没有缓存，真的没读完。 */
export function unread(photo: Photo): boolean {
  return photo.gone && !openable(photo);
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
  markBad(id: string, version: number): void;
  markGone(id: string, version: number): void;
  /** 整份 DNG，在 Worker 里读完转移过来。主线程不调 File.arrayBuffer。 */
  readFull(photo: Photo): Promise<FullRead>;
  /** 下一张的 DNG 字节。离开编辑器就丢掉，最多留一份。 */
  takeWarm(id: string, version: number): ArrayBuffer | null;
  keepWarm(id: string, version: number, bytes: ArrayBuffer): void;
  gpu(): boolean;
  /** 卡被拔出过、还有没读完的照片。导航副标题按它写，普通刷新不会盖掉。 */
  cardOut(): boolean;
  /** 编辑器里没有显卡或只有 JPG 时，按屏幕宽度在 Worker 里解一张清楚的。 */
  still(photo: Photo, width: number): Promise<ImageBitmap | null>;
  watch(kind: 'scroll' | 'drag', on: boolean): void;
}
