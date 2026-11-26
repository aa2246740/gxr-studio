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
