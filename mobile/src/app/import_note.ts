// 选照片或拖进来以后，没有用上的文件说一句（2026-11-26）。相机配方文件、校准数据 zip 和 .cube 不算（见 lib/group.ts）。

import type { Classified } from '../lib/group';

const RULE = '要 R 开头的 .JPG / .DNG';

/** 没有要说的就是 null。这一批里什么都没用上时（没有照片，也没有相机配方文件）换一种说法。 */
export function unusedNote(classified: Classified): string | null {
  const n = classified.unusable;
  if (!n) return null;
  const used = classified.photos.length > 0 || classified.datIndex !== null || classified.binIndex !== null;
  if (used) return `${n} 个文件不是 GXR 拍的照片（${RULE}），没有用上`;
  return n === 1 ? `没有能用的照片：这个文件不是 GXR 拍的照片（${RULE}）` : `没有能用的照片：这 ${n} 个文件都不是 GXR 拍的照片（${RULE}）`;
}
