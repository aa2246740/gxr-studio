// 导出进相册：编辑器单点「存到相册」按记住的选项立即开始导出（不先弹选项表）；「导出选项」从 ⋯ 或 U2「更多」打开。
// 做好后在底栏上方弹出独立就绪条（「第 N 张做好了 · 存进相册」/「N 张做好了 · 存进相册」）；再次导出不丢未存成片（队列 + 暂存）。
// 系统共享面板关闭后自动收起就绪条，不再留一层「好」。

import { ExportCore, calibPack, type RunResult } from '../core/export_api';
import { usedLogoId } from '../lib/export_request';
import { fontsNeeded, type FontKey } from '../lib/fonts';
import { hdrAvailable } from '../lib/hdr_text';
import { loadExportHalfframe, loadExportHdr, loadExportQuality, loadExportSize } from '../lib/prefs';
import { cameraRecipeErrorOf } from '../lib/recipes';
import { shareChunks, SHARE_MAX_BYTES, SHARE_MAX_FILES } from '../lib/share_plan';
import type { Book } from './book';
import { el } from './dom';
import { ExportBatch, STAGE_CAP_BYTES, STAGE_CAP_HDR_BYTES, type BatchHost, type BatchItem } from './export_batch';
import { estimatePeak, type BatchFonts } from './export_budget';
import { mountExportSheet, type OpenedExportSheet } from './export_options';
import { setExportPaused } from './export_pause';
import { calibGate } from './export_calib_wait';
import { basePhotoId, ExportQueueState, type ExportOptions } from './export_queue';
import {
  mountExportDock,
  paintExportMemory,
  paintReadyBar,
  paintRunningSlot,
  stateAnnouncement,
  type ReadyEntry,
} from './export_ready';
import {
  applyAdoptedAndLogos,
  applyHalfframeFirstNeighbors,
  buildBatchItems,
  buildExportThumbs,
  cardsFor,
  requestPhoto,
  resolveHalfframeFirstNeighbors,
} from './export_request_prep';
import { ExportDetailsSheet } from './export_sheet';
import { cachedFontPack, ensureFontPack, replaceFontBytesForDev } from './font_loader';
import { logoStore } from './logo_store';
import type { LutBook } from './lut_book';
import { ensureCameraRecipes, needsCameraRecipe, type Photo } from './model';
import './export.css';

export { cardsFor, requestPhoto };

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

/** 切屏、换照片或展开/收起单带时同步导出条挂载位（单带顶 44pt 槽 vs 收起态/网格态贴底 40pt 条）。 */
export function syncExportDock(shell: HTMLElement): void {
  flowByShell.get(shell)?.syncHost();
}

export class ExportFlow {
  readonly core = new ExportCore();
  private batch: ExportBatch | null = null;
  private dock: HTMLElement | null = null;
  private slotBand: HTMLElement | null = null;
  private sheet: OpenedExportSheet | null = null;
  private readonly detailsSheet: ExportDetailsSheet;
  private liveRegion: HTMLElement | null = null;
  private lastAnnouncedKey = '';
  private view: View = 'progress';
  private detailsOpen = false;
  private chunk = 0;
  private chunks: number[][] = [];
  private chunkEntryCount = 0;
  private wake: { release(): Promise<void> } | null = null;
  /** 队列、待存成片与失败保留状态机（供双击去重、失败后续跑、分批不重发与运行中保存共用）。 */
  private readonly queue = new ExportQueueState<Photo>();
  /** 下载兜底的对象 URL 全记在这里，重画、交出、清空、新的一批和批次结束时统一撤销（Sol r1 P2-03）。 */
  private readonly urls = new Set<string>();
  /** 默认校准还在路上时，开始导出前先等它落定（最多几秒，见 export_calib_wait.ts）。 */
  private readonly whenCalib = calibGate(() => this.app.book(), () => this.app.session());

  constructor(
    private readonly shell: HTMLElement,
    private readonly app: ExportApp,
    private readonly perf: HTMLElement | null,
  ) {
    flowByShell.set(shell, this);
    this.detailsSheet = new ExportDetailsSheet(shell);
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

  /** 编辑器单点「存到相册」：直接按记住的选项开始导出，不先弹导出选项表；已在导出/排队/待存中的照片不重复发起。 */
  startQuick(ids: string[], onStart?: () => void): boolean {
    if (!this.orderedPhotos(ids).length) return false;
    this.closeSheet();
    this.whenCalib(() => {
      const photos = this.queue.filterQuickPhotos(this.orderedPhotos(ids), this.batch);
      if (!photos.length) {
        this.paint();
        return;
      }
      onStart?.();
      const hdr = hdrAvailable(photos) && loadExportHdr();
      const halfframe = photos.some((p) => Boolean(p.diptych)) ? loadExportHalfframe() : 'diptych';
      this.start(photos, { size: loadExportSize(), quality: loadExportQuality(), hdr, halfframe });
    });
    return true;
  }

  /** 清空照片：这一批和排队任务全部停下，待存成片与失败全丢，晚到的结果不会再到共享这一步。 */
  clear(): void {
    this.closeSheet();
    this.detailsSheet.close(false);
    this.queue.clear();
    this.detailsOpen = false;
    this.lastAnnouncedKey = '';
    this.chunks = [];
    this.chunk = 0;
    this.chunkEntryCount = 0;
    this.batch?.cancel();
    this.batch = null;
    this.removeDock();
  }

  /** 供编辑器/启动器在切屏、换图或展开/收起单带后重挂导出条。 */
  syncHost(): void {
    if (!this.dock) {
      this.clearSlotFlag();
      return;
    }
    this.mountDock();
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
        onStart: (picked, options) => this.whenCalib(() => this.start(picked, options)),
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
    const base = basePhotoId(id);
    const index = this.app.photos().findIndex((p) => p.id === base);
    return index >= 0 ? index + 1 : null;
  }

  private carryoverEntries(): ReadyEntry[] {
    return this.queue.carryover.map((entry) => ({
      ...entry,
      position: this.positionOf(entry.id) ?? entry.position,
    }));
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
    const carried = this.carryoverEntries();
    const current = batch && batch.phase !== 'closed' ? this.batchEntries(batch) : [];
    return this.queue.readyEntries(carried, current);
  }

  /** 开始或排队一批导出：保留未共享的成片与失败记录，不丢结果。 */
  private start(photos: Photo[], options: ExportOptions): void {
    this.revokeUrls();
    this.view = 'progress';
    const current = this.batch;
    const action = this.queue.enqueueOrPrepareStart(photos, options, current, (b) => this.batchEntries(b as ExportBatch));
    if (action === 'queued') {
      this.paint();
      return;
    }
    if (current && current.phase !== 'closed') {
      // 取消旧批次前清空引用，避免旧批次的 changed 回调误清刚建的 dock。
      this.batch = null;
      current.cancel();
    }
    this.launch(photos, options);
  }

  private launch(photos: Photo[], options: ExportOptions, onlyIds?: readonly string[]): void {
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
      diptych: Boolean(photo.diptych),
      dng: photo.dng,
      jpg: photo.jpg,
      explicitChoice: photo.choice !== null,
      recipeOff: Boolean(photo.recipeOff),
      reqPhoto: requestPhoto(photo, lutBook, book),
      cards: cardsFor(book, photo),
      note: cameraRecipeErrorOf(photo, dat)?.message,
    }));
    const items = buildBatchItems(snapshots, options, __GXR_BUILD__, binding, onlyIds);
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
      baseStagedBytes: () => this.queue.carryoverBytes(),
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
    const needNeighbors = items.some((it) => it.halfframeRole === 'first');
    if (!needAdopt && !needNeighbors && (!cubeIds.length || !lutBook) && syncFonts && logosReady) {
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
    const neighborTask = needNeighbors ? resolveHalfframeFirstNeighbors(photos, this.app.photos(), items) : Promise.resolve(new Map());
    void Promise.all([lutTask, fontTask, logoTask, adoptTask, neighborTask]).then(([luts, fonts0, , , neighbors]) => {
      if (this.batch !== batch || batch.phase !== 'running') return;
      if (needAdopt || logoIds.length) {
        applyAdoptedAndLogos(photos, snapshots, items, dat, needAdopt, logoIds.length > 0, options, __GXR_BUILD__, binding);
      }
      if (neighbors.size) applyHalfframeFirstNeighbors(snapshots, items, neighbors, options, __GXR_BUILD__, binding);
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
      ...(item.halfframeSource ? { halfframeSource: item.halfframeSource } : {}),
      ...(item.stripCopyright ? { stripCopyright: true } : {}),
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

  private clearSlotFlag(): void {
    if (this.slotBand) {
      this.slotBand.removeAttribute('data-export-slot');
      this.slotBand = null;
    }
  }

  private mountDock(): void {
    if (!this.dock) return;
    this.slotBand = mountExportDock(this.shell, this.dock, this.slotBand);
  }

  private ensureLiveRegion(): HTMLElement {
    if (!this.liveRegion || !this.liveRegion.isConnected) {
      const region = el('div', 'sr-only');
      region.setAttribute('aria-live', 'polite');
      region.setAttribute('aria-atomic', 'true');
      region.dataset.role = 'export-announcer';
      this.shell.append(region);
      this.liveRegion = region;
    }
    return this.liveRegion;
  }

  private announce(text: string | null, key: string): void {
    if (!text || key === this.lastAnnouncedKey) return;
    this.lastAnnouncedKey = key;
    const region = this.ensureLiveRegion();
    region.textContent = '';
    region.textContent = text;
  }

  private removeDock(): void {
    this.revokeUrls();
    this.detailsSheet.close(false);
    this.detailsOpen = false;
    this.lastAnnouncedKey = '';
    if (this.liveRegion) this.liveRegion.textContent = '';
    this.clearSlotFlag();
    this.dock?.remove();
    this.dock = null;
    this.shell.removeAttribute('data-export');
  }

  /** 44pt 顶槽 / 40pt 贴底导出条与「详情」底部工作表重画。 */
  private paint(): void {
    const batch = this.batch;
    // 当前批次进入任意非运行、非 held 状态（含失败后 ready / closed / stopped）且还有排队任务：保留未共享成片与失败，自动跑下一项。
    const next = this.queue.takeNextQueued(batch, (b) => this.batchEntries(b as ExportBatch));
    if (next) {
      if (batch && batch.phase !== 'closed') {
        this.batch = null;
        batch.cancel();
      }
      this.launch(next.photos, next.options, next.onlyIds);
      return;
    }
    const batchLive = Boolean(batch && batch.phase !== 'closed');
    if (!batchLive && !this.queue.carryover.length && !this.queue.carriedFailures.size) {
      this.removeDock();
      return;
    }
    if (!this.dock) {
      this.dock = el('div', 'pdock');
      this.dock.setAttribute('role', 'region');
      this.dock.setAttribute('aria-label', '导出状态');
    }
    this.ensureLiveRegion();
    this.mountDock();
    const dock = this.dock;
    const activeEl = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dockFocusAct = !this.detailsOpen && activeEl && dock.contains(activeEl) ? (activeEl.dataset.act ?? null) : null;
    // 重画会丢掉旧的下载链接：它们的 URL 先撤销，需要的话导出详情表再建。
    this.revokeUrls();
    dock.className = 'pdock';
    dock.replaceChildren();
    const phase = batch && batch.phase !== 'closed' ? batch.phase : 'ready';
    this.shell.dataset.export = phase;
    dock.dataset.phase = phase;
    if (batch) this.paintMemory(batch);

    const restoreDockOpener = () => {
      const opener =
        this.dock?.querySelector<HTMLElement>('[data-act="toggle-details"]') ??
        this.dock?.querySelector<HTMLElement>('button');
      opener?.focus({ preventScroll: true });
    };
    const restoreDockFocus = () => {
      if (!dockFocusAct || this.detailsOpen || dock.contains(document.activeElement)) return;
      dock.querySelector<HTMLElement>(`[data-act="${dockFocusAct}"]`)?.focus({ preventScroll: true });
    };
    const toggleDetails = () => {
      const wasOpen = this.detailsOpen;
      this.detailsOpen = !this.detailsOpen;
      if (!this.detailsOpen && this.view === 'noshare') this.view = 'progress';
      this.paint();
      if (wasOpen && !this.detailsOpen) restoreDockOpener();
    };
    const closeDetails = (restoreFocus = true) => {
      if (!this.detailsOpen && this.view !== 'noshare') return;
      this.detailsOpen = false;
      if (this.view === 'noshare') {
        this.view = 'progress';
        this.revokeUrls();
      }
      this.paint();
      if (restoreFocus) restoreDockOpener();
    };

    if (batch && batch.phase === 'running') {
      this.chunks = [];
      this.chunk = 0;
      this.chunkEntryCount = 0;
      const { done, total, current } = batch.progress();
      const carried = this.carryoverEntries();
      const limit = dev()?.chunkFiles ?? SHARE_MAX_FILES;
      const carryChunks = carried.length
        ? shareChunks(
            carried.map((e) => e.file.size),
            limit,
            SHARE_MAX_BYTES,
          )
        : [];
      const picked = (carryChunks[0] ?? []).map((i) => carried[i]).filter((e): e is ReadyEntry => !!e);
      const stopNow = () => {
        this.queue.stop();
        batch.stop();
      };
      paintRunningSlot({
        dock,
        done,
        total,
        current,
        hdr: batch.hdr,
        queued: this.queue.queuedCount(),
        carried,
        carryChunk: 0,
        carryTotalChunks: carryChunks.length,
        detailsOpen: this.detailsOpen,
        onShareCarryover: () => this.shareCarryover(picked),
        onStop: stopNow,
        onToggleDetails: toggleDetails,
      });
      if (this.detailsOpen) {
        this.detailsSheet.render({
          phase,
          running: { done, total, current, hdr: batch.hdr, queued: this.queue.queuedCount() },
          entries: carried,
          failures: this.queue.allFailures(batch),
          savedCount: this.queue.savedCount(batch),
          failurePosition: (id) => this.positionOf(id),
          chunk: 0,
          totalChunks: carryChunks.length,
          noShare: this.view === 'noshare',
          thumbs: this.thumbs(batch),
          onShare: () => this.shareCarryover(picked),
          onStop: stopNow,
          onRetry: () => this.retry(batch),
          onClearFailures: () => this.clearFailures(batch),
          onFinishNoShare: () => {
            this.view = 'progress';
            this.detailsOpen = false;
            this.queue.consumeCarryover(picked.map((e) => e.id));
            this.paint();
          },
          onClose: closeDetails,
          trackUrl: (url) => this.urls.add(url),
        });
      } else {
        this.detailsSheet.close(false);
        restoreDockFocus();
      }
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
    const failures = this.queue.allFailures(batch);
    const ann = stateAnnouncement(phase, entries, failures, (id) => this.positionOf(id));
    if (ann) this.announce(ann.text, ann.key);

    const onShare = () => this.share(batch, this.chunkEntries(entries));
    paintReadyBar({
      dock,
      phase,
      entries,
      failures,
      failurePosition: (id) => this.positionOf(id),
      chunk: this.chunk,
      totalChunks: this.chunks.length,
      detailsOpen: this.detailsOpen,
      onShare,
      onRetry: () => this.retry(batch),
      onToggleDetails: toggleDetails,
      onDismiss: () => this.dismiss(batch),
    });

    const showSheet = this.detailsOpen || (this.view === 'noshare' && entries.length > 0);
    if (showSheet) {
      const showThumbs = batch && batch.phase !== 'closed' && (failures.length > 0 || batch.phase === 'stopped');
      this.detailsSheet.render({
        phase,
        running: null,
        entries,
        failures,
        savedCount: this.queue.savedCount(batch),
        failurePosition: (id) => this.positionOf(id),
        chunk: this.chunk,
        totalChunks: this.chunks.length,
        noShare: this.view === 'noshare',
        thumbs: showThumbs ? this.thumbs(batch) : null,
        onShare,
        onStop: () => undefined,
        onRetry: () => this.retry(batch),
        onClearFailures: () => this.clearFailures(batch),
        onFinishNoShare: () => this.finishShare(batch),
        onClose: closeDetails,
        trackUrl: (url) => this.urls.add(url),
      });
    } else {
      this.detailsSheet.close(false);
      restoreDockFocus();
    }
  }

  private retry(batch: ExportBatch | null): void {
    this.detailsOpen = false;
    this.chunks = [];
    this.chunk = 0;
    this.chunkEntryCount = 0;
    const carriedJobs = this.queue.takeCarriedRetryJobs((p) => this.app.photo(p.id) ?? p);
    if (batch && batch.phase !== 'closed' && batch.failures.size > 0) {
      for (const job of carriedJobs) this.queue.pendingQueue.push(job);
      batch.retry();
      return;
    }
    const first = carriedJobs.shift();
    if (!first) return;
    for (const job of carriedJobs) this.queue.pendingQueue.push(job);
    if (batch && (batch.phase === 'running' || batch.phase === 'held')) {
      this.queue.pendingQueue.unshift(first);
      this.paint();
      return;
    }
    if (batch && batch.phase !== 'closed') {
      this.queue.preserveBatch(batch, this.batchEntries(batch));
      this.batch = null;
      batch.cancel();
    }
    this.queue.beginBatch(first.photos, first.options, first.onlyIds);
    this.launch(first.photos, first.options, first.onlyIds);
  }

  private clearFailures(batch: ExportBatch | null): void {
    const entries = this.allReadyEntries(batch);
    if (!entries.length && (!batch || batch.phase !== 'running')) {
      this.dismiss(batch);
      return;
    }
    this.queue.carriedFailures.clear();
    if (batch && batch.phase !== 'closed') batch.failures.clear();
    this.detailsOpen = false;
    this.paint();
  }

  private paintMemory(batch: ExportBatch): void {
    const stagedHigh = Math.max(batch.stagedHigh, batch.stagedBytes + this.queue.carryoverBytes());
    paintExportMemory(this.shell, this.perf, this.core.marks, stagedHigh, batch.times);
  }

  private thumbs(batch: ExportBatch): HTMLElement {
    return buildExportThumbs(batch, (id) => this.app.photo(id));
  }

  private chunkEntries(entries: readonly ReadyEntry[]): ReadyEntry[] {
    const pick = this.chunks[this.chunk] ?? [];
    return pick.map((i) => entries[i]).filter((e): e is ReadyEntry => !!e);
  }

  /** 运行中直接保存已做好待存的 carryover：同步在点击里调用 navigator.share，绝不在前面 await。 */
  private shareCarryover(picked: ReadyEntry[]): void {
    const files = picked.map((e) => e.file);
    const nav = navigator as Navigator & { canShare?: (data: ShareData) => boolean };
    const can = typeof nav.share === 'function' && typeof nav.canShare === 'function' && nav.canShare({ files });
    if (!can) {
      this.view = 'noshare';
      this.detailsOpen = true;
      this.paint();
      return;
    }
    const session = this.app.session();
    const ids = picked.map((e) => e.id);
    nav.share({ files }).then(
      () => {
        if (this.app.session() !== session) return;
        this.view = 'progress';
        this.detailsOpen = false;
        this.queue.consumeCarryover(ids);
        this.chunks = [];
        this.chunk = 0;
        this.chunkEntryCount = 0;
        this.paint();
      },
      () => {
        // 取消共享面板：保留 carryover，仍在栏上。
      },
    );
  }

  /** 必须在点击里同步调用，前面不能有 await：共享面板要这次点击的用户激活。 */
  private share(batch: ExportBatch | null, picked: ReadyEntry[]): void {
    const files = picked.map((e) => e.file);
    const nav = navigator as Navigator & { canShare?: (data: ShareData) => boolean };
    const can = typeof nav.share === 'function' && typeof nav.canShare === 'function' && nav.canShare({ files });
    if (!can) {
      this.view = 'noshare';
      this.detailsOpen = true;
      this.paint();
      return;
    }
    const ids = picked.map((e) => e.id);
    nav.share({ files }).then(
      () => {
        if (this.batch !== batch) return;
        if (this.chunk + 1 < this.chunks.length) {
          this.queue.markChunkShared(ids);
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
    this.queue.finishShare();
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
    this.queue.dismiss();
    if (batch && batch.phase !== 'closed') {
      this.batch = null;
      batch.cancel();
    }
    this.paint();
  }
}
