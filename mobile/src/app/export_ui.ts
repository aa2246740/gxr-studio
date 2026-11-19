// 导出进相册：编辑器单点「存到相册」按记住的选项立即开始导出（不先弹选项表）；「导出选项」从 ⋯ 或 U2「更多」打开。
// 做好后在底栏上方弹出独立就绪条（「第 N 张做好了 · 存进相册」/「N 张做好了 · 存进相册」）；再次导出不丢未存成片（队列 + 暂存）。
// 系统共享面板关闭后自动收起就绪条，不再留一层「好」。

import { ExportCore, calibPack, type RunResult } from '../core/export_api';
import { unitOfLens } from '../lib/calib';
import { mergeMeta } from '../lib/exif';
import { buildRequest, usedLogoId, type QualityTier, type RequestPhoto, type SizeTier } from '../lib/export_request';
import { fontsNeeded, type FontKey } from '../lib/fonts';
import { HDR_PROGRESS, hdrAvailable } from '../lib/hdr_text';
import { loadExportHdr, loadExportQuality, loadExportSize } from '../lib/prefs';
import { adoptedSelectionOf, cameraRecipeErrorOf, photoWithAdopted, recipeUnitOf, shelf, shelves, type Card } from '../lib/recipes';
import { shareChunks, SHARE_MAX_BYTES, SHARE_MAX_FILES } from '../lib/share_plan';
import type { Book } from './book';
import { button, el } from './dom';
import { ExportBatch, STAGE_CAP_BYTES, STAGE_CAP_HDR_BYTES, type BatchHost, type BatchItem } from './export_batch';
import { estimatePeak, type BatchFonts } from './export_budget';
import { mountExportSheet, type OpenedExportSheet } from './export_options';
import { setExportPaused } from './export_pause';
import {
  EXPORT_ICON,
  exportIco,
  paintReadyBar,
  readyPositionsSummary,
  type ReadyEntry,
} from './export_ready';
import { cachedFontPack, ensureFontPack, replaceFontBytesForDev } from './font_loader';
import { logoStore } from './logo_store';
import type { LutBook } from './lut_book';
import { ensureCameraRecipes, needsCameraRecipe, type Photo } from './model';
import './export.css';

declare const __GXR_BUILD__: string;

/** 导出这边要的 App 接口。 */
export interface ExportApp {
  photos(): Photo[];
  photo(id: string): Photo | undefined;
  session(): number;
  book(): Book;
  lutBook?(): LutBook;
  markGone(id: string, version: number): void;
}

/** 只在开发服务器里：测试改 binding、分批上限、在第几张模拟陷阱，读批次和内存。生产包里没有这段。 */
interface DevExport {
  binding?: string;
  chunkFiles?: number;
  trapAt?: number;
  inkTrapAt?: number;
  canvasReportPrefix?: string;
  /** 去 hint 的第二次导出：exact 时也把画布交出来。 */
  forceCanvas?: boolean;
  installFonts?: (entries: Array<[FontKey, Uint8Array]>) => void;
  flow?: ExportFlow;
  core?: ExportCore;
}

function dev(): DevExport | null {
  if (!import.meta.env.DEV) return null;
  const scope = window as unknown as { __devExport?: DevExport };
  scope.__devExport ??= {};
  const hook = scope.__devExport;
  hook.installFonts ??= (entries) => replaceFontBytesForDev(entries);
  return hook;
}

export function cardsFor(book: Book, photo: Photo): Card[] {
  const dat = book.dat;
  if (!dat) return [];
  const adopted = adoptedSelectionOf(photo, dat);
  const unit = recipeUnitOf(photo.choice, adopted, unitOfLens(mergeMeta(photo.jpgMeta, photo.dngMeta).lens));
  return shelf(shelves(dat.listing, unit), 'all');
}

export function requestPhoto(photo: Photo, lutBook?: LutBook, book?: Book): RequestPhoto {
  const view = book ? photoWithAdopted(photo, book.dat) : photo;
  const meta = mergeMeta(photo.jpgMeta, photo.dngMeta);
  const source = photo.jpg ?? photo.dng;
  const lutRecord = photo.lut ? lutBook?.get(photo.lut) : undefined;
  const border = photo.border ? { ...photo.border } : null;
  const stamp = photo.stamp ? { ...photo.stamp } : null;
  const logoId = usedLogoId(border);
  return {
    stem: photo.stem,
    dngName: photo.dng?.name ?? null,
    jpgName: photo.jpg?.name ?? null,
    lens: meta.lens,
    iso: meta.iso,
    choice: view.choice ? { ...view.choice } : null,
    levels: new Map([...view.levels].map(([k, v]) => [k, { ...v }])),
    memory: new Map([...photo.memory].map(([k, v]) => [k, { ...v }])),
    lut: photo.lut,
    lutInputSpace: lutRecord?.input_space ?? null,
    knobs: { ...photo.knobs },
    curve: photo.curve ?? null,
    frame: photo.frame ? { ...photo.frame } : null,
    geometry: photo.geometry ? { ...photo.geometry } : undefined,
    crop: photo.crop
      ? {
          preset: photo.crop.preset,
          rect: [photo.crop.rect[0], photo.crop.rect[1], photo.crop.rect[2], photo.crop.rect[3]],
          angle_deg: photo.crop.angle_deg,
        }
      : null,
    border,
    stamp,
    logoInfo: logoId ? logoStore.info(logoId) : null,
    sourceModified: source?.lastModified ?? photo.lastModified,
  };
}

interface ExportOptions {
  size: SizeTier;
  quality: QualityTier;
  hdr: boolean;
}

interface QueuedJob {
  photos: Photo[];
  options: ExportOptions;
}

type View = 'progress' | 'noshare';

const flowByShell = new WeakMap<HTMLElement, ExportFlow>();

/** 打开「导出选项」面板：供编辑器 ⋯ 与 U2「更多」调用，不改 boot.ts / model.ts 接口。 */
export function openExportOptions(shell: HTMLElement, ids: string[], onStart?: () => void): boolean {
  const flow = flowByShell.get(shell);
  if (!flow) return false;
  return flow.openOptions(ids, onStart);
}

/** 用记住的尺寸、品质与 HDR 立即开始导出：供编辑器单点「存到相册」调用。 */
export function startQuickExport(shell: HTMLElement, ids: string[], onStart?: () => void): boolean {
  const flow = flowByShell.get(shell);
  if (!flow) return false;
  return flow.startQuick(ids, onStart);
}

export class ExportFlow {
  readonly core = new ExportCore();
  private batch: ExportBatch | null = null;
  private dock: HTMLElement | null = null;
  private sheet: OpenedExportSheet | null = null;
  private view: View = 'progress';
  private detailsOpen = false;
  private chunk = 0;
  private chunks: number[][] = [];
  private chunkEntryCount = 0;
  private wake: { release(): Promise<void> } | null = null;
  /** 前一批已做好、还没存进相册的成片：再次导出时保留在这里，和后续成片一并显示与保存。 */
  private carryover: ReadyEntry[] = [];
  /** 当前批次还在跑时新发起的导出请求，排在这里顺序执行。 */
  private pendingQueue: QueuedJob[] = [];
  /** 下载兜底的对象 URL 全记在这里，重画、交出、清空、新的一批和批次结束时统一撤销（Sol r1 P2-03）。 */
  private readonly urls = new Set<string>();

  constructor(
    private readonly shell: HTMLElement,
    private readonly app: ExportApp,
    private readonly perf: HTMLElement | null,
  ) {
    flowByShell.set(shell, this);
    const hooks = dev();
    if (hooks) {
      hooks.flow = this;
      hooks.core = this.core;
    }
  }

  current(): ExportBatch | null {
    return this.batch;
  }

  /** 图库多选栏「导出 N 张」：打开多选导出面板。`ids` 按网格顺序排好。 */
  open(ids: string[], onStart?: () => void): void {
    this.openSheet(ids, { onStart });
  }

  /** 编辑器「⋯ → 导出选项」与 U2「更多」：打开「导出选项」面板。 */
  openOptions(ids: string[], onStart?: () => void): boolean {
    return this.openSheet(ids, { title: '导出选项', onStart });
  }

  /** 编辑器单点「存到相册」：直接按记住的选项开始导出，不先弹导出选项表。 */
  startQuick(ids: string[], onStart?: () => void): boolean {
    const photos = this.orderedPhotos(ids);
    if (!photos.length) return false;
    this.closeSheet();
    onStart?.();
    const hdr = hdrAvailable(photos) && loadExportHdr();
    this.start(photos, { size: loadExportSize(), quality: loadExportQuality(), hdr });
    return true;
  }

  /** 清空照片：这一批和排队任务全部停下，待存成片全丢，晚到的结果不会再到共享这一步。 */
  clear(): void {
    this.closeSheet();
    this.pendingQueue = [];
    this.carryover = [];
    this.detailsOpen = false;
    this.chunks = [];
    this.chunk = 0;
    this.chunkEntryCount = 0;
    this.batch?.cancel();
    this.batch = null;
    this.removeDock();
  }

  /** 现在还没撤销的对象 URL 有几个（开发服务器的测试读）。 */
  liveUrls(): number {
    return this.urls.size;
  }

  private orderedPhotos(ids: string[]): Photo[] {
    const want = new Set(ids);
    const ordered = this.app.photos().filter((p) => want.has(p.id));
    if (ordered.length) return ordered;
    return ids.map((id) => this.app.photo(id)).filter((p): p is Photo => !!p);
  }

  private openSheet(ids: string[], opts: { title?: string; onStart?: () => void }): boolean {
    const photos = this.orderedPhotos(ids);
    if (!photos.length) return false;
    this.closeSheet();
    this.sheet = mountExportSheet(
      {
        shell: this.shell,
        core: this.core,
        book: () => this.app.book(),
        lutBook: () => this.app.lutBook?.(),
        cardsFor,
        requestPhoto,
        isBatchClosed: () => !this.batch || this.batch.phase === 'closed',
        onStart: (picked, options) => this.start(picked, options),
        onClose: () => this.closeSheet(),
      },
      photos,
      opts,
    );
    return true;
  }

  private revokeUrls(): void {
    for (const url of this.urls) URL.revokeObjectURL(url);
    this.urls.clear();
  }

  private closeSheet(): void {
    if (!this.sheet) return;
    this.sheet.cleanup();
    for (const node of this.sheet.nodes) node.remove();
    this.sheet = null;
  }

  private positionOf(id: string): number | null {
    const index = this.app.photos().findIndex((p) => p.id === id);
    return index >= 0 ? index + 1 : null;
  }

  private carryoverBytes(): number {
    return this.carryover.reduce((sum, item) => sum + item.file.size, 0);
  }

  private batchEntries(batch: ExportBatch): ReadyEntry[] {
    return batch.staged.map((s) => {
      const item = batch.item(s.id);
      return {
        id: s.id,
        stem: item?.stem ?? s.id,
        file: s.file,
        position: this.positionOf(s.id),
        hdrRequested: Boolean(item?.req.hdr),
        result: batch.results.get(s.id),
        ...(item?.cameraRecipeNote ? { cameraRecipeNote: item.cameraRecipeNote } : {}),
      };
    });
  }

  private allReadyEntries(batch: ExportBatch | null): ReadyEntry[] {
    const carried = this.carryover.map((entry) => ({
      ...entry,
      position: this.positionOf(entry.id) ?? entry.position,
    }));
    const current = batch && batch.phase !== 'closed' ? this.batchEntries(batch) : [];
    return [...carried, ...current];
  }

  /** 把已完成、未存进相册的成片收进 carryover，防止被新导出丢掉。 */
  private preserveBatch(batch: ExportBatch): void {
    for (const entry of this.batchEntries(batch)) this.carryover.push(entry);
    batch.staged = [];
    batch.stagedBytes = 0;
  }

  /** 开始或排队一批导出：不丢掉之前做好但还没存进相册的成片。 */
  private start(photos: Photo[], options: ExportOptions): void {
    this.revokeUrls();
    this.view = 'progress';
    const current = this.batch;
    if (current && (current.phase === 'running' || current.phase === 'held')) {
      this.pendingQueue.push({ photos, options });
      this.paint();
      return;
    }
    if (current && current.phase !== 'closed') {
      this.preserveBatch(current);
      // 取消旧批次前清空引用，避免旧批次的 changed 回调误清刚建的 dock。
      this.batch = null;
      current.cancel();
    }
    this.launch(photos, options);
  }

  private launch(photos: Photo[], options: ExportOptions): void {
    this.revokeUrls();
    const hooks = dev();
    const binding = hooks?.binding;
    const book = this.app.book();
    const dat = book.dat;
    const lutBook = this.app.lutBook?.();
    const snapshots = photos.map((photo) => ({
      id: photo.id,
      stem: photo.stem,
      version: photo.version,
      dng: photo.dng,
      jpg: photo.jpg,
      explicitChoice: photo.choice !== null,
      recipeOff: Boolean(photo.recipeOff),
      reqPhoto: requestPhoto(photo, lutBook, book),
      cards: cardsFor(book, photo),
      note: cameraRecipeErrorOf(photo, dat)?.message,
    }));
    const items: BatchItem[] = snapshots.map((s) => ({
      id: s.id,
      stem: s.stem,
      version: s.version,
      dng: s.dng,
      jpg: s.jpg,
      req: buildRequest(s.reqPhoto, s.cards, options, __GXR_BUILD__, binding),
      ...(s.note ? { cameraRecipeNote: s.note } : {}),
    }));
    const cubeIds = [...new Set(snapshots.map((s) => s.reqPhoto.lut).filter((id): id is string => Boolean(id)))];
    const fontKeys = [...new Set(snapshots.flatMap((s) => fontsNeeded(s.reqPhoto.border, s.reqPhoto.stamp)))];
    const logoIds = [...new Set(snapshots.map((s) => usedLogoId(s.reqPhoto.border)).filter((id): id is string => id !== null))];
    const pack = calibPack(book.pack?.files ?? null);
    const datCopy = dat ? dat.read.bytes.slice(0) : null;
    const host: BatchHost = {
      run: (item, index) => this.run(item, index),
      stop: () => this.core.stop(),
      idle: () => this.core.release(),
      session: () => this.app.session(),
      photo: (id) => {
        const p = this.app.photo(id);
        return p ? { version: p.version, dng: p.dng, jpg: p.jpg } : null;
      },
      markGone: (id, version) => this.app.markGone(id, version),
      busy: (on) => this.busy(on),
      changed: () => this.paint(),
      memoryPeak: () => this.core.marks.total,
      peakEstimate: (item) => estimatePeak(this.core, item, options.size, fonts),
      baseStagedBytes: () => this.carryoverBytes(),
    };
    let fonts: BatchFonts = { fontIndex: '', fontPack: null };
    this.view = 'progress';
    this.chunk = 0;
    this.chunks = [];
    this.chunkEntryCount = 0;
    const batch = new ExportBatch(items, host, options.hdr ? STAGE_CAP_HDR_BYTES : STAGE_CAP_BYTES);
    this.batch = batch;
    const begin = (luts: Parameters<ExportCore['begin']>[0]['luts'], f: BatchFonts): void => {
      const packedLogos = logoStore.packLogos(logoIds);
      fonts = f;
      this.core.begin({
        dat: datCopy,
        calibIndex: pack.calibIndex,
        calibBlob: pack.calibBlob,
        luts,
        fontIndex: f.fontIndex,
        fontPack: f.fontPack,
        logoIndex: packedLogos.logoIndex,
        logoBlob: packedLogos.logoBlob,
      });
      batch.start();
    };
    const syncFonts = cachedFontPack(fontKeys);
    const logosReady = !logoIds.length || logoStore.loaded();
    const needAdopt = photos.some((p) => needsCameraRecipe(p, dat, { checkMissingDat: true }));
    if (!needAdopt && (!cubeIds.length || !lutBook) && syncFonts && logosReady) {
      begin({}, syncFonts);
      return;
    }
    this.paint();
    const lutTask = cubeIds.length && lutBook ? lutBook.exportSnapshot(cubeIds).catch(() => ({})) : Promise.resolve({});
    const fontTask = fontKeys.length
      ? ensureFontPack(fontKeys).catch(() => ({ fontIndex: '', fontPack: null }))
      : Promise.resolve({ fontIndex: '', fontPack: null });
    const logoTask = logoIds.length && !logoStore.loaded() ? logoStore.ensureLoaded().catch(() => []) : Promise.resolve([]);
    const adoptTask = needAdopt ? ensureCameraRecipes(photos, book, { checkMissingDat: true }) : Promise.resolve();
    void Promise.all([lutTask, fontTask, logoTask, adoptTask]).then(([luts, fonts0]) => {
      if (this.batch !== batch || batch.phase !== 'running') return;
      if (needAdopt || logoIds.length) {
        photos.forEach((photo, idx) => {
          const snap = snapshots[idx];
          let changed = false;
          const logoId = logoIds.length ? usedLogoId(snap.reqPhoto.border) : null;
          if (logoId) {
            snap.reqPhoto.logoInfo = logoStore.info(logoId);
            changed = true;
          }
          if (needAdopt && !snap.explicitChoice && !snap.recipeOff) {
            const adopted = adoptedSelectionOf(photo, dat);
            if (adopted) {
              snap.reqPhoto.choice = { ...adopted.choice };
              snap.reqPhoto.levels = new Map([[adopted.choice.id, { ...adopted.levels }]]);
              if (dat) snap.cards = shelf(shelves(dat.listing, recipeUnitOf(null, adopted, unitOfLens(snap.reqPhoto.lens))), 'all');
              changed = true;
            }
            const note = cameraRecipeErrorOf(photo, dat)?.message;
            if (note) items[idx].cameraRecipeNote = note;
          }
          if (changed) items[idx].req = buildRequest(snap.reqPhoto, snap.cards, options, __GXR_BUILD__, binding);
        });
      }
      begin(luts, fonts0);
    });
  }

  private run(item: BatchItem, index: number): Promise<RunResult> {
    const d = dev();
    const trap = import.meta.env.DEV && d?.trapAt === index;
    const inkTrap = import.meta.env.DEV && d?.inkTrapAt === index;
    const canvasReport = import.meta.env.DEV && d?.canvasReportPrefix ? `${d.canvasReportPrefix}-${index}` : undefined;
    const forceCanvas = import.meta.env.DEV && d?.forceCanvas === true;
    return this.core.run({
      dng: item.dng,
      jpg: item.jpg,
      req: item.req,
      ...(trap ? { devTrap: true } : {}),
      ...(inkTrap ? { devInkTrap: true } : {}),
      ...(canvasReport ? { devCanvasReport: canvasReport } : {}),
      ...(canvasReport && forceCanvas ? { devForceCanvas: true } : {}),
    });
  }

  private busy(on: boolean): void {
    setExportPaused(on);
    if (on && !this.wake) {
      const lock = (navigator as Navigator & { wakeLock?: { request(type: 'screen'): Promise<{ release(): Promise<void> }> } }).wakeLock;
      lock?.request('screen').then(
        (sentinel) => {
          if (this.batch?.phase === 'running') this.wake = sentinel;
          else void sentinel.release().catch(() => undefined);
        },
        () => undefined,
      );
    } else if (!on && this.wake) {
      void this.wake.release().catch(() => undefined);
      this.wake = null;
    }
  }

  private removeDock(): void {
    this.revokeUrls();
    this.dock?.remove();
    this.dock = null;
    this.shell.removeAttribute('data-export');
  }

  /** 独立浮在底栏上方的导出进度 / 就绪条，按批次与暂存队列状态重画。 */
  private paint(): void {
    const batch = this.batch;
    // 当前批次顺利做完且还有排队的导出任务：保留刚做好的成片，自动跑下一项。
    if (batch && batch.phase === 'ready' && !batch.failures.size && this.pendingQueue.length) {
      const next = this.pendingQueue.shift();
      if (next) {
        this.preserveBatch(batch);
        this.batch = null;
        batch.cancel();
        this.launch(next.photos, next.options);
        return;
      }
    }
    const batchLive = Boolean(batch && batch.phase !== 'closed');
    if (!batchLive && !this.carryover.length) {
      this.removeDock();
      return;
    }
    if (!this.dock) {
      this.dock = el('div', 'pdock');
      const perf = this.shell.querySelector('.perf');
      this.shell.insertBefore(this.dock, perf);
    }
    const dock = this.dock;
    // 重画会丢掉旧的下载链接：它们的 URL 先撤销，需要的话 paintReadyBar 再建。
    this.revokeUrls();
    dock.replaceChildren();
    const phase = batch && batch.phase !== 'closed' ? batch.phase : 'ready';
    this.shell.dataset.export = phase;
    dock.dataset.phase = phase;
    if (batch) this.paintMemory(batch);
    if (batch && batch.phase === 'running') {
      this.chunks = [];
      this.chunk = 0;
      this.chunkEntryCount = 0;
      this.paintRunning(dock, batch);
      return;
    }
    const entries = this.allReadyEntries(batch);
    if ((!this.chunks.length || this.chunkEntryCount !== entries.length) && entries.length) {
      const limit = dev()?.chunkFiles ?? SHARE_MAX_FILES;
      this.chunks = shareChunks(
        entries.map((e) => e.file.size),
        limit,
        SHARE_MAX_BYTES,
      );
      this.chunk = 0;
      this.chunkEntryCount = entries.length;
    }
    const failures = batch && batch.phase !== 'closed' ? [...batch.failures.values()] : [];
    const showThumbs = batch && batch.phase !== 'closed' && (failures.length > 0 || batch.phase === 'stopped');
    paintReadyBar({
      dock,
      phase,
      entries,
      failures,
      savedCount: batch?.savedCount() ?? 0,
      chunk: this.chunk,
      totalChunks: this.chunks.length,
      detailsOpen: this.detailsOpen,
      noShare: this.view === 'noshare',
      thumbs: showThumbs ? this.thumbs(batch) : null,
      onShare: () => this.share(batch, this.chunkEntries(entries)),
      onRetry: () => {
        if (!batch) return;
        this.chunks = [];
        this.chunk = 0;
        this.chunkEntryCount = 0;
        batch.retry();
      },
      onToggleDetails: () => {
        this.detailsOpen = !this.detailsOpen;
        this.paint();
      },
      onDismiss: () => this.dismiss(batch),
      onFinishNoShare: () => this.finishShare(batch),
      trackUrl: (url) => this.urls.add(url),
    });
  }

  private paintMemory(batch: ExportBatch): void {
    const marks = this.core.marks;
    const mib = (v: number) => Math.round((v / 1048576) * 10) / 10;
    const stagedHigh = Math.max(batch.stagedHigh, batch.stagedBytes + this.carryoverBytes());
    const data = { wasm: marks.wasm, js: marks.js, staged: stagedHigh, workers: marks.workers, times: batch.times };
    this.shell.dataset.exportMem = JSON.stringify(data);
    if (this.perf && !this.perf.hidden) {
      const avg = batch.times.length ? `${Math.round(batch.times.reduce((a, b) => a + b, 0) / batch.times.length)} ms` : '—';
      this.perf.textContent = `导出 WASM ${mib(marks.wasm)} MiB · JS ${mib(marks.js)} MiB · 待存 ${mib(stagedHigh)} MiB · 每张 ${avg}`;
    }
  }

  private thumbs(batch: ExportBatch): HTMLElement {
    const row = el('div', 'pthumbs');
    const { done } = batch.progress();
    const start = Math.max(0, Math.min(done - 2, batch.items.length - 6));
    for (const item of batch.items.slice(start, start + 6)) {
      const state = batch.states.get(item.id) ?? 'wait';
      const t = el('div', `t ${state}`);
      t.dataset.id = item.id;
      const bitmap = this.app.photo(item.id)?.bitmap;
      if (bitmap) {
        const canvas = document.createElement('canvas');
        canvas.width = bitmap.width;
        canvas.height = bitmap.height;
        canvas.getContext('2d')?.drawImage(bitmap, 0, 0);
        t.append(canvas);
      }
      if (state === 'run') t.append(el('span', 'spin'));
      else if (state === 'ok' || state === 'saved' || state === 'fail') {
        if (state === 'saved') t.classList.add('ok');
        const st = el('span', 'st');
        st.append(exportIco(state === 'fail' ? EXPORT_ICON.x : EXPORT_ICON.check));
        t.append(st);
      }
      row.append(t);
    }
    return row;
  }

  private paintRunning(dock: HTMLElement, batch: ExportBatch): void {
    const { done, total, current } = batch.progress();
    const top = el('div', 'top');
    const tx = el('div', 'tx');
    const queued = this.pendingQueue.reduce((sum, job) => sum + job.photos.length, 0);
    const title = queued ? `正在导出 ${current} / ${total}（另排 ${queued} 张）` : `正在导出 ${current} / ${total}`;
    tx.append(el('b', 'num', title), el('small', '', batch.hdr ? HDR_PROGRESS : '可以接着看照片。'));
    if (this.carryover.length) {
      const summary = readyPositionsSummary(this.carryover.map((e) => this.positionOf(e.id) ?? e.position));
      tx.append(el('small', 'ready-carry', `已做好待存：${summary || `${this.carryover.length} 张`}`));
    }
    const stop = button('fbtn fill-ctl', '停止');
    stop.addEventListener('click', () => {
      this.pendingQueue = [];
      batch.stop();
    });
    top.append(el('span', 'spin'), tx, stop);
    const bar = el('div', 'bar');
    const fill = el('i');
    fill.style.width = `${total ? (done / total) * 100 : 0}%`;
    bar.append(fill);
    dock.append(top, bar, this.thumbs(batch));
  }

  private chunkEntries(entries: readonly ReadyEntry[]): ReadyEntry[] {
    const pick = this.chunks[this.chunk] ?? [];
    return pick.map((i) => entries[i]).filter((e): e is ReadyEntry => !!e);
  }

  /** 必须在点击里同步调用，前面不能有 await：共享面板要这次点击的用户激活。 */
  private share(batch: ExportBatch | null, picked: ReadyEntry[]): void {
    const files = picked.map((e) => e.file);
    const nav = navigator as Navigator & { canShare?: (data: ShareData) => boolean };
    const can = typeof nav.share === 'function' && typeof nav.canShare === 'function' && nav.canShare({ files });
    if (!can) {
      this.view = 'noshare';
      this.paint();
      return;
    }
    nav.share({ files }).then(
      () => {
        if (this.batch !== batch) return;
        if (this.chunk + 1 < this.chunks.length) {
          this.chunk += 1;
          this.paint();
          return;
        }
        // 最后一批交出后自动收起，不再多点一次「好」。
        this.finishShare(batch);
      },
      () => {
        // 关掉了面板（AbortError）或别的失败：留在就绪条，文件还在。
      },
    );
  }

  private finishShare(batch: ExportBatch | null): void {
    this.revokeUrls();
    this.view = 'progress';
    this.detailsOpen = false;
    this.chunks = [];
    this.chunk = 0;
    this.chunkEntryCount = 0;
    this.carryover = [];
    if (batch && batch.phase !== 'closed') {
      batch.release();
    }
    this.paint();
  }

  private dismiss(batch: ExportBatch | null): void {
    this.revokeUrls();
    this.view = 'progress';
    this.detailsOpen = false;
    this.chunks = [];
    this.chunk = 0;
    this.chunkEntryCount = 0;
    this.carryover = [];
    if (batch && batch.phase !== 'closed') {
      batch.cancel();
    }
    this.paint();
  }
}
