// UI-M3-GROUP：手机怎么定一张照片的拍摄时间（M1 的 Worker 那一套：JPG 读前 128 KiB，DNG 读前 256 KiB，
// 读不到就用文件的修改时间）。只输出一行 JSON，按传进来的顺序；Python 那边和桌面的 photo_meta 比，不打印数值。

import { readFileSync, statSync, openSync, readSync, closeSync } from 'node:fs';
import { captureTimeFromJpeg, captureTimeFromTiff } from '../../mobile/src/lib/exif.ts';

function head(path, length) {
  const fd = openSync(path, 'r');
  try {
    const buf = Buffer.alloc(length);
    const n = readSync(fd, buf, 0, length, 0);
    return new Uint8Array(buf.buffer, buf.byteOffset, n);
  } finally {
    closeSync(fd);
  }
}

const items = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const out = items.map(({ path, kind }) => {
  const exif = kind === 'jpg' ? captureTimeFromJpeg(head(path, 128 * 1024)) : captureTimeFromTiff(head(path, 256 * 1024));
  // File.lastModified 是整毫秒。
  const lastModified = Math.floor(statSync(path).mtimeMs);
  return { exif: exif?.ms ?? null, ms: exif?.ms ?? lastModified, lastModified };
});
console.log(JSON.stringify(out));
