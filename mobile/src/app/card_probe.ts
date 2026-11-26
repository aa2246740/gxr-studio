// 一个文件读不了，不一定是卡被拔了（2026-11-26）：电脑上常是那一个文件被挪走、改名或删了。
// 先试读别的几张照片各 1 个字节：读得到就只是这一张；一张都读不到才按拔卡处理。

import type { Photo } from './model';

/** 最多试读几张别的照片。 */
const PROBES = 3;

function readable(file: File): Promise<boolean> {
  return file.slice(0, 1).arrayBuffer().then(() => true, () => false);
}

/** 除了 `trigger` 以外，还有没有照片的原文件读得到。没有别的照片可试就是 false（照旧按拔卡处理）。 */
export async function othersReadable(photos: Iterable<Photo>, trigger: Photo): Promise<boolean> {
  const files: File[] = [];
  for (const photo of photos) {
    if (photo === trigger || photo.gone) continue;
    const file = photo.dng ?? photo.jpg;
    if (file && file !== trigger.dng && file !== trigger.jpg) files.push(file);
    if (files.length >= PROBES) break;
  }
  const results = await Promise.all(files.map((file) => readable(file)));
  return results.some(Boolean);
}
