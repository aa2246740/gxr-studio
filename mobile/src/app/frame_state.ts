// 边框与时间戳的状态、Worker 版面缓存、EXIF 元数据读取与防过期版本校验。

import type { Engine } from '../../../frontend/src/index';
import { computeCardLayouts, computeFrameLayout, readFrameMeta } from '../core/api';
import { fontsNeeded, GXR_MARK, type FontKey } from '../lib/fonts';
import {
  DEFAULT_FRAME,
  FRAME_ORDER,
  isDateString,
  LOGO_STYLES,
  momentFromIso,
  normalizeFrame,
  normalizeStamp,
  pickLogoMode,
  sameFrameSpec,
  sameStampSpec,
  selectFrame,
  styleBgLum,
  type Deco,
  type FrameLayoutData,
  type FrameMetaResult,
  type FrameSpec,
  type FrameStyle,
  type LogoInfo,
  type StampSpec,
} from '../lib/frame_spec';
import { ensureFontsLoaded, fontReady } from './font_loader';
import { logoStore, MISSING_LOGO_TOAST } from './logo_store';
import { photoMs, type Photo } from './model';

const MAX_DECO_CACHE = 48;
const decoCache = new Map<string, Deco>();
const inFlightMeta = new WeakMap<
  Photo,
  { version: number; dng: File | null; jpg: File | null; promise: Promise<FrameMetaResult | null> }
>();

function rememberDeco(key: string, deco: Deco): void {
  if (decoCache.size >= MAX_DECO_CACHE) {
    const first = decoCache.keys().next().value;
    if (first !== undefined) decoCache.delete(first);
  }
  decoCache.set(key, deco);
}

export function formatLocalIsoDate(ms: number): string {
  const dt = new Date(ms);
  const y = dt.getFullYear();
  const m = String(dt.getMonth() + 1).padStart(2, '0');
  const d = String(dt.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

export function photoIsoDate(photo: Photo): string {
  const taken = photo.frameMeta?.taken;
  if (typeof taken === 'string' && isDateString(taken.slice(0, 10))) return taken.slice(0, 10);
  const fileTime = photo.frameMeta?.file_time;
  if (typeof fileTime === 'string' && isDateString(fileTime.slice(0, 10))) return fileTime.slice(0, 10);
  return formatLocalIsoDate(photoMs(photo));
}

export function photoIsoTime(photo: Photo): string {
  const taken = photo.frameMeta?.taken;
  if (typeof taken === 'string' && taken.length >= 16) return taken.slice(11, 16);
  const fileTime = photo.frameMeta?.file_time;
  if (typeof fileTime === 'string' && fileTime.length >= 16) return fileTime.slice(11, 16);
  const dt = new Date(photoMs(photo));
  return `${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}`;
}

export function isMtimeDate(photo: Photo): boolean {
  return Boolean(photo.frameMeta && !photo.frameMeta.taken && photo.frameMeta.file_time);
}

export function photoExportSize(photo: Photo, engine: Engine | null): [number, number] {
  const w = photo.frameMeta?.width;
  const h = photo.frameMeta?.height;
  if (w && h && w > 0 && h > 0) return [w, h];
  const gpuSize = engine?.finalSize();
  if (gpuSize && gpuSize.w > 0 && gpuSize.h > 0) return [gpuSize.w, gpuSize.h];
  if (photo.bitmap && photo.bitmap.width > 0 && photo.bitmap.height > 0) {
    return [photo.bitmap.width, photo.bitmap.height];
  }
  return [4000, 3000];
}

export function ensureFrameMeta(photo: Photo): Promise<FrameMetaResult | null> {
  if (photo.frameMeta) return Promise.resolve(photo.frameMeta);
  const pending = inFlightMeta.get(photo);
  if (pending && pending.version === photo.version && pending.dng === photo.dng && pending.jpg === photo.jpg) {
    return pending.promise;
  }
  const version = photo.version;
  const dng = photo.dng;
  const jpg = photo.jpg;
  const source = jpg ?? dng;
  const mtimeMs = source?.lastModified ?? photo.lastModified;
  const tzOffsetMin = Number.isFinite(mtimeMs) ? -new Date(mtimeMs).getTimezoneOffset() : 0;
  const name = photo.stem;
  const promise = readFrameMeta({
    jpg,
    dng,
    name,
    mtimeMs,
    tzOffsetMin,
  })
    .then((res) => {
      if (photo.version !== version || photo.dng !== dng || photo.jpg !== jpg) return null;
      photo.frameMeta = res;
      return res;
    })
    .catch(() => null)
    .finally(() => {
      const cur = inFlightMeta.get(photo);
      if (cur && cur.promise === promise) inFlightMeta.delete(photo);
    });
  inFlightMeta.set(photo, { version, dng, jpg, promise });
  return promise;
}

export function checkMissingLogo(photo: Photo, onToast?: (msg: string) => void): boolean {
  if (!logoStore.loaded()) return false;
  const id = photo.border.logo;
  if (!id || id === 'ricoh' || id === 'none') return false;
  if (logoStore.info(id)) return false;
  photo.border = normalizeFrame({ ...photo.border, logo: 'ricoh', show_logo: true });
  onToast?.(MISSING_LOGO_TOAST);
  return true;
}

export function autoMonoMessage(frame: FrameSpec, explicitInfo?: LogoInfo | null): string | null {
  if (!LOGO_STYLES.includes(frame.style) || frame.style === 'blur_fill') return null;
  const id = frame.logo || (frame.show_logo === false ? 'none' : 'ricoh');
  if (id === 'ricoh' || id === 'none' || frame.show_logo === false) return null;
  if (frame.logo_mode !== 'auto') return null;
  const info = explicitInfo ?? logoStore.info(id);
  if (!info || info.opaque) return null;
  const bgLum = styleBgLum(frame);
  if (pickLogoMode(info, 'auto', bgLum) !== 'mono') return null;
  const tone = bgLum < 128 ? '白色' : '深色';
  return `标志和这个边框的颜色太接近，已自动改成${tone}。要用原色，把「颜色」改成「原色」。`;
}

export function buildFrameRequestJson(
  ew: number,
  eh: number,
  photo: Photo,
  logoId: string | undefined,
): string {
  const logos: LogoInfo[] = [];
  if (logoId && logoId !== 'ricoh' && logoId !== 'none') {
    const info = logoStore.info(logoId);
    if (info) logos.push(info);
  }
  return JSON.stringify({
    w: ew,
    h: eh,
    meta: photo.frameMeta?.frame ?? {},
    logos,
    mark: GXR_MARK,
  });
}

export function buildStampRequestJson(
  ew: number,
  eh: number,
  photo: Photo,
  borderFrame: FrameSpec | null,
): string {
  const logos: LogoInfo[] = [];
  if (borderFrame && borderFrame.logo && borderFrame.logo !== 'ricoh' && borderFrame.logo !== 'none') {
    const info = logoStore.info(borderFrame.logo);
    if (info) logos.push(info);
  }
  return JSON.stringify({
    w: ew,
    h: eh,
    meta: photo.frameMeta?.frame ?? {},
    logos,
    mark: GXR_MARK,
    taken: momentFromIso(photo.frameMeta?.taken),
    file_time: momentFromIso(photo.frameMeta?.file_time),
    frame: borderFrame,
  });
}

export function commitFrame(
  photo: Photo,
  patch: Partial<FrameSpec>,
  onChanged?: () => void,
): boolean {
  const next = normalizeFrame({ ...photo.border, ...patch });
  if (sameFrameSpec(next, photo.border)) return false;
  photo.border = next;
  photo.rev += 1;
  onChanged?.();
  return true;
}

export function commitSelectStyle(
  photo: Photo,
  style: FrameStyle,
  onChanged?: () => void,
): boolean {
  const next = selectFrame(photo.border, style);
  if (sameFrameSpec(next, photo.border)) return false;
  photo.border = next;
  photo.rev += 1;
  onChanged?.();
  return true;
}

export function commitStamp(
  photo: Photo,
  patch: Partial<StampSpec>,
  onChanged?: () => void,
): boolean {
  const next = normalizeStamp({ ...photo.stamp, ...patch });
  if (sameStampSpec(next, photo.stamp)) return false;
  photo.stamp = next;
  photo.rev += 1;
  onChanged?.();
  return true;
}

export function cardSpecFor(style: FrameStyle, current: FrameSpec): FrameSpec | null {
  if (style === 'none') return null;
  const base = { ...DEFAULT_FRAME };
  return style === current.style ? { ...base, ...current } : selectFrame({ ...base, ...current }, style);
}

export function decoFontsReady(frame: FrameSpec | null, stamp: StampSpec | null): boolean {
  const needed = fontsNeeded(frame, stamp);
  return needed.every((k) => fontReady(k));
}

export async function resolveStageDeco(
  photo: Photo,
  engine: Engine | null,
  onAsyncUpdate: () => void,
  onToast?: (msg: string) => void,
  isAlive: () => boolean = () => true,
): Promise<Deco | null> {
  const version = photo.version;
  const dng = photo.dng;
  const jpg = photo.jpg;
  const stillAlive = (): boolean =>
    photo.version === version && photo.dng === dng && photo.jpg === jpg && isAlive();

  const hasFrame = photo.border.style !== 'none';
  const hasStamp = photo.stamp.enabled;
  if (!hasFrame && !hasStamp) return null;

  if (!logoStore.loaded()) {
    void logoStore.ensureLoaded().then(
      () => {
        if (stillAlive()) onAsyncUpdate();
      },
      () => undefined,
    );
  } else if (checkMissingLogo(photo, onToast)) {
    photo.rev += 1;
  }

  if (!photo.frameMeta) {
    void ensureFrameMeta(photo).then((res) => {
      if (res && stillAlive()) onAsyncUpdate();
    });
  }

  const [ew, eh] = photoExportSize(photo, engine);
  const frameSpec = hasFrame ? photo.border : null;
  const stampSpec = hasStamp ? photo.stamp : null;
  const needed = fontsNeeded(frameSpec, stampSpec);
  ensureFontsLoaded(needed, onAsyncUpdate, stillAlive);

  const logoId = frameSpec?.logo;
  const logoInfo = logoId && logoId !== 'ricoh' && logoId !== 'none' ? logoStore.info(logoId) : null;
  const key = JSON.stringify([
    photo.id,
    version,
    ew,
    eh,
    frameSpec,
    stampSpec,
    photo.frameMeta ?? null,
    logoInfo,
  ]);
  const cached = decoCache.get(key);
  if (cached) return cached;

  const borderStamp = Boolean(stampSpec && stampSpec.placement === 'border' && frameSpec?.style === 'instant');
  const metaJson = buildFrameRequestJson(ew, eh, photo, frameSpec?.logo);
  const stampMetaJson = stampSpec ? buildStampRequestJson(ew, eh, photo, borderStamp ? frameSpec : null) : null;

  const result = await computeFrameLayout({
    metaJson,
    frameSpecJson: frameSpec ? JSON.stringify(frameSpec) : null,
    stampMetaJson,
    stampSpecJson: stampSpec ? JSON.stringify(stampSpec) : null,
    fontKeys: needed,
  });

  if (!stillAlive() || (!result.frame && !result.stamp)) return null;
  const deco: Deco = {
    key,
    frame: result.frame,
    stamp: result.stamp,
    stampOrigin: borderStamp ? [0, 0] : null,
    ew,
    eh,
    width: result.frame ? result.frame.width : ew,
    height: result.frame ? result.frame.height : eh,
  };
  rememberDeco(key, deco);
  return deco;
}

export async function resolveCardLayouts(
  photo: Photo,
  ew: number,
  eh: number,
  onFontsReady: () => void,
  isAlive: () => boolean = () => true,
): Promise<Record<string, FrameLayoutData | null>> {
  const version = photo.version;
  const stillAlive = (): boolean => photo.version === version && isAlive();
  const needed = new Set<FontKey>();
  const cards: Array<{ id: string; specJson: string }> = [];
  for (const style of FRAME_ORDER) {
    if (style === 'none') continue;
    const spec = cardSpecFor(style, photo.border);
    if (!spec) continue;
    for (const k of fontsNeeded(spec, null)) needed.add(k);
    cards.push({ id: style, specJson: JSON.stringify(spec) });
  }
  const fontKeys = [...needed];
  ensureFontsLoaded(fontKeys, onFontsReady, stillAlive);
  const metaJson = buildFrameRequestJson(ew, eh, photo, photo.border.logo);
  return computeCardLayouts(metaJson, fontKeys, cards);
}
