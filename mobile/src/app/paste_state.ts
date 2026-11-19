// 复制 / 套用这一次会话里的状态：剪贴板、撤销记录、底栏上现在写什么。整个页面一份（网格和编辑器会重建，它不跟着丢）。
// 剪贴板只在这一页的内存里，和桌面一样；撤销也只在内存里，重新打开页面就没有了。

import { unitOfLens } from '../lib/calib';
import { cloneCrop, geometrySidecar, sideSubset, zeroGeometry } from '../lib/crop';
import { mergeMeta } from '../lib/exif';
import { formatClock } from '../lib/group';
import { isDisplaySpace } from '../lib/lut';
import { curveJson } from '../lib/export_request';
import { copyFrom, pasteBatch, PasteUndo, undoBatch, type Clip, type Env, type Target } from '../lib/paste';
import { photoWithAdopted, shelf, shelves } from '../lib/recipes';
import { copyMessage, groupName, pasteMessage, undoMessage, type GroupId, type Outcome } from '../lib/paste_text';
import type { AppApi, Photo } from './model';

/** 几何 / 裁剪变了，把旧图刷出的中性小图和配方卡小图放掉（不在这里 static import model.ts，Node 测试安全）。 */
function invalidateThumbs(photo: Photo | undefined): void {
  if (!photo) return;
  photo.neutral?.bitmap?.close?.();
  photo.neutral = null;
  if (photo.looks) {
    for (const entry of photo.looks.values()) entry?.bitmap?.close?.();
    photo.looks.clear();
  }
}

/** model.ts 的 photoMs（这里只 import 类型，Node 测试不用把 looks.ts 也加载进来）。 */
function photoMs(photo: Photo): number {
  return photo.captureMs ?? photo.lastModified;
}

/** 桌面普通提示（没有按钮）停留的时间。 */
export const INFO_MS = 3200;
/** 桌面「撤销」提示的 `timeout: 15000`：离开网格这么久以后底栏收起。 */
export const UNDO_MS = 15_000;

export type Bar =
  | { kind: 'applied'; batchId: string; line: string; sub: string; used: boolean; ids: ReadonlySet<string> }
  | { kind: 'info'; line: string }
  | null;

/** 每张目标照片在这次会话里的身份：清空、去掉、重新选过的照片不再套用。 */
function targetOf(photo: Photo): Target {
  const meta = mergeMeta(photo.jpgMeta, photo.dngMeta);
  return { photo, raw: photo.dng !== null, unit: unitOfLens(meta.lens), version: photo.version, rev: photo.rev };
}

function envOf(api: AppApi): Env {
  const dat = api.book().dat;
  const lutBook = (api as Partial<AppApi>).lutBook?.();
  return {
    recipeWithoutRaw: false,
    listed: (unit, id) => (dat ? shelf(shelves(dat.listing, unit), 'all').some((card) => card.id === id) : false),
    lutNeedsRaw: (cubeId) => {
      const rec = lutBook?.get(cubeId);
      if (!rec || !rec.input_space) return 'missing';
      return !isDisplaySpace(rec.input_space);
    },
  };
}

/** 一组照片的时间范围（设计稿的「17:43–17:45」）。 */
export function rangeOf(photos: readonly Photo[]): string {
  if (!photos.length) return '';
  const times = photos.map((photo) => photoMs(photo));
  const a = formatClock(Math.min(...times));
  const b = formatClock(Math.max(...times));
  return a === b ? a : `${a}–${b}`;
}

/** 「配方 / 风格、调整」。 */
export function groupsText(groups: readonly GroupId[]): string {
  return groups.map(groupName).join('、');
}

/** 确认句里的「配方 / 风格和调整」：最后两项用「和」连起来。 */
export function groupsAnd(groups: readonly GroupId[]): string {
  const names = groups.map(groupName);
  if (names.length <= 1) return names.join('');
  return `${names.slice(0, -1).join('、')}和${names[names.length - 1]}`;
}

let batchSeq = 0;

export class PasteState {
  clip: Clip | null = null;
  readonly undo = new PasteUndo();
  bar: Bar = null;
  /** 「选照片…」：下一次打开网格时进挑选模式。 */
  pickNext = false;
  /** 正在套用（按时间片）。这时再点套用不理。 */
  busy = false;
  /** 上一批每张的结果（和桌面 `Outcome.to_json` 同形；对拍测试读它）。 */
  lastOutcomes: Outcome[] = [];
  /** 每一片最多占主线程多久（测试可以改小，让每张各占一片）。 */
  budgetMs = 4;
  private session = -1;
  private leaveTimer = 0;
  /** 网格现在看得见吗；看不见时从什么时候起（Date.now）。结果在离开网格之后才到，也按离开那一刻算 15 秒。 */
  private onGrid = true;
  private leftAt = 0;
  private infoTimer = 0;
  private readonly listeners = new Set<() => void>();
  private readonly editListeners = new Set<(ids: ReadonlySet<string>) => void>();

  onChange(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /** 照片的编辑状态被套用或撤销改了（编辑器开着这张就重画）。 */
  onEdit(fn: (ids: ReadonlySet<string>) => void): () => void {
    this.editListeners.add(fn);
    return () => this.editListeners.delete(fn);
  }

  private changed(): void {
    for (const fn of [...this.listeners]) fn();
  }

  private edited(ids: ReadonlySet<string>): void {
    if (!ids.size) return;
    for (const fn of [...this.editListeners]) fn(ids);
  }

  /** 换了会话（清空照片）：剪贴板、撤销、底栏都不要了。 */
  sync(api: AppApi): void {
    if (import.meta.env.DEV) exposeEdits(api);
    if (api.session() !== this.session) {
      this.session = api.session();
      this.reset();
    }
  }

  reset(): void {
    this.clip = null;
    this.undo.clear();
    this.pickNext = false;
    this.setBar(null);
  }

  private setBar(bar: Bar): void {
    window.clearTimeout(this.infoTimer);
    this.infoTimer = 0;
    window.clearTimeout(this.leaveTimer);
    this.leaveTimer = 0;
    this.bar = bar;
    if (bar?.kind === 'applied' && !this.onGrid) this.expireApplied(bar);
    if (bar?.kind === 'info') {
      this.infoTimer = window.setTimeout(() => {
        if (this.bar === bar) {
          this.bar = null;
          this.changed();
        }
      }, INFO_MS);
    }
    this.changed();
  }

  /** 网格上写一行（没复制就点了套用之类）。 */
  info(line: string): void {
    this.setBar({ kind: 'info', line });
  }

  /** 回到了网格：底栏不再倒计时。 */
  gridShown(): void {
    this.onGrid = true;
    window.clearTimeout(this.leaveTimer);
    this.leaveTimer = 0;
  }

  /** 离开网格：记下离开的时刻；套用结果从这一刻起再留 15 秒（结果还没到的，到的时候补上计时）。 */
  gridLeft(): void {
    if (this.onGrid) {
      this.onGrid = false;
      this.leftAt = Date.now();
    }
    if (this.bar?.kind === 'applied' && !this.leaveTimer) this.expireApplied(this.bar);
  }

  private expireApplied(bar: Bar): void {
    window.clearTimeout(this.leaveTimer);
    const left = Math.max(0, UNDO_MS - (Date.now() - this.leftAt));
    this.leaveTimer = window.setTimeout(() => {
      this.leaveTimer = 0;
      if (this.bar === bar) {
        this.bar = null;
        this.changed();
      }
    }, left);
  }

  /** 复制：勾选的、这张确实设置过的组进剪贴板。什么都没有就不动剪贴板。返回要说的那句。 */
  copy(api: AppApi, photo: Photo, groups: readonly GroupId[]): { ok: boolean; line: string } {
    this.sync(api);
    const result = copyFrom(photoWithAdopted(photo, api.book().dat), photo.stem, groups);
    if (result.clip) {
      this.clip = result.clip;
      this.setBar(null);
    }
    return { ok: result.clip !== null, line: copyMessage(result) };
  }

  /** 「清除」：剪贴板丢掉。 */
  drop(): void {
    this.clip = null;
    this.setBar(null);
  }

  /** 这些照片里去掉来源那张，按传进来的顺序。 */
  targets(api: AppApi, ids: readonly string[]): Photo[] {
    const from = this.clip?.from.id;
    const out: Photo[] = [];
    for (const id of ids) {
      if (id === from) continue;
      const photo = api.photo(id);
      if (photo) out.push(photo);
    }
    return out;
  }

  /** 套用到这些照片（同步、在内存里，按时间片）。完成后底栏写结果和「撤销」。 */
  async apply(api: AppApi, photos: readonly Photo[], range: string): Promise<Outcome[]> {
    const clip = this.clip;
    if (!clip || this.busy) return [];
    this.busy = true;
    const session = api.session();
    const batchId = `p${(batchSeq += 1)}`;
    const targets = photos.filter((photo) => api.photo(photo.id) === photo).map((photo) => targetOf(photo));
    let outcomes: Outcome[] = [];
    try {
      const needsGeom = clip.groups.includes('geometry') || clip.groups.includes('crop');
      const coreApi = needsGeom ? await import('../core/api') : null;
      outcomes = await pasteBatch(targets, clip, clip.groups, envOf(api), this.undo, batchId, {
        alive: () => api.session() === session,
        current: (id) => api.photo(id),
        budgetMs: this.budgetMs,
        ...(coreApi
          ? {
              geom: {
                applyGeometry: async (photo, geometry) => {
                  const reply = await coreApi.geomGeometryWrite(
                    sideSubset({
                      frame: photo.frame ?? null,
                      geometry: photo.geometry ?? zeroGeometry(),
                      crop: photo.crop ?? null,
                    }),
                    geometrySidecar(geometry.straighten),
                  );
                  return {
                    geometry: { straighten: reply.geometry?.straighten ?? 0 },
                    crop: reply.crop,
                  };
                },
                applyCrop: async (photo, geometry, crop, preset, rect) => {
                  const reply = await coreApi.geomCropWrite(
                    sideSubset({ frame: photo.frame ?? null, geometry, crop }),
                    { preset, rect, fit: true },
                  );
                  return {
                    geometry: { straighten: reply.geometry?.straighten ?? 0 },
                    crop: reply.crop,
                  };
                },
              },
            }
          : {}),
      });
    } finally {
      this.busy = false;
    }
    if (api.session() !== session) return outcomes;
    this.lastOutcomes = outcomes;
    const used = outcomes.some((r) => r.applied.length);
    const line = pasteMessage(outcomes);
    const ids = new Set(outcomes.filter((r) => r.applied.length).map((r) => r.id));
    for (const id of ids) invalidateThumbs(api.photo(id));
    if (used) this.setBar({ kind: 'applied', batchId, line, sub: `${range} · ${groupsText(clip.groups)}`, used, ids });
    else this.setBar({ kind: 'info', line });
    this.edited(ids);
    return outcomes;
  }

  /** 「撤销」：这一批每张还原成套用之前；套用之后又改过的不动，并说明。 */
  revert(api: AppApi): string | null {
    const bar = this.bar;
    if (bar?.kind !== 'applied') return null;
    const entries = this.undo.take(bar.batchId);
    const res = entries ? undoBatch(entries, (id) => api.photo(id)) : { counts: { restored: 0, changed: 0, error: 0 }, results: [] };
    const line = undoMessage(res);
    this.setBar({ kind: 'info', line });
    const restoredIds = new Set(res.results.filter((r) => r.status === 'restored').map((r) => r.id));
    for (const id of restoredIds) invalidateThumbs(api.photo(id));
    this.edited(restoredIds);
    return line;
  }
}

/** 整个页面一份。 */
export const pasteState = new PasteState();

/** 只在开发服务器里：测试读每张照片的编辑状态（配方、各配方的档位、调整、几何、裁剪、rev）。 */
function exposeEdits(api: AppApi): void {
  const w = window as unknown as { __gxrEdits?: () => unknown; __gxrPaste?: PasteState; __gxrRequest?: (stem: string) => Promise<unknown> };
  w.__gxrPaste = pasteState;
  // 这张照片导出时交给核心的请求（ui-m3 的 buildRequest，和点「存到相册」时同一份输入）。动态 import：Node 测试不加载导出界面。
  w.__gxrRequest = async (stem) => {
    const photo = api.photos().find((item) => item.stem === stem);
    if (!photo) return null;
    const { ensureCameraRecipe } = await import('./model');
    await ensureCameraRecipe(photo, api.book());
    const { requestPhoto, cardsFor } = await import('./export_ui');
    const { buildRequest } = await import('../lib/export_request');
    return buildRequest(requestPhoto(photo, api.lutBook(), api.book()), cardsFor(api.book(), photo), { size: 'original', quality: 'high' }, 'dev');
  };
  w.__gxrEdits = () =>
    api.photos().map((photo) => ({
      stem: photo.stem,
      rev: photo.rev,
      choice: photo.choice,
      levels: Object.fromEntries(photo.levels),
      memory: Object.fromEntries(photo.memory),
      lut: photo.lut,
      knobs: { ...photo.knobs },
      curve: curveJson(photo.curve),
      frame: photo.frame,
      geometry: { ...photo.geometry },
      crop: cloneCrop(photo.crop),
      border: { ...photo.border },
      stamp: { ...photo.stamp },
      ms: photoMs(photo),
    }));
}
