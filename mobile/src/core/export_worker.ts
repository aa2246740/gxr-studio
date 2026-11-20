// 导出 Worker：一份 WASM，一次一张。一批开始时建、结束（或闲 30 秒）就关掉，WASM 的线性内存跟着还回去
// （线性内存只增不减，留在 M1/M2 的 Worker 里会一直占着）。文件在这里读，主线程不调 File.arrayBuffer。
//
// 每张：export_plan → 读 DNG / JPG → 选了配方时 color_resolve → 需要时解码相机 JPG（或核心要的内嵌预览）
// → export_prepare → export_ink（边框、标志、时间戳与 HDR 日期戳掩码）→ export_complete。
// 开了 HDR 时 prepare 还给一份数据层（hdr_layer），留在这个 Worker 里，连同着墨掩码一起交给 complete。

import init, {
  color_resolve,
  export_complete,
  export_ink,
  export_meta_photo,
  export_meta_preview,
  export_output_sizes,
  export_plan,
  export_prepare,
  type InitOutput,
} from '../wasm-pkg/gxr_wasm.js';
import wasmUrl from '../wasm-pkg/gxr_wasm_bg.wasm?url';
import { mapInkError, shouldDecodeCameraJpg, shouldRetryWithCameraJpg } from '../lib/export_request';
import { momentFromIso } from '../lib/frame_spec';
import { sliceDiptychHalfRgb, sliceFirstHalfStripRgb, stripExifCopyrightFromJpeg } from '../lib/halfframe';
import { shotSize, jpegSize, dngSize } from '../lib/photo_size';
import { isDisplaySpace, LUT_ERRORS } from '../lib/lut';
import type { ExportIn, ExportJob, ExportMemory, ExportOut, ExportSetup, FailKind, SizesJob } from './export_protocol';

let ready: Promise<InitOutput> | null = null;
let wasm: InitOutput | null = null;
let setup: ExportSetup = { dat: null, library: null, calibIndex: '', calibBlob: null, luts: {}, fontIndex: '', fontPack: null, logoIndex: '', logoBlob: null };

function load(): Promise<InitOutput> {
  ready ??= init({ module_or_path: wasmUrl }).then((out) => {
    wasm = out;
    return out;
  });
  return ready;
}

function post(msg: ExportOut, transfer: Transferable[] = []): void {
  self.postMessage(msg, { transfer });
}

class Fail extends Error {
  constructor(
    readonly kind: FailKind,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

/** 核心的拒绝是 `{"error": {"kind", "message", ...}}` 文本。认不出的当作内部错误。 */
function coreFail(err: unknown): Fail {
  if (err instanceof Fail) return err;
  if (err instanceof WebAssembly.RuntimeError || err instanceof RangeError) return new Fail('trap', 'trap', err.message);
  const text = typeof err === 'string' ? err : err instanceof Error ? err.message : String(err);
  try {
    const parsed = JSON.parse(text) as { error?: { kind?: unknown; message?: unknown } };
    const kind = parsed.error?.kind;
    const message = parsed.error?.message;
    if (typeof kind === 'string') return new Fail('core', kind, typeof message === 'string' ? message : '');
  } catch {
    /* 不是 JSON。 */
  }
  return new Fail('core', 'unknown', '');
}

/** 读整个文件。读不了（卡拔了、文件变了）是 `read`。 */
async function readAll(file: File): Promise<Uint8Array> {
  try {
    return new Uint8Array(await file.arrayBuffer());
  } catch (err) {
    throw new Fail('read', 'read', err instanceof Error ? err.name : 'read');
  }
}

/**
 * 照桌面 `finish/compose.load_jpeg`（`ImageOps.exif_transpose(im).convert("RGB")`）解码：按 EXIF 方向转正，不做色彩转换，
 * 去掉 alpha 成紧凑的 RGB。位图马上关掉。
 */
export async function decodeJpeg(bytes: Uint8Array): Promise<{ data: Uint8Array; width: number; height: number }> {
  const blob = new Blob([bytes as Uint8Array<ArrayBuffer>], { type: 'image/jpeg' });
  const bitmap = await createImageBitmap(blob, { imageOrientation: 'from-image', colorSpaceConversion: 'none', premultiplyAlpha: 'none' });
  const { width, height } = bitmap;
  let rgba: Uint8ClampedArray;
  try {
    const canvas = new OffscreenCanvas(width, height);
    const ctx = canvas.getContext('2d', { colorSpace: 'srgb', willReadFrequently: true });
    if (!ctx) throw new Error('2d context');
    ctx.drawImage(bitmap, 0, 0);
    rgba = ctx.getImageData(0, 0, width, height, { colorSpace: 'srgb' }).data;
  } finally {
    bitmap.close();
  }
  const data = new Uint8Array(width * height * 3);
  for (let i = 0, j = 0; i < rgba.length; i += 4, j += 3) {
    data[j] = rgba[i];
    data[j + 1] = rgba[i + 1];
    data[j + 2] = rgba[i + 2];
  }
  return { data, width, height };
}

function fromHex(text: string): Uint8Array {
  const out = new Uint8Array(text.length >> 1);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(text.slice(i * 2, i * 2 + 2), 16);
  return out;
}

const EMPTY = new Uint8Array(0);
const NO_LAYER = new Float32Array(0);

function memory(id: number, stage: ExportMemory['stage'], js: number): void {
  post({ op: 'mem', id, mem: { stage, wasm: wasm?.memory.buffer.byteLength ?? 0, js } });
}

/** DNG 读不出全尺寸数据时，桌面改用内嵌预览（serve._raw_rgb）。只有这几种错误才去解预览再试一次。 */
const NEEDS_PREVIEW = new Set(['raw_unreadable', 'no_jpg_source', 'raw_decode']);

function isLogLutSpace(spaceText: string): boolean {
  if (!spaceText) return false;
  try {
    const parsed = JSON.parse(spaceText) as { input_space?: unknown };
    return typeof parsed.input_space === 'string' && parsed.input_space !== '' && !isDisplaySpace(parsed.input_space);
  } catch {
    return false;
  }
}

async function headSlice(file: File | null): Promise<Uint8Array | null> {
  if (!file) return null;
  try {
    return new Uint8Array(await file.slice(0, 1 << 20).arrayBuffer());
  } catch {
    return null;
  }
}

async function runJob(job: ExportJob): Promise<void> {
  const start = performance.now();
  await load();
  const { req } = job;
  // 1. 读文件。DNG 和相机 JPG 的字节都要：DNG 给 color_image，JPG 给 EXIF、拍摄核对和原样导出。
  let jpgBytes: Uint8Array | null = job.jpg ? await readAll(job.jpg) : null;
  let dngBytes: Uint8Array | null = job.dng ? await readAll(job.dng) : null;
  const size = shotSize(jpgBytes, dngBytes);
  const shot: Record<string, unknown> = { ...req.shot, ...(size ? { width: size.width, height: size.height } : {}) };
  let shotText = JSON.stringify(shot);
  const sideText = JSON.stringify(req.side);
  // 2. 计划：没有可见编辑、有相机 JPG 时原样导出相机 JPG。
  const plan = JSON.parse(export_plan(shotText, sideText, '{}', req.base)) as { original?: boolean };
  // 3. 配方条目。只有 RAW、没选配方时用原厂 04；没有相机配方文件就不给，由核心决定（中性转换）。
  let entry: Uint8Array = EMPTY;
  const dat = setup.dat ? new Uint8Array(setup.dat) : EMPTY;
  if (req.resolve && dat.length) {
    try {
      const library = setup.library ? new Uint8Array(setup.library.slice(0)) : undefined;
      entry = fromHex((JSON.parse(color_resolve(dat, new TextEncoder().encode(req.resolve), library)) as { entry: string }).entry);
    } catch (err) {
      if ((req.side as { recipe?: unknown }).recipe) throw coreFail(err);
    }
  }
  // 4. LUT 快照（如果在编辑状态选了 LUT，找不到快照就按桌面语义直接拒绝，绝不静默跳过）。
  const lutId = typeof (req.side as { lut?: unknown }).lut === 'string' ? (req.side as { lut: string }).lut : null;
  let cubeBytes: Uint8Array = EMPTY;
  let spaceText = '';
  if (lutId) {
    const lutEntry = setup.luts?.[lutId];
    if (!lutEntry) throw new Fail('core', 'cube_not_found', LUT_ERRORS.cube_not_found);
    cubeBytes = new Uint8Array(lutEntry.cube);
    spaceText = lutEntry.space || '{}';
  }
  const calibBlob = setup.calibBlob ? new Uint8Array(setup.calibBlob) : EMPTY;
  // 5. 相机 JPG 在原样/JPG 路径，或缺少校准/相机数据可能走 camera-jpg-fallback 时预先解码；
  //    部分校准包缺机身或 DNG 损坏触发回退时，下方 catch 也会按需解码再重试一次。
  const isLogLut = Boolean(lutId && isLogLutSpace(spaceText));
  const isLocalRecipe = (req.side as { recipe?: { kind?: unknown } }).recipe?.kind === 'local';
  let jpg: { data: Uint8Array; width: number; height: number } | null = null;
  if (job.halfframeSource && jpgBytes) {
    const decoded = await decodeJpeg(jpgBytes);
    jpg =
      job.halfframeSource === 'first-strip'
        ? sliceFirstHalfStripRgb(decoded.data, decoded.width, decoded.height)
        : sliceDiptychHalfRgb(decoded.data, decoded.width, decoded.height, job.halfframeSource === 'diptych-first' ? 'first' : 'second');
    shot.width = jpg.width;
    shot.height = jpg.height;
    shotText = JSON.stringify(shot);
  } else if (
    jpgBytes &&
    shouldDecodeCameraJpg({
      hasJpg: true,
      needsJpg: req.needsJpg,
      planOriginal: Boolean(plan.original),
      isLogLut,
      isLocalRecipe,
      hasCalib: calibBlob.length > 0,
      hasDat: dat.length > 0,
    })
  ) {
    jpg = await decodeJpeg(jpgBytes);
  }
  const fontIndex = setup.fontIndex ?? '';
  const fontPack = setup.fontPack ? new Uint8Array(setup.fontPack) : EMPTY;
  const logoIndex = setup.logoIndex ?? '';
  const logoBlob = setup.logoBlob ? new Uint8Array(setup.logoBlob) : EMPTY;
  // 空字节就是没有 GXRFX.BIN。和 color_resolve 各拷一份，避免 WASM 把同一段视图吃掉。
  const libraryBytes = setup.library?.byteLength ? new Uint8Array(setup.library.slice(0)) : EMPTY;
  const prepare = (preview: { data: Uint8Array; width: number; height: number } | null) =>
    export_prepare(
      shotText, sideText, JSON.stringify(req.defaults), JSON.stringify(req.layout), req.base,
      req.quality, req.longEdge, req.hdr, fontIndex, fontPack,
      dat, libraryBytes, entry, setup.calibIndex, calibBlob, req.binding,
      dngBytes ?? EMPTY, jpgBytes ?? EMPTY, cubeBytes, spaceText,
      jpg?.data ?? EMPTY, jpg?.width ?? 0, jpg?.height ?? 0,
      preview?.data ?? EMPTY, preview?.width ?? 0, preview?.height ?? 0,
      req.mtimeMs, req.tzOffsetMin,
    );
  let made;
  try {
    made = prepare(null);
  } catch (err) {
    const fail = coreFail(err);
    if (
      jpgBytes &&
      shouldRetryWithCameraJpg({
        hasJpg: true,
        alreadyDecodedJpg: jpg !== null,
        failKind: fail.kind,
        failCode: fail.code,
        failMessage: fail.message,
      })
    ) {
      jpg = await decodeJpeg(jpgBytes);
      made = prepare(null);
    } else {
      const at = fail.kind === 'core' && NEEDS_PREVIEW.has(fail.code) && dngBytes ? (JSON.parse(export_meta_preview(dngBytes)) as { offset: number; length: number } | null) : null;
      if (!at || !dngBytes) throw fail;
      let preview: { data: Uint8Array; width: number; height: number } | null = null;
      try {
        preview = await decodeJpeg(dngBytes.subarray(at.offset, at.offset + at.length));
      } catch {
        throw fail;
      }
      made = prepare(preview);
    }
  }
  // WASM 有了自己的那份：解码的像素和用不到的源文件马上放掉，只留 complete 写 EXIF 要的那份源文件。
  jpg = null;
  let source: Uint8Array = jpgBytes ?? dngBytes ?? EMPTY;
  jpgBytes = null;
  dngBytes = null;
  let canvas: Uint8Array;
  let madeJson: string;
  let json: { prepared: Record<string, unknown> };
  let width: number;
  let height: number;
  // HDR 的数据层（float32，(h, w, 2)）：只在这个 Worker 里，原样交给 export_complete，不回主线程。
  let layer: Float32Array = NO_LAYER;
  try {
    madeJson = made.json();
    json = JSON.parse(madeJson) as { prepared: Record<string, unknown> };
    width = made.width;
    height = made.height;
    canvas = made.canvas();
    if (req.hdr) layer = made.hdr_layer();
  } finally {
    made.free();
  }
  memory(job.id, 'prepare', canvas.byteLength + layer.byteLength + source.byteLength);
  if (job.devTrap && import.meta.env.DEV) throw new WebAssembly.RuntimeError('unreachable (dev trap)');
  // 6. Rust 着墨：边框底衬与元素、日期戳和 HDR 照片落点掩码。
  let stampMask: Uint8Array = EMPTY;
  let maskWidth = 0;
  let maskHeight = 0;
  let exact = true;
  let inked;
  try {
    if (job.devInkTrap && import.meta.env.DEV) throw new WebAssembly.RuntimeError('unreachable (dev ink trap)');
    const inCanvas = canvas;
    canvas = EMPTY;
    inked = export_ink(inCanvas, width, height, madeJson, fontIndex, fontPack, logoIndex, logoBlob);
  } catch (err) {
    const mapped = mapInkError(err);
    throw new Fail(mapped.kind, mapped.code, mapped.message);
  }
  try {
    width = inked.width;
    height = inked.height;
    maskWidth = inked.mask_width;
    maskHeight = inked.mask_height;
    exact = inked.exact;
    canvas = inked.canvas();
    stampMask = inked.stamp_mask();
  } finally {
    inked.free();
  }
  memory(job.id, 'ink', canvas.byteLength + stampMask.byteLength + layer.byteLength + source.byteLength);
  if (import.meta.env.DEV && job.devCanvasReport && (!exact || job.devForceCanvas)) {
    const hdr = new Uint8Array(8);
    const view = new DataView(hdr.buffer);
    view.setUint32(0, width, true);
    view.setUint32(4, height, true);
    await fetch(`/__dev/report/${encodeURIComponent(job.devCanvasReport)}`, {
      method: 'POST',
      headers: { 'content-type': 'application/octet-stream' },
      body: new Blob([hdr, canvas as Uint8Array<ArrayBuffer>]),
    }).catch(() => undefined);
    // 同一张的字形掩模（Rust 实际混合的 8-bit coverage）。模块只在开发服务器里动态载入。
    const name = encodeURIComponent(`${job.devCanvasReport}-masks`);
    await import('./dev_glyph_masks')
      .then(({ glyphMaskReport }) => glyphMaskReport(json.prepared, req.layout, exact, fontIndex, fontPack))
      .then((body) => fetch(`/__dev/report/${name}`, { method: 'POST', headers: { 'content-type': 'application/octet-stream' }, body }))
      .catch(() => undefined);
  }
  const completeState = json.prepared.state as { labels?: Record<string, unknown> };
  if (job.halfframeSource && completeState && typeof completeState === 'object') {
    const labels = completeState.labels && typeof completeState.labels === 'object' ? { ...completeState.labels } : {};
    const prevTag = typeof labels.tag === 'string' ? labels.tag : '';
    labels.tag = `${prevTag}_half`;
    completeState.labels = labels;
  }
  if (job.stripCopyright && source.length) {
    source = stripExifCopyrightFromJpeg(source);
  }
  const done = export_complete(canvas, width, height, JSON.stringify(completeState), source, layer, stampMask, maskWidth, maskHeight);
  canvas = EMPTY;
  stampMask = EMPTY;
  layer = NO_LAYER;
  source = EMPTY;
  let file: Uint8Array;
  let result: Record<string, unknown>;
  try {
    result = (JSON.parse(done.json()) as { result: Record<string, unknown> }).result;
    file = done.file();
  } finally {
    done.free();
  }
  memory(job.id, 'complete', file.byteLength);
  const buffer = file.buffer as ArrayBuffer;
  post({ op: 'done', id: job.id, ok: true, file: buffer, result, shot, exact, ms: performance.now() - start }, [buffer]);
}

/** 导出面板上的成品尺寸：源尺寸从文件头来（相机 JPG 转正后的尺寸，或 DNG 的裁切窗口），有边框或时间戳时补齐字体与拍摄元数据。 */
async function runSizes(job: SizesJob): Promise<void> {
  await load();
  const [jpgHead, dngHead] = await Promise.all([headSlice(job.jpg), headSlice(job.dng)]);
  const head = job.useJpg ? jpgHead : dngHead;
  if (!head) throw new Error('no file');
  const size = job.useJpg ? jpegSize(head) : dngSize(head);
  if (!size) throw new Error('no size');
  let layout: Record<string, unknown> = job.layout ? { ...job.layout } : {};
  if ((job.side.border || job.side.stamp) && (jpgHead || dngHead)) {
    try {
      const raw = JSON.parse(export_meta_photo(jpgHead, dngHead, job.stem ?? '', job.mtimeMs, job.tzOffsetMin ?? 0)) as {
        frame?: Record<string, string>;
        taken?: string | null;
        file_time?: string | null;
      };
      layout = {
        ...layout,
        meta: raw.frame ?? {},
        taken: momentFromIso(raw.taken ?? null),
        file_time: momentFromIso(raw.file_time ?? null),
      };
    } catch {
      /* 文件头不含完整元数据时退回空拍摄信息。 */
    }
  }
  const crop = job.side.crop as { rect?: unknown; preset?: unknown } | undefined;
  const rect = Array.isArray(crop?.rect) ? JSON.stringify(crop.rect) : '';
  const preset = typeof crop?.preset === 'string' ? crop.preset : undefined;
  const fontPack = job.fontPack ? new Uint8Array(job.fontPack) : EMPTY;
  const json = export_output_sizes(size.width, size.height, JSON.stringify(job.side), '{}', JSON.stringify(layout), rect, preset, job.fontIndex ?? '', fontPack);
  const pixels = (s: { width: number; height: number } | null) => (s ? s.width * s.height : 0);
  const source = Math.max(pixels(jpgHead ? jpegSize(jpgHead) : null), pixels(dngHead ? dngSize(dngHead) : null));
  post({ op: 'sizes', id: job.id, ok: true, json, source });
}

self.onmessage = (event: MessageEvent<ExportIn>) => {
  const msg = event.data;
  if (msg.op === 'setup') {
    setup = msg.setup;
    return;
  }
  if (msg.op === 'run') {
    const { id } = msg.job;
    runJob(msg.job).catch((err: unknown) => {
      const fail = coreFail(err);
      post({ op: 'done', id, ok: false, kind: fail.kind, code: fail.code, message: fail.message });
    });
    return;
  }
  if (msg.op === 'sizes') {
    const { id } = msg.job;
    runSizes(msg.job).catch((err: unknown) => post({ op: 'sizes', id, ok: false, message: err instanceof Error ? err.message : String(err) }));
    return;
  }
  if (msg.op === 'decode') {
    const { id } = msg;
    msg.file
      .arrayBuffer()
      .then((buf) => decodeJpeg(new Uint8Array(buf)))
      .then(({ data, width, height }) => {
        const rgb = data.buffer as ArrayBuffer;
        post({ op: 'decoded', id, ok: true, rgb, width, height }, [rgb]);
      })
      .catch((err: unknown) => post({ op: 'decoded', id, ok: false, message: err instanceof Error ? err.message : String(err) }));
  }
};
