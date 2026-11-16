// 主线程和核心 Worker 之间的消息。

export type CoreOp =
  | 'ping'
  | 'describe'
  | 'dat'
  | 'calib-file'
  | 'calib-restore'
  | 'bake'
  | 'resolve'
  | 'calib-check'
  | 'geom-crop-write'
  | 'geom-geometry-write'
  | 'geom-final-box'
  | 'export-output-sizes';

export class CoreError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = 'CoreError';
  }
}

/** 烘焙一张表要的全部输入。字节都是拷贝，交给 Worker 后这边不再用。 */
export interface BakeArgs {
  entry: ArrayBuffer;
  stock: ArrayBuffer;
  menu: string;
  ctx: ArrayBuffer;
  ring: ArrayBuffer;
  params: string;
}

/** `color_resolve`：这份 DAT 上的一个配方选择（桌面 RecipeSel 的 JSON）。 */
export interface ResolveArgs {
  dat: ArrayBuffer;
  recipe: string;
}

/** `color_calib_check`：校准包的 manifest、其余文件、这个单元的原厂 04 和 ISO。 */
export interface CalibCheckArgs {
  manifest: ArrayBuffer;
  names: string[];
  blobs: ArrayBuffer[];
  stock: ArrayBuffer;
  unit: string;
  iso: number;
}

export interface GeomWriteArgs {
  side: string;
  payload: string;
}

export interface FinalBoxArgs {
  w: number;
  h: number;
  geometry: string;
  crop: string;
}

export interface OutputSizesArgs {
  w: number;
  h: number;
  side: string;
  rect: string;
  preset?: string;
}

export interface CoreRequest {
  id: number;
  op: CoreOp;
  buffer?: ArrayBuffer | null;
  args?: BakeArgs | null;
  /** 校准数据 zip：File 交给 Worker，大小在 Worker 里读之前先查，主线程不碰字节。 */
  file?: File | null;
  /** 导入时存进 IndexedDB 用的名字和时间。 */
  meta?: { name: string; savedAt: number } | null;
  resolve?: ResolveArgs | null;
  check?: CalibCheckArgs | null;
  geom?: GeomWriteArgs | null;
  finalBox?: FinalBoxArgs | null;
  sizes?: OutputSizesArgs | null;
}

export type CoreReply =
  | { id: number; ok: true; json: string; buffers: ArrayBuffer[] }
  | { id: number; ok: false; code?: string; message: string };

export interface CoreResult {
  json: string;
  buffers: ArrayBuffer[];
}
