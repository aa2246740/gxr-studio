// 缩略图和拍摄时间都在这个 Worker 里读。主线程只留着 File，不碰字节。

import { captureTimeFromJpeg, captureTimeFromTiff, checkDng, embeddedJpegRange, photoMetaFromJpeg, photoMetaFromTiff, type PhotoMeta } from '../lib/exif';
import { isDiptychBlob } from '../lib/halfframe';
import { dngFrame, jpegFrame, type Frame } from '../lib/photo_size';

const PREFIX = 128 * 1024;
const TIFF_PREFIX = 256 * 1024;
const SIZE_PREFIX = 1024 * 1024;
const THUMB = 360;

export interface ThumbRequest {
  id: number;
  file: File;
  /** jpg / dng：缩略图和拍摄时间。full：整份文件的字节，转移回主线程（主线程不调 File.arrayBuffer）。 */
  role: 'jpg' | 'dng' | 'full';
  width: number;
  /** 大屏网格：还要盖住这么高（设备像素，object-fit: cover）。0 或没有：只按宽解（今天的小版式）。 */
  height?: number;
}

export interface ThumbOk {
  kind: 'thumb';
  name: string;
  id: number;
  ok: true;
  captureMs: number | null;
  bitmap: ImageBitmap | null;
  bad: boolean;
  /** 镜头名和 ISO，从读缩略图时已经读进来的那一段里取。 */
  meta: PhotoMeta;
  /** 文件头的转正尺寸与方向，供裁剪面板与导出请求使用。 */
  frame: Frame | null;
  /** 是否为半格双联相机 JPG。 */
  diptych?: boolean;
}

export interface FullOk {
  kind: 'full';
  name: string;
  id: number;
  ok: true;
  bytes: ArrayBuffer;
}

export interface ThumbFail {
  kind: 'thumb' | 'full';
  name: string;
  id: number;
  ok: false;
  gone: boolean;
}

export type WorkerReply = ThumbOk | FullOk | ThumbFail;

function gone(err: unknown): boolean {
  const name = err instanceof DOMException ? err.name : err instanceof Error ? err.name : '';
  return name === 'NotFoundError' || name === 'NotReadableError';
}

async function scaled(blob: Blob, width: number): Promise<ImageBitmap | null> {
  if (!Number.isFinite(width)) {
    try {
      return await createImageBitmap(blob, { imageOrientation: 'from-image' });
    } catch {
      return await createImageBitmap(blob).catch(() => null);
    }
  }
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

const NO_META: PhotoMeta = { lens: null, iso: null };

/** 按文件头的转正尺寸把解码宽放到盖得住 `height`（比格子宽的照片要更宽），最多 2 倍 `width`。不知道比例就只按宽。 */
function coverWidth(width: number, height: number, frame: Frame | null): number {
  if (!(height > 0) || !frame || frame.width <= 0 || frame.height <= 0 || !Number.isFinite(width)) return width;
  return Math.min(2 * width, Math.max(width, Math.ceil((height * frame.width) / frame.height)));
}

async function fromJpeg(file: File, width: number, height: number): Promise<{ captureMs: number | null; bitmap: ImageBitmap | null; meta: PhotoMeta; frame: Frame | null; diptych: boolean }> {
  const head = await readRange(file, 0, PREFIX);
  const captureMs = head ? captureTimeFromJpeg(head)?.ms ?? null : null;
  const meta = head ? photoMetaFromJpeg(head) : NO_META;
  let frame = head ? jpegFrame(head) : null;
  if (!frame && file.size > PREFIX) {
    const wide = await readRange(file, 0, SIZE_PREFIX);
    if (wide) frame = jpegFrame(wide);
  }
  const [bitmap, diptych] = await Promise.all([scaled(file, coverWidth(width, height, frame)), isDiptychBlob(file, frame)]);
  return { captureMs, bitmap, meta, frame, diptych };
}

async function fromDng(file: File, width: number, height: number): Promise<{ captureMs: number | null; bitmap: ImageBitmap | null; bad: boolean; meta: PhotoMeta; frame: Frame | null; diptych: boolean }> {
  const head = await readRange(file, 0, TIFF_PREFIX);
  // 只看第一个字节不够：「II」开头的坏文件也会被当成 RAW。先查完整的 TIFF 头和 IFD 结构。
  if (!head || checkDng(head, file.size) !== null) return { captureMs: null, bitmap: null, bad: true, meta: NO_META, frame: null, diptych: false };
  let captureMs = captureTimeFromTiff(head)?.ms ?? null;
  const meta = photoMetaFromTiff(head);
  const frame = dngFrame(head);
  const range = embeddedJpegRange(head);
  // 结构没问题但没有内嵌预览：合法的「只有 RAW」，不算坏。
  if (!range || range.offset + range.length > file.size) return { captureMs, bitmap: null, bad: false, meta, frame, diptych: false };
  const jpeg = await readRange(file, range.offset, range.length);
  if (!jpeg || jpeg.length < 4 || jpeg[0] !== 0xff || jpeg[1] !== 0xd8) return { captureMs, bitmap: null, bad: false, meta, frame, diptych: false };
  if (captureMs === null) captureMs = captureTimeFromJpeg(jpeg)?.ms ?? null;
  const bitmap = await scaled(new Blob([jpeg.slice()], { type: 'image/jpeg' }), coverWidth(width, height, frame));
  return { captureMs, bitmap, bad: false, meta, frame, diptych: false };
}

self.onmessage = (event: MessageEvent<ThumbRequest>) => {
  const { id, file, role, width, height = 0 } = event.data;
  const name = file.name;
  if (role === 'full') {
    file
      .arrayBuffer()
      .then((bytes) => {
        const reply: FullOk = { kind: 'full', name, id, ok: true, bytes };
        self.postMessage(reply, { transfer: [bytes] });
      })
      .catch((err: unknown) => {
        const reply: ThumbFail = { kind: 'full', name, id, ok: false, gone: gone(err) };
        self.postMessage(reply);
      });
    return;
  }
  const thumbWidth = width > 0 ? width : THUMB;
  const run = role === 'jpg' ? fromJpeg(file, thumbWidth, height).then((row) => ({ ...row, bad: row.bitmap === null && role === 'jpg' })) : fromDng(file, thumbWidth, height);
  run
    .then((row) => {
      const reply: ThumbOk = {
        kind: 'thumb',
        name,
        id,
        ok: true,
        captureMs: row.captureMs,
        bitmap: row.bitmap,
        bad: row.bad,
        meta: row.meta,
        frame: row.frame,
        diptych: row.diptych,
      };
      const transfer: Transferable[] = row.bitmap ? [row.bitmap] : [];
      self.postMessage(reply, { transfer });
    })
    .catch((err: unknown) => {
      const reply: ThumbFail = { kind: 'thumb', name, id, ok: false, gone: gone(err) };
      self.postMessage(reply);
    });
};
