// 这一次打开里的照片。文件句柄留着，字节只在正在看的那一张上。

import type { RawLayout, Scene } from '../../../frontend/src/index';
import { describeCamFilesMismatch } from '../lib/camfiles';
import type { CropState, GeometryState } from '../lib/crop';
import { copyrightFromTiff, mergeCopyright, type PhotoMeta } from '../lib/exif';
import type { FrameMetaResult, FrameSpec, StampSpec } from '../lib/frame_spec';
import type { GapMinutes, Order } from '../lib/group';
import type { CurveMap } from '../lib/export_request';
import type { Frame } from '../lib/photo_size';
import { parseRecipeTag } from '../lib/recipe_tags';
import {
  datKeyOf,
  NO_DAT_CAMERA_RECIPE_KEY,
  type CameraRecipeCache,
  type CameraRecipeResult,
  type Choice,
} from '../lib/recipes';
import type { Book } from './book';
import { lookCache } from './looks';
import type { LutBook } from './lut_book';
import type { LutGpu } from './lut_gpu';

/** 缩略图这一步的状态。文件读不读得了、显卡里有没有源，是另外两件事（gone、cached）。 */
export type PhotoStatus = 'pending' | 'ready' | 'bad';

export interface Knobs {
  exposure_ev: number;
  contrast: number;
  highlight: number;
  shadow: number;
  black_point: number;
  warm: number;
  tint: number;
  saturation: number;
  vibrance: number;
  vignette: number;
}

export function zeroKnobs(): Knobs {
  return {
    exposure_ev: 0,
    contrast: 0,
    highlight: 0,
    shadow: 0,
    black_point: 0,
    warm: 0,
    tint: 0,
    saturation: 0,
    vibrance: 0,
    vignette: 0,
  };
}

export interface Photo {
  id: string;
  stem: string;
  /** 每换一次 File（重新选了同名照片）加一。异步结果回来时先核对它，过期的丢掉。 */
  version: number;
  dng: File | null;
  jpg: File | null;
  /** 认出的半格双联 JPG（第 2 张）：保留 JPG，丢掉自己的 DNG，编辑和导出按只有 JPG 处理。 */
  diptych?: boolean;
  lastModified: number;
  captureMs: number | null;
  status: PhotoStatus;
  /** 这个版本的文件读不了（卡拔了）。重新选同一张会换版本、清掉它。 */
  gone: boolean;
  /** 显卡里已有这张的完整源，是哪个版本。等于 version 时不用再读卡就能打开、调整。 */
  cached: number | null;
  /** WASM 描述通过的版本。没有显卡时，据此不用再读卡就能打开。 */
  checked: number | null;
  bitmap: ImageBitmap | null;
  /** 显卡出的中性转换小图（配方卡用），按版本缓存，只做一次。 */
  neutral: { version: number; bitmap: ImageBitmap } | null;
  knobs: Knobs;
  /** 桌面的四条曲线。null 是没调过；恒等通道不放在里面。 */
  curve: CurveMap | null;
  /** 相机 JPG 和 DNG 里读到的镜头名、ISO（桌面导入时先用 JPG 的）。 */
  jpgMeta: PhotoMeta | null;
  dngMeta: PhotoMeta | null;
  /** 边框 / 时间戳规格与 Worker 读出来的完整 EXIF/尺寸信息。 */
  border: FrameSpec;
  stamp: StampSpec;
  frameMeta: FrameMetaResult | null;
  /** 文件头读出的方向和转正尺寸（JPG 优先，否则 DNG）。裁剪提交和导出用来对拍。 */
  frame: Frame | null;
  /** 手机端的几何编辑（只有拉直；旋转 90° / 翻转 / 透视 / 畸变留在 0 / false）。 */
  geometry: GeometryState;
  /** 手机端的裁切状态；null 表示未改过（默认原比例）。 */
  crop: CropState | null;
  /**
   * 编辑状态的版本：每次落定的修改（选配方、旋钮松手、滑块松手、裁剪/拉直落定、重置、套用、撤销）加一。重新选同一张照片不变。
   * 撤销套用时核对它，和桌面 sidecar 的 rev 一样：套用之后又改过的不还原。
   */
  rev: number;
  /** 选中的配方。和调整一样是这张照片这次的编辑状态；换片、重新选同一张都留着。 */
  choice: Choice | null;
  /** 显式关闭拍摄配方采用（如套用了仅含 LUT 的剪贴板，对应桌面 sidecar recipe: null）。 */
  recipeOff?: boolean;
  /**
   * 选中的配方的旋钮档位（桌面 `recipe.levels`）：只有一项，键是 `choice.id`；只记用户给过的档位，没给的取配方默认。
   */
  levels: Map<string, Record<string, number>>;
  /**
   * 桌面的 `memory`（`look_cards.js` recipeTarget）：换配方时切走的那个配方的档位记在这里，键是卡的 id
   * （导出时按镜头单元换成桌面的 recipeKey）。当前配方不在这里更新；套用时整份跟过来，来源是空的就保留目标原来的。
   */
  memory: Map<string, Record<string, number>>;
  /** 选中的 LUT（`.cube` 文件 SHA-256 `cube_id`，null 表示不用 LUT）。 */
  lut: string | null;
  /** 配方卡与 LUT 卡的小图，按烘焙/LUT 令牌的键记，只对当时的版本有效。 */
  looks: Map<string, { version: number; bitmap: ImageBitmap }>;
  /** DNG 文件头里的 Exif Copyright (0x8298)，只在 JPG 没有 Copyright 时按需补读。 */
  dngCopyright?: string | null;
  /** 已补读过 DNG 头部 Copyright 的照片版本。 */
  dngCopyrightRead?: number | null;
  /** 拍摄配方标签在当前相机配方文件下的解析结果缓存；不写进 choice。 */
  cameraRecipe?: CameraRecipeCache | null;
  /** 同名照片另外留下的第几份（2 起，new_photo.ts）。第一份没有。界面上写「名字 (N)」（photoName）。 */
  copy?: number;
  /** 文件所在的文件夹（webkitRelativePath 的目录）；浏览器没给就是空的。配对 RAW+JPG 和半格邻居先看它。 */
  folder?: string;
}

/** 界面上这张照片叫什么：第一份是文件名，同名另外留下的写「名字 (2)」。网格、编辑器、复制套用和导出的字都用它。 */
export function photoName(photo: { stem: string; copy?: number }): string {
  return photo.copy ? `${photo.stem} (${photo.copy})` : photo.stem;
}

/** 同名另外留下的那张导出的文件名：核心起的名字以文件名开头（`R0001_gxr.jpg`），就在文件名后面加「 (2)」；不是的话加在扩展名前面。 */
export function copyFileName(name: string, stem: string, copy: number): string {
  if (name.startsWith(stem)) return `${stem} (${copy})${name.slice(stem.length)}`;
  const dot = name.lastIndexOf('.');
  return dot > 0 ? `${name.slice(0, dot)} (${copy})${name.slice(dot)}` : `${name} (${copy})`;
}

/** 剪贴板记的来源（`{ id, stem }`）叫什么：照片还在就按 photoName，不在了用记下的文件名。 */
export function nameOf(api: { photo(id: string): Photo | undefined }, from: { id: string; stem: string }): string {
  const photo = api.photo(from.id);
  return photo ? photoName(photo) : from.stem;
}

/** stale：读的时候照片被重新选过，结果作废。 */
export type FullRead = { ok: true; bytes: ArrayBuffer } | { ok: false; gone: boolean; stale: boolean };

/** 读文件时文件已经不在或读不了（用户删了、移走了、权限变了）。 */
export function isGone(err: unknown): boolean {
  const name = err instanceof DOMException ? err.name : err instanceof Error ? err.name : '';
  return name === 'NotFoundError' || name === 'NotReadableError';
}

/**
 * 点得开：缩略图好了，并且文件还读得了，或者这个版本的完整源还在显卡里。
 * 卡拔了以后，只有缩略图、还要原文件的照片不算：换配方、导出都得先重新选。
 */
export function openable(photo: Photo): boolean {
  if (photo.status !== 'ready') return false;
  return !photo.gone || photo.cached === photo.version;
}

/** 卡拔了、又没有缓存，真的没读完。 */
export function unread(photo: Photo): boolean {
  return photo.gone && !openable(photo);
}

/** 放掉这张照片手里的位图（缩略图和中性小图）。 */
export function releaseBitmaps(photo: Photo): void {
  photo.bitmap?.close();
  photo.bitmap = null;
  photo.neutral?.bitmap.close();
  photo.neutral = null;
  releaseLooks(photo);
}

/** 放掉配方卡的小图（换版本、清空时）。 */
export function releaseLooks(photo: Photo): void {
  lookCache.forget(photo);
}

/** 清掉这张照片的拍摄配方缓存和 DNG 头部补读标记（重选文件或识别为半格双联时调用）。 */
export function clearCameraRecipe(photo: Photo): void {
  photo.dngCopyright = null;
  photo.dngCopyrightRead = null;
  photo.cameraRecipe = null;
}

export function photoMs(photo: Photo): number {
  return photo.captureMs ?? photo.lastModified;
}

export interface AppApi {
  photos(): Photo[];
  photo(id: string): Photo | undefined;
  order(): Order;
  gap(): GapMinutes;
  ignored(): number;
  hasDat(): boolean;
  loading(): { done: number; total: number };
  setOrder(order: Order): void;
  setGap(gap: GapMinutes): void;
  open(id: string): void;
  remove(id: string): void;
  clear(): void;
  addPhotos(): void;
  showHelp(): void;
  back(): void;
  swipe(id: string, dir: -1 | 1): void;
  markBad(id: string, version: number): void;
  markGone(id: string, version: number): void;
  /** 整份 DNG，在 Worker 里读完转移过来。主线程不调 File.arrayBuffer。 */
  readFull(photo: Photo): Promise<FullRead>;
  /**
   * 把这张 RAW 交给显卡。同一张照片的上传排成一队：前一次做完才开始下一次，开始前再核对照片、File 和版本，
   * 所以旧版本的上传不会在新版本之后把源换回去。返回 false 表示这次请求已经过期，没有提交。
   */
  uploadRaw(photo: Photo, version: number, layout: RawLayout, bytes: ArrayBuffer, scene: Scene | null): Promise<boolean>;
  /** 下一张的 DNG 字节。离开编辑器就丢掉，最多留一份。 */
  takeWarm(id: string, version: number): ArrayBuffer | null;
  keepWarm(id: string, version: number, bytes: ArrayBuffer): void;
  gpu(): boolean;
  /** 这次会话的编号。清空照片就换一个；异步结果据此判断还是不是这次的。 */
  session(): number;
  /** 卡被拔出过、还有没读完的照片。导航副标题按它写，普通刷新不会盖掉。 */
  cardOut(): boolean;
  /** 编辑器里没有显卡或只有 JPG 时，按屏幕宽度在 Worker 里解一张清楚的。 */
  still(photo: Photo, width: number): Promise<ImageBitmap | null>;
  watch(kind: 'scroll' | 'drag', on: boolean): void;
  /** 相机配方文件、校准数据和烘焙表。 */
  book(): Book;
  /** 用户 LUT 目录。 */
  lutBook(): LutBook;
  /** GPU LUT 纹理队列（3 槽上限、短命 lut_table Worker）。 */
  lutGpu(): LutGpu;
  /** 选 .cube LUT 文件。 */
  pickLut(): void;
  /** 只选 GXRCE.DAT（06d 的按钮）。选完留在当前这一屏。 */
  pickDat(): void;
  /** 选校准数据 zip。 */
  pickCalib(): void;
  /** 「更多 → 校准数据」。 */
  showCalib(): void;
  /** 打开导出面板（05a）。`ids` 按网格的顺序导出；点了「存到相册」时调 `onStart`。 */
  exportPhotos(ids: string[], onStart?: () => void): void;
  /** 图库多选变了（进出多选、勾选变了）：还在等分包的点开、导出连同它们的错误提示和重试一起作废。 */
  dropPending(): void;
}

type HeadReader = (file: File) => Promise<ArrayBuffer | null>;
export type CameraRecipeResolver = (
  tag: string,
  dat: ArrayBuffer | null,
  library: ArrayBuffer | null,
) => Promise<CameraRecipeResult>;

export interface EnsureCameraRecipeOptions {
  /** 为 true 时即使未加载 DAT 也检查 `GXR1 ` 标签，供导出附注提示「请先导入拍摄时使用的相机配方文件。」。 */
  checkMissingDat?: boolean;
  resolve?: CameraRecipeResolver;
}

let dngHeadReader: HeadReader | null = null;
const cameraRecipeListeners = new Set<(photoId: string) => void>();
const cameraRecipePending = new WeakMap<Photo, { key: string; promise: Promise<void> }>();
const cameraRecipeMemo = new WeakMap<CameraRecipeResolver, Map<string, Promise<CameraRecipeResult>>>();

async function defaultCameraRecipeResolver(
  tag: string,
  dat: ArrayBuffer | null,
  library: ArrayBuffer | null,
): Promise<CameraRecipeResult> {
  const { cameraRecipe } = await import('../core/api');
  return cameraRecipe(tag, dat, library);
}

function resolveCameraRecipeMemo(
  resolver: CameraRecipeResolver,
  key: string,
  copyright: string,
  dat: Book['dat'],
): Promise<CameraRecipeResult> {
  let byKey = cameraRecipeMemo.get(resolver);
  if (!byKey) {
    byKey = new Map();
    cameraRecipeMemo.set(resolver, byKey);
  }
  const memoKey = `${key}\0${copyright}`;
  const hit = byKey.get(memoKey);
  if (hit) return hit;
  const datCopy = dat ? dat.read.bytes.slice(0) : null;
  const libCopy = dat?.read.library ? dat.read.library.slice(0) : null;
  const pending = resolver(copyright, datCopy, libCopy).catch((err) => {
    if (byKey?.get(memoKey) === pending) byKey.delete(memoKey);
    throw err;
  });
  if (byKey.size >= 256) byKey.clear();
  byKey.set(memoKey, pending);
  return pending;
}

/** 注册 Worker 端 DNG 头部读取器（主线程不调 File.arrayBuffer）。 */
export function registerCameraRecipeReader(reader: HeadReader): void {
  dngHeadReader = reader;
}

/** 某张照片的拍摄配方解析落地时通知编辑页刷新。 */
export function onCameraRecipe(fn: (photoId: string) => void): () => void {
  cameraRecipeListeners.add(fn);
  return () => cameraRecipeListeners.delete(fn);
}

function cameraRecipeKey(dat: Book['dat'], checkMissingDat = false): string | null {
  if (dat) return datKeyOf(dat);
  return checkMissingDat ? NO_DAT_CAMERA_RECIPE_KEY : null;
}

/** 这张照片在当前相机配方文件下是否还需要解析拍摄配方标签。 */
export function needsCameraRecipe(photo: Photo, dat: Book['dat'], options?: { checkMissingDat?: boolean }): boolean {
  if (!photo.dng || photo.diptych || photo.choice !== null || photo.recipeOff) return false;
  const key = cameraRecipeKey(dat, options?.checkMissingDat);
  if (!key) return false;
  const cache = photo.cameraRecipe;
  return !cache || cache.datKey !== key || cache.version !== photo.version;
}

/** 按桌面优先级（JPG 优先、DNG 兜底）读取 Exif Copyright，并在核心 Worker 里按 `(datKey, tag)` 记忆化解析 `GXR1` 标签。 */
export function ensureCameraRecipe(photo: Photo, book: Book, options?: EnsureCameraRecipeOptions): Promise<void> {
  const dat = book.dat;
  const checkMissingDat = Boolean(options?.checkMissingDat);
  if (!needsCameraRecipe(photo, dat, { checkMissingDat })) return Promise.resolve();
  const key = cameraRecipeKey(dat, checkMissingDat);
  if (!key) return Promise.resolve();
  const version = photo.version;
  const inflightKey = `${version}:${key}`;
  const existing = cameraRecipePending.get(photo);
  if (existing && existing.key === inflightKey) return existing.promise;

  const resolver = options?.resolve ?? defaultCameraRecipeResolver;
  const promise = (async () => {
    let copyright: string | null;
    if (photo.jpgMeta?.copyright != null) {
      copyright = photo.jpgMeta.copyright;
    } else {
      let dngCopy: string | null = null;
      if (photo.dngMeta !== null) {
        dngCopy = photo.dngMeta.copyright ?? null;
      } else if (photo.dngCopyrightRead === version) {
        dngCopy = photo.dngCopyright ?? null;
      } else if (photo.dng && dngHeadReader) {
        const head = await dngHeadReader(photo.dng).catch(() => null);
        if (photo.version !== version) return;
        // 读 DNG 头部失败：不当成“没有标签”缓存，留给下一次请求重试。
        if (!head) return;
        dngCopy = copyrightFromTiff(new Uint8Array(head));
        photo.dngCopyright = dngCopy;
        photo.dngCopyrightRead = version;
      }
      copyright = mergeCopyright(photo.jpgMeta?.copyright, dngCopy);
    }
    if (photo.version !== version || cameraRecipeKey(book.dat, checkMissingDat) !== key) return;
    let result: CameraRecipeResult;
    if (copyright === null || !copyright.startsWith('GXR1 ')) {
      result = { status: 'none' };
    } else {
      try {
        result = await resolveCameraRecipeMemo(resolver, key, copyright, dat);
      } catch {
        // Worker 或传输临时出错时不缓存成 none，留给下一次请求重试。
        return;
      }
      // 同库代数内不同目录（如非 KUBIK g185/g250 对 KUBIK 照片）或未知未来目录：绝不采用错误配方。
      if (dat && result.status !== 'error') {
        const mismatch = describeCamFilesMismatch(parseRecipeTag(copyright), dat);
        if (mismatch) result = { status: 'error', error: { code: mismatch.code, message: '请先导入拍摄时使用的相机配方文件。' } };
      }
    }
    if (photo.version !== version || cameraRecipeKey(book.dat, checkMissingDat) !== key) return;
    photo.cameraRecipe = { datKey: key, copyright, version, result };
    for (const fn of [...cameraRecipeListeners]) fn(photo.id);
  })()
    .catch(() => undefined)
    .finally(() => {
      if (cameraRecipePending.get(photo)?.key === inflightKey) cameraRecipePending.delete(photo);
    });

  cameraRecipePending.set(photo, { key: inflightKey, promise });
  return promise;
}

export async function ensureCameraRecipes(
  photos: readonly Photo[],
  book: Book,
  options?: EnsureCameraRecipeOptions,
): Promise<void> {
  if (!book.dat && !options?.checkMissingDat) return;
  await Promise.all(photos.map((photo) => ensureCameraRecipe(photo, book, options)));
}
