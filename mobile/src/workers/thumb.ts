// 缩略图和拍摄时间都在这个 Worker 里读。主线程只留着 File，不碰字节。

import { captureTimeFromJpeg, captureTimeFromTiff, checkDng, embeddedJpegRange } from '../lib/exif';

const PREFIX = 128 * 1024;
const TIFF_PREFIX = 256 * 1024;
const THUMB = 360;

export interface ThumbRequest {
  id: number;
  file: File;
  role: 'jpg' | 'dng';
  width: number;
}

export interface ThumbOk {
  id: number;
  ok: true;
  captureMs: number | null;
  bitmap: ImageBitmap | null;
  bad: boolean;
}

export interface ThumbFail {
  id: number;
  ok: false;
  gone: boolean;
}

function gone(err: unknown): boolean {
  const name = err instanceof DOMException ? err.name : err instanceof Error ? err.name : '';
  return name === 'NotFoundError' || name === 'NotReadableError';
}

async function scaled(blob: Blob, width: number): Promise<ImageBitmap | null> {
  try {
    return await createImageBitmap(blob, { resizeWidth: width, resizeQuality: 'medium' });
  } catch {
    /* 老内核没有 resizeWidth，下面用画布缩小。 */
  }
  try {
    const full = await createImageBitmap(blob);
    const target = Math.min(width, full.width || width);
    const height = Math.max(1, Math.round((full.height || 1) * (target / Math.max(1, full.width))));
    const canvas = new OffscreenCanvas(target, height);
    const ctx = canvas.getContext('2d');
    if (!ctx) {
      full.close();
      return null;
    }
    ctx.drawImage(full, 0, 0, target, height);
    full.close();
    return await createImageBitmap(canvas);
  } catch {
    return null;
  }
}

async function readRange(file: File, start: number, length: number): Promise<Uint8Array | null> {
  const end = Math.min(file.size, start + length);
  if (end <= start) return new Uint8Array();
  const buffer = await file.slice(start, end).arrayBuffer();
  return new Uint8Array(buffer);
}

async function fromJpeg(file: File, width: number): Promise<{ captureMs: number | null; bitmap: ImageBitmap | null }> {
  const head = await readRange(file, 0, PREFIX);
  const captureMs = head ? captureTimeFromJpeg(head)?.ms ?? null : null;
  const bitmap = await scaled(file, width);
  return { captureMs, bitmap };
}

async function fromDng(file: File, width: number): Promise<{ captureMs: number | null; bitmap: ImageBitmap | null; bad: boolean }> {
  const head = await readRange(file, 0, TIFF_PREFIX);
  // 只看第一个字节不够：「II」开头的坏文件也会被当成 RAW。先查完整的 TIFF 头和 IFD 结构。
  if (!head || checkDng(head, file.size) !== null) return { captureMs: null, bitmap: null, bad: true };
  let captureMs = captureTimeFromTiff(head)?.ms ?? null;
  const range = embeddedJpegRange(head);
  // 结构没问题但没有内嵌预览：合法的「只有 RAW」，不算坏。
  if (!range || range.offset + range.length > file.size) return { captureMs, bitmap: null, bad: false };
  const jpeg = await readRange(file, range.offset, range.length);
  if (!jpeg || jpeg.length < 4 || jpeg[0] !== 0xff || jpeg[1] !== 0xd8) return { captureMs, bitmap: null, bad: false };
  if (captureMs === null) captureMs = captureTimeFromJpeg(jpeg)?.ms ?? null;
  const bitmap = await scaled(new Blob([jpeg.slice()], { type: 'image/jpeg' }), width);
  return { captureMs, bitmap, bad: false };
}

self.onmessage = (event: MessageEvent<ThumbRequest>) => {
  const { id, file, role, width } = event.data;
  const thumbWidth = width > 0 ? width : THUMB;
  const run = role === 'jpg' ? fromJpeg(file, thumbWidth).then((row) => ({ ...row, bad: row.bitmap === null && role === 'jpg' })) : fromDng(file, thumbWidth);
  run
    .then((row) => {
      const reply: ThumbOk = { id, ok: true, captureMs: row.captureMs, bitmap: row.bitmap, bad: row.bad };
      const transfer: Transferable[] = row.bitmap ? [row.bitmap] : [];
      self.postMessage(reply, { transfer });
    })
    .catch((err: unknown) => {
      const reply: ThumbFail = { id, ok: false, gone: gone(err) };
      self.postMessage(reply);
    });
};
