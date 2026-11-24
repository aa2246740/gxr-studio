// 主线程和核心 Worker 之间的消息。

import type { FontKey } from '../lib/fonts';
import type { LutPatchFields } from '../lib/idb';

export type CoreOp =
  | 'ping'
  | 'describe'
  | 'dat'
  | 'calib-file'
  | 'calib-restore'
  | 'calib-default'
  | 'bake'
  | 'resolve'
  | 'camera-recipe'
  | 'calib-check'
  | 'frame-meta'
  | 'frame-layout'
  | 'logo-preview'
  | 'logo-save'
  | 'geom-crop-write'
  | 'geom-geometry-write'
  | 'geom-final-box'
  | 'export-output-sizes'
  | 'lut-import'
  | 'lut-restore'
  | 'lut-patch'
  | 'lut-delete'
  | 'lut-blob'
  | 'lut-snapshot'
  | 'hdr-preview-grid';

export class CoreError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.code = code;
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

/** `color_resolve`：这份 DAT 上的一个配方选择（桌面 RecipeSel 的 JSON）。`library` 是同卡的 GXRFX.BIN。 */
export interface ResolveArgs {
  dat: ArrayBuffer;
  recipe: string;
  library?: ArrayBuffer | null;
}

/** `camdata_camera_recipe`：Exif Copyright 拍摄标签 + 当前加载的 DAT / GXRFX.BIN（均可为 null）。 */
export interface CameraRecipeArgs {
  tag: string;
  dat?: ArrayBuffer | null;
  library?: ArrayBuffer | null;
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

export interface FrameMetaArgs {
  jpg: File | null;
  dng: File | null;
  name: string;
  mtimeMs: number;
  tzOffsetMin: number;
}

export interface FrameLayoutArgs {
  metaJson: string;
  frameSpecJson: string | null;
  stampMetaJson: string | null;
  stampSpecJson: string | null;
  fontKeys: FontKey[];
  cards?: Array<{ id: string; specJson: string }> | null;
}

export interface LogoSaveArgs {
  name: string;
  keyWhite: boolean;
  tol: number;
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

/** 导入一个 `.cube` LUT：File 交给 Worker，大小检查、sha256、解析识别、写 IndexedDB 都在 Worker 里做。 */
export interface LutImportArgs {
  file: File;
  filename: string;
  version: number;
}

export interface LutPatchArgs {
  cubeId: string;
  patch: LutPatchFields;
  version: number;
}

export interface LutDeleteArgs {
  cubeId: string;
  version: number;
}

export interface LutBlobArgs {
  cubeId: string;
}

export interface LutSnapshotArgs {
  cubeIds: string[];
}

export interface HdrGridArgs {
  width: number;
  height: number;
  dng: ArrayBuffer;
  jpg: ArrayBuffer | null;
  base?: ArrayBuffer | null;
  baseWidth?: number;
  baseHeight?: number;
  geometryJson?: string | null;
  cropJson?: string | null;
  box?: { x: number; y: number; w: number; h: number } | null;
  frameLayoutJson?: string | null;
  factor?: number;
}

export interface CoreRequest {
  id: number;
  op: CoreOp;
  buffer?: ArrayBuffer | null;
  args?: BakeArgs | null;
  /** 校准数据 zip / 标志图片：File 交给 Worker，大小在 Worker 里读之前先查，主线程不碰字节。 */
  file?: File | null;
  /** 导入时存进 IndexedDB 用的名字和时间。 */
  meta?: { name: string; savedAt: number } | null;
  /** 内置的默认校准：下载地址和整个文件应有的 SHA-256。 */
  defaultCalib?: { url: string; sha256: string } | null;
  resolve?: ResolveArgs | null;
  cameraRecipe?: CameraRecipeArgs | null;
  check?: CalibCheckArgs | null;
  frameMeta?: FrameMetaArgs | null;
  frameLayout?: FrameLayoutArgs | null;
  logoSave?: LogoSaveArgs | null;
  geom?: GeomWriteArgs | null;
  finalBox?: FinalBoxArgs | null;
  sizes?: OutputSizesArgs | null;
  lutImport?: LutImportArgs | null;
  lutPatch?: LutPatchArgs | null;
  lutDelete?: LutDeleteArgs | null;
  lutBlob?: LutBlobArgs | null;
  lutSnapshot?: LutSnapshotArgs | null;
  hdrGrid?: HdrGridArgs | null;
}

export type CoreReply =
  | { id: number; ok: true; json: string; buffers: ArrayBuffer[] }
  | { id: number; ok: false; code?: string; message: string };

export interface CoreResult {
  json: string;
  buffers: ArrayBuffer[];
}
