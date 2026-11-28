// 网格上空闲时先烘焙几张配方表，放进 Book 原来的缓存：编辑器第一次换到默认卡、或者第一屏看得见的卡时，表已经在了，
// 不用再等一次 color_bake（约 0.4–0.7 秒）。烘焙表只看（校准和相机配方文件的版本、单元、ISO 模型、条目 sha256），
// 和照片无关（book.ts 的 bake），所以在网格上就能做。表和编辑器自己做的是同一次调用、同样的参数，像素和导出不变。
//
// 规矩：
// - 只在网格上、显卡引擎在、缩略图都读完、导出没在跑、池子里只有它自己的活（poolLoad）、用户 1.2 秒没碰屏幕时才开始新的；
//   点按、滚动、按键、打开照片、导出都让它先停（已经交给 Worker 的那一张做完，结果照样进缓存）。
// - 同时最多 quietIdleBakeCap(workers) 张（workers - 2：4 个 Worker 两张，3 个一张，两个以下不做），点击总有空的 Worker。
// - 每份（校准、相机配方文件）最多 PREBAKE_MAX 张，表放在 Book 的缓存里（KEEP_BAKES 张，最久没用的先丢），不另占内存。
// - 走后台 Lane（core/pool.ts）：编辑器点了同一张卡就升成前台，同一个 Promise，不重复烘焙。
// - 换了校准或相机配方文件（Book 的版本变了）就按新版本重新排、重新数；清空照片时没开始的全部作废。
// - 编辑器按镜头名认单元。MOUNT 单元的相机 JPG 里没有镜头名，要等打开时读 DNG 才知道；网格上不知道镜头名的照片，先读一次
//   DNG 开头 256 KiB（和读拍摄配方标签同一个 Worker、同样大小），只拿来排预烘焙，不写回照片。
// - 照片里记着相机配方（GXR1 标签）时，编辑器打开就采用它、把横条滚到它那里，所以也先替它解析标签（ensureCameraRecipe，
//   和编辑器打开时同一个调用、同一个结果）。这两件事只给网格上前 PREBAKE_MAX 张 RAW 做，一次一件。

import { modelIso, unitOfLens, type Unit } from '../lib/calib';
import { mergeMeta, photoMetaFromTiff, type PhotoMeta } from '../lib/exif';
import { adoptedSelectionOf, DEFAULT_CARD, recipeUnitOf, selection, selectionKey, shelf } from '../lib/recipes';
import type { AdoptedSelection, Card, Shelves } from '../lib/recipes';
import { poolLoad } from '../core/pool';
import { KEEP_BAKES, type Book } from './book';
import { exportPaused } from './export_pause';
import { coreWorkerCount, quietIdleBakeCap } from './looks';
import { ensureCameraRecipe, needsCameraRecipe, openable, type Photo } from './model';

/** 每份（校准、相机配方文件）最多预烘焙几张：缓存的一半，另一半留给编辑器自己。 */
export const PREBAKE_MAX = KEEP_BAKES / 2;
/** 点按、滚动、按键之后多久不开始新的预烘焙（和配方卡小图的冷却一样）。 */
export const PREBAKE_QUIET_MS = 1200;
/** 导出在跑、池子里有别人的活、单元还在核对时，隔多久再看一次。 */
const RETRY_MS = 250;
/** 候选最多这么多张（包括失败的），失败的同一个版本里不再试。 */
const PLAN_MAX = PREBAKE_MAX * 2;

/** `busy`：还有要做的；`done`：这一轮都做完了（或到上限了）；`idle`：没在网格上，或者这台设备不做。 */
export type PrebakeState = 'idle' | 'busy' | 'done';

export interface PrebakeHost {
  book(): Book;
  /** 网格上的顺序（第一张最可能先打开）。 */
  photos(): Photo[];
  /** 现在能不能开始新的：在网格上、显卡引擎在、缩略图都读完。变成 true 时调 wake()。 */
  ready(): boolean;
  /** 编辑器配方横条第一屏大约看得见几张卡（按版式）。 */
  firstRow(): number;
  /** DNG 开头 256 KiB（拿镜头名）。不给就不读，不知道镜头名的照片按编辑器的默认单元排。 */
  readHead?(file: File): Promise<ArrayBuffer | null>;
  /** 状态变了（写到 `.app[data-prebake]`，给测试和测速看；界面上不显示）。 */
  report?(state: PrebakeState): void;
}

interface Want {
  card: Card;
  unit: Unit;
  iso: number | null;
  levels: Record<string, number> | undefined;
  /** （单元、ISO 模型、选择）：和 Book.bakeKey 的缓存键同一个口径。 */
  id: string;
}

function photoKey(photo: Photo): string {
  return `${photo.id}\u0000${photo.version}`;
}

/** 编辑器里这张卡用的档位：照 RecipeView.levelsOf（选中的、相机里采用的、默认卡、切走时记下的）。 */
function levelsFor(photo: Photo, card: Card, adopted: AdoptedSelection | null): Record<string, number> | undefined {
  const choice = photo.choice;
  if (choice?.id === card.id) return photo.levels.get(card.id);
  if (!choice && adopted?.choice.id === card.id) return adopted.levels;
  if (!choice && !adopted && card.id === DEFAULT_CARD) return undefined;
  return photo.memory.get(card.id);
}

/** 编辑器打开这张时舞台用的卡：照 RecipeView.target（选中的、相机里采用的，都没有就是默认卡）。`active`：横条会把它滚到中间。 */
function openingCard(photo: Photo, list: Shelves, unit: Unit, adopted: AdoptedSelection | null): { card: Card; active: boolean } | null {
  const all = shelf(list, 'all');
  const choice = photo.choice;
  const chosen = choice && choice.unit === unit ? all.find((card) => card.id === choice.id) : undefined;
  if (chosen) return { card: chosen, active: true };
  const taken = adopted && adopted.choice.unit === unit ? all.find((card) => card.id === adopted.choice.id) : undefined;
  if (taken) return { card: taken, active: true };
  const fallback = list.builtin.find((card) => card.id === DEFAULT_CARD);
  return fallback ? { card: fallback, active: false } : null;
}

/** 第一屏看得见的卡：没有选中的卡时是最前面几张；有的话横条把它滚到中间，取它两边的。 */
function visibleCards(all: Card[], opening: { card: Card; active: boolean } | null, count: number): Card[] {
  const n = Math.max(0, Math.floor(count));
  let from = 0;
  if (opening?.active) {
    const at = all.indexOf(opening.card);
    from = Math.max(0, Math.min(all.length - n, at - Math.floor((n - 1) / 2)));
  }
  return all.slice(from, from + n).filter((card) => card.controls.length > 0);
}

export class Prebaker {
  private readonly host: PrebakeHost;
  private readonly cap: number;
  private readonly load: () => number;
  private generation = -1;
  private plan: Want[] = [];
  private planned = false;
  private readonly tried = new Set<string>();
  /** 这一版本、这一轮里替它解析过相机配方的照片（id 和版本；每张最多一次，解析失败就按默认卡排）。 */
  private readonly recipesAsked = new Set<string>();
  /** 这一轮里读过 DNG 开头的照片（id 和版本）→ 读出的镜头名和 ISO（读不了是 null）。 */
  private readonly heads = new Map<string, PhotoMeta | null>();
  private reading = false;
  /** 这一版本、这一轮里预烘焙成功的张数（算进 PREBAKE_MAX）。 */
  private made = 0;
  /** 交出去还没回来的（不管属于哪一轮，池子里真的占着）。 */
  private running = 0;
  /** cancel() 加一：之前那一轮的任务回来时不再算数。 */
  private round = 0;
  private quietUntil = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private due = 0;
  private state: PrebakeState = 'idle';
  private offBook: (() => void) | null = null;
  private offInput: (() => void) | null = null;

  constructor(host: PrebakeHost, workers: number = coreWorkerCount(), load: () => number = poolLoad) {
    this.host = host;
    this.cap = quietIdleBakeCap(workers);
    this.load = load;
  }

  /** 订阅 Book 的变化；听 `input` 上的点按、滚动和按键（捕获阶段，被动）。 */
  start(input: EventTarget | null): void {
    this.stop();
    this.offBook = this.host.book().onChange(() => this.wake());
    if (input) {
      const onInput = (): void => this.noteInput();
      const opts: AddEventListenerOptions = { capture: true, passive: true };
      const events = ['pointerdown', 'touchstart', 'wheel', 'keydown', 'scroll'];
      for (const name of events) input.addEventListener(name, onInput, opts);
      this.offInput = () => {
        for (const name of events) input.removeEventListener(name, onInput, opts);
      };
    }
    this.wake();
  }

  stop(): void {
    this.offBook?.();
    this.offBook = null;
    this.offInput?.();
    this.offInput = null;
    this.cancel();
  }

  /** 用户动了：这段时间里不开始新的（已经在 Worker 上的照常做完）。 */
  noteInput(ms: number = PREBAKE_QUIET_MS): void {
    this.quietUntil = Math.max(this.quietUntil, Date.now() + ms);
  }

  /** 照片、引擎、屏幕、Book 有变化：下一轮重新排。 */
  wake(): void {
    this.planned = false;
    this.schedule(0);
  }

  /** 清空照片：没开始的作废，计数清零；在路上的那几张做完照常进缓存，但不算这一轮的。 */
  cancel(): void {
    this.round += 1;
    this.plan = [];
    this.planned = false;
    this.tried.clear();
    this.recipesAsked.clear();
    this.heads.clear();
    this.made = 0;
    this.reading = false;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.report('idle');
  }

  private schedule(ms: number): void {
    const at = Date.now() + ms;
    if (this.timer && this.due <= at) return;
    if (this.timer) clearTimeout(this.timer);
    this.due = at;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.pump();
    }, ms);
  }

  private report(state: PrebakeState): void {
    if (state === this.state) return;
    this.state = state;
    this.host.report?.(state);
  }

  private pump(): void {
    if (this.cap <= 0) return;
    const book = this.host.book();
    const generation = book.generationId();
    if (generation !== this.generation) {
      // 换了校准或相机配方文件：旧表 Book 已经丢了，按新版本重新排、重新数。
      this.generation = generation;
      this.tried.clear();
      this.recipesAsked.clear();
      this.made = 0;
      this.planned = false;
    }
    if (!this.host.ready()) return this.report('idle');
    if (this.reading) return this.report('busy'); // 解析完会 wake()
    const quiet = this.quietUntil - Date.now();
    if (quiet > 0) return this.schedule(quiet + 20);
    // 池子里有别人的活（打开照片、读 DAT、LUT……）或者导出在跑：先不开始新的。
    if (exportPaused() || this.load() > this.running) return this.schedule(RETRY_MS);
    if (!this.planned) {
      const made = this.makePlan(book);
      if (made === null) {
        // 有单元还在核对校准，或者刚开始替一张照片解析相机配方（都在池子里）：做完会通知，这里只是兜底。
        this.report('busy');
        return this.schedule(RETRY_MS);
      }
      this.plan = made;
      this.planned = true;
    }
    while (this.running < this.cap && this.made + this.running < PREBAKE_MAX) {
      const next = this.plan.find((want) => !this.tried.has(want.id) && !this.cached(book, want));
      if (!next) break;
      this.tried.add(next.id);
      this.launch(book, next);
    }
    const left = this.made + this.running < PREBAKE_MAX && this.plan.some((want) => !this.tried.has(want.id) && !this.cached(book, want));
    this.report(this.running > 0 || left ? 'busy' : 'done');
  }

  private cached(book: Book, want: Want): boolean {
    const key = book.bakeKey(want.card, want.unit, want.iso, want.levels);
    return key !== null && book.cached(key);
  }

  private launch(book: Book, want: Want): void {
    const round = this.round;
    const generation = this.generation;
    this.running += 1;
    book
      .bake(want.card, want.unit, want.iso, want.levels, true)
      .then(
        () => {
          if (round === this.round && generation === this.generation) this.made += 1;
        },
        // 换了版本、文件缺了、Worker 坏了：这一张算试过，这个版本里不再试。
        () => undefined,
      )
      .finally(() => {
        this.running -= 1;
        this.schedule(0);
      });
  }

  /**
   * 候选，按先后：第一张照片打开时舞台用的卡、它第一屏看得见的卡、别的照片打开时用的卡、别的（单元、ISO 模型）第一屏的卡。
   * 有单元还在核对校准、或者刚开始解析一张照片的相机配方时返回 null（一次只做一件，别一下子占满 Worker）。
   */
  private makePlan(book: Book): Want[] | null {
    const row = this.host.firstRow();
    const openings: Want[] = [];
    const firstRows = new Map<string, Want[]>();
    let raws = 0;
    for (const photo of this.host.photos()) {
      if (openings.length >= PLAN_MAX) break;
      if (!photo.dng || photo.diptych || !openable(photo)) continue;
      const dat = book.usableDatFor(photo);
      if (!dat) continue;
      raws += 1;
      if (raws <= PREBAKE_MAX && (this.readHead(photo) || this.readRecipe(photo, book))) return null;
      // 和编辑器一样先用相机 JPG 的，没有再用 DNG 的（网格上还没读 DNG 时用刚读的开头）。
      const meta = mergeMeta(photo.jpgMeta, photo.dngMeta ?? this.heads.get(photoKey(photo)) ?? null);
      const adopted = adoptedSelectionOf(photo, dat);
      const lensUnit = unitOfLens(meta.lens);
      // 选择、采用的配方、镜头名都说不出单元（开头没读、读不出）：编辑器读了 DNG 才知道，猜了多半白烘焙，不排。
      if (!photo.choice && !adopted && lensUnit === null) continue;
      const unit = recipeUnitOf(photo.choice, adopted, lensUnit);
      const state = book.unitStateFor(photo, unit).state;
      if (state === 'checking') return null;
      if (state !== 'ready') continue;
      const list = book.shelvesFor(photo, unit);
      if (!list) continue;
      const want = (card: Card): Want => {
        const levels = levelsFor(photo, card, adopted);
        return { card, unit, iso: meta.iso, levels, id: `${unit}|${modelIso(meta.iso)}|${selectionKey(selection(card, unit, levels))}` };
      };
      const opening = openingCard(photo, list, unit, adopted);
      if (opening && opening.card.controls.length) openings.push(want(opening.card));
      const combo = `${unit}|${modelIso(meta.iso)}`;
      if (!firstRows.has(combo)) firstRows.set(combo, visibleCards(shelf(list, 'all'), opening, row).map(want));
    }
    const rows = [...firstRows.values()];
    const ordered = [...openings.slice(0, 1), ...(rows[0] ?? []), ...openings.slice(1), ...rows.slice(1).flat()];
    const seen = new Set<string>();
    const out: Want[] = [];
    for (const want of ordered) {
      if (seen.has(want.id)) continue;
      seen.add(want.id);
      out.push(want);
      if (out.length >= PLAN_MAX) break;
    }
    return out;
  }

  /** 前几张照片打开时会采用相机里记的配方：现在替它解析（要读 DNG 开头时 ensureCameraRecipe 自己读），开始了返回 true。 */
  private readRecipe(photo: Photo, book: Book): boolean {
    const asked = photoKey(photo);
    if (this.recipesAsked.has(asked) || !needsCameraRecipe(photo, book.dat)) return false;
    this.recipesAsked.add(asked);
    this.begin(ensureCameraRecipe(photo, book));
    return true;
  }

  /** 网格上不知道这张的镜头名（相机 JPG 里没有、DNG 还没读）：读 DNG 开头，开始了返回 true。 */
  private readHead(photo: Photo): boolean {
    const file = photo.dng;
    const key = photoKey(photo);
    if (!this.host.readHead || !file || photo.dngMeta !== null || photo.jpgMeta?.lens || this.heads.has(key)) return false;
    this.heads.set(key, null);
    const round = this.round;
    this.begin(
      this.host.readHead(file).then((bytes) => {
        if (round === this.round && bytes && photo.dng === file) this.heads.set(key, photoMetaFromTiff(new Uint8Array(bytes)));
      }),
    );
    return true;
  }

  /** 一次只做一件（解析标签、读开头）：做完（成功失败都一样）再排。 */
  private begin(job: Promise<unknown>): void {
    this.reading = true;
    const round = this.round;
    const done = (): void => {
      if (round !== this.round) return;
      this.reading = false;
      this.wake();
    };
    job.then(done, done);
  }
}
