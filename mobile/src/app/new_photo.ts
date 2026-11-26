// 选进来的一张新照片：还没读过字节，调整全是默认值。从 boot.ts 原样搬出（单文件 ≤ 800 行）。

import { zeroGeometry } from '../lib/crop';
import { DEFAULT_FRAME, DEFAULT_STAMP } from '../lib/frame_spec';
import { zeroKnobs, type Photo } from './model';

export function newPhoto(id: string, stem: string, lastModified: number): Photo {
  return {
    id,
    stem,
    version: 0,
    dng: null,
    jpg: null,
    diptych: false,
    lastModified,
    captureMs: null,
    status: 'pending',
    gone: false,
    cached: null,
    checked: null,
    bitmap: null,
    neutral: null,
    knobs: zeroKnobs(),
    curve: null,
    rev: 0,
    jpgMeta: null,
    dngMeta: null,
    border: { ...DEFAULT_FRAME },
    stamp: { ...DEFAULT_STAMP },
    frameMeta: null,
    frame: null,
    geometry: zeroGeometry(),
    crop: null,
    choice: null,
    levels: new Map(),
    memory: new Map(),
    lut: null,
    looks: new Map(),
  };
}

function sameFile(had: File | null, incoming: File | null): boolean {
  return !had || !incoming || (had.size === incoming.size && had.lastModified === incoming.lastModified);
}

/**
 * 这次选进来的一张（同一次里按主文件名配好的 JPG / DNG）该落在哪个 id 上（2026-11-26）。
 * 和已有的那张同名、同类文件的大小和修改时间也一样（或那张还没有这一类文件）：是同一张，重新选不会多出一张。
 * 同名但文件不一样（两张卡上的同名照片）：是另一张，id 后面加「~2」「~3」，两张都留着。
 */
export function photoId(byId: ReadonlyMap<string, Photo>, stem: string, jpg: File | null, dng: File | null): string {
  const base = stem.toLowerCase();
  for (let n = 1; ; n += 1) {
    const id = n === 1 ? base : `${base}~${n}`;
    const had = byId.get(id);
    if (!had || (sameFile(had.jpg, jpg) && sameFile(had.dng, dng))) return id;
  }
}
