// 手机上的三屏：起始、网格、编辑。不碰历史记录，返回就是回到网格。
// 首屏只有开始页；网格和编辑器（连同导出、⋯ 菜单、关机图和显卡引擎）是两个懒加载分包，见 boot_lazy.ts。

// 全部样式随首屏一次加载，顺序与拆包前的单包一致：分包里没有样式，加载失败时只需重新请求一个 JS。
import './export.css';
import './lut_deck.css';
import './paste.css';
import './frame.css';
import './ipad.css';
import './shell.css';
import type { Engine, RawLayout, Scene } from '../../../frontend/src/index';
import { classify, sortIds } from '../lib/group';
import { loadGap, loadOrder, saveGap, saveOrder } from '../lib/prefs';
import { formatStats, measureInterval, watchFrames, type PerfStats } from '../ui/perf';
import { bakeIn, calibCheck, importCalib, loadDefaultCalib, readDat, resolveRecipe, restoreCalib, warmCore } from '../core/api';
import { camFilesIo, deletePack, loadWatermark } from '../lib/idb';
import { Book } from './book';
import { lookCache } from './looks';
import { afterFirstPaint, bindPickers, Chunk, clearChunkError, createBootDom, scheduleIdle, showChunkError } from './boot_lazy';
import type { ShotHost } from './boot_shots';
import { showCalibSheet } from './calib_ui';
import type { EditorView } from './editor';
import type * as EditorChunk from './editor_chunk';
import type { ExportFlow } from './export_ui';
import type * as GridChunk from './grid';
import type { GridView } from './grid';
import { dropGpuState, EngineSlot } from './gpu_loss';
import { LutBook } from './lut_book';
import { LutGpu } from './lut_gpu';
import { clearCameraRecipe, openable, photoMs, registerCameraRecipeReader, releaseBitmaps, releaseLooks, unread, type AppApi, type FullRead, type Photo } from './model';
import { newPhoto, photoId } from './new_photo';
import { othersReadable } from './card_probe';
import { unusedNote } from './import_note';
import { showToast } from './toast';
import { guardUnload } from './unload_guard';
import { dropReply, ThumbQueue } from './queue';
import { renderHelp, renderStart } from './start';
import type { GapMinutes, Order } from '../lib/group';
import { watchViewportLayout } from './layout';

class App implements AppApi {
  private readonly byId = new Map<string, Photo>();
  private sortOrder: Order = loadOrder();
  private gapMinutes: GapMinutes = loadGap();
  private ignoredCount = 0;
  private generation = 0;
  private screen: 'start' | 'grid' | 'editor' = 'start';
  private grid: GridView | null = null;
  private editor: EditorView | null = null;
  private engine: Engine | null = null;
  /** 网格分包：开始页画好后空闲时预取。照片都要等它到了才进来（take），所以有照片就一定有网格。 */
  private readonly gridChunk = new Chunk<typeof GridChunk>('grid', () => import('./grid'));
  /** 编辑器分包：第一批缩略图画好后预取，点开照片时还没到就等它。 */
  private readonly editorChunk = new Chunk<typeof EditorChunk>('editor', () => import('./editor_chunk'));
  /** 编辑器分包到了、导出流程建好、显卡引擎也定下来（有或没有）以后才不是 null。 */
  private editorMod: typeof EditorChunk | null = null;
  private editorLoad: Promise<typeof EditorChunk> | null = null;
  /** 显卡引擎：建好、建不起来和设备丢了都交给 useEngine（gpu_loss.ts）；建不起来的原因记进 pageErrors。 */
  private readonly gpuSlot = new EngineSlot(() => this.shell, (err) => this.pageErrors.push(err instanceof Error ? err.message : String(err)),
    (engine, lost) => this.useEngine(engine, lost));
  /** 编辑器开着的那张（设备丢了按没有引擎挂回它）。 */
  private editing: Photo | null = null;
  /** 选照片时网格分包还没到：先记下，到了再一起导入。 */
  private pendingFiles: File[] = [];
  /** 切屏、换照片、清空都加一。在路上的分包回来时不是同一次，就什么也不做（不弹旧照片，不报旧错误）。 */
  private nav = 0;
  private rawWarm = 0;
  private restored: Promise<void> = Promise.resolve();
  private frameWatch: { stop: () => PerfStats } | null = null;
  private warm: { id: string; version: number; bytes: ArrayBuffer } | null = null;
  private interval = 16.7;
  private cardIsOut = false;
  /** 每张照片最后一次上传的尾巴。新的上传接在后面。 */
  private readonly uploads = new Map<string, Promise<unknown>>();
  private readonly queue = new ThumbQueue(2);
  /** 整份 DNG 单独一个 Worker：点开照片不排在缩略图后面。 */
  private readonly reader = new ThumbQueue(1);
  /** 相机配方文件、校准数据和烘焙表。文件字节都由上面那个 Worker 读。 */
  private readonly recipeBook = new Book({
    readFile: async (file) => {
      const reply = await this.reader.read(file);
      return reply.ok ? reply.bytes : null;
    },
    readDat,
    importCalib,
    restoreCalib,
    bake: bakeIn,
    resolve: resolveRecipe,
    calibCheck,
    deletePack,
    watermark: loadWatermark,
    defaultCalib: loadDefaultCalib,
    ...camFilesIo,
  });
  /** 用户导入的 .cube LUT 目录（只存 IndexedDB 与 Worker，页面按需拿字节）。 */
  private readonly lutBookStore = new LutBook();
  /** 3 槽 GPU 3D LUT 纹理队列与短命 lut_table Worker 调度。 */
  private readonly lutGpuQueue = new LutGpu(() => this.engine);

  /** 导出进相册（UI-M3）：导出面板、贴底的进度和结果、共享面板。在编辑器分包里，第一次加载时建一个。 */
  private exporter: ExportFlow | null = null;

  constructor(
    private readonly shell: HTMLElement,
    private readonly view: HTMLElement,
    private readonly picker: HTMLInputElement,
    private readonly calibPicker: HTMLInputElement,
    private readonly lutPicker: HTMLInputElement,
    private readonly perf: HTMLElement,
    private readonly perfOn: boolean,
    private readonly pageErrors: string[],
  ) {
    watchViewportLayout(shell, () => {}); // data-layout 跟着窗口走，图库和开始页上也一样（不只编辑器里）
  }

  start(): void {
    registerCameraRecipeReader(async (file) => {
      const reply = await this.reader.read(new File([file.slice(0, 262144)], file.name));
      return reply.ok ? reply.bytes : null;
    });
    this.lutGpuQueue.attach(() => this.engine);
    // 相机配方文件、校准数据变了：网格的那一行跟着变。编辑器和校准卡自己订阅。
    let generation = this.recipeBook.generationId();
    this.recipeBook.onChange(() => {
      // 换了相机配方文件或校准数据：旧表画的配方卡小图全部放掉。
      if (this.recipeBook.generationId() !== generation) {
        generation = this.recipeBook.generationId();
        lookCache.clear();
      }
      if (this.screen === 'grid') this.grid?.refresh();
    });
    if (this.perfOn) {
      this.perf.hidden = false;
      void measureInterval().then((value) => {
        this.interval = value;
      });
    }
    this.showStart();
    // 开始页先画出来；本机存的校准、LUT、网格分包和显卡引擎（WebGPU 初始化可能要几百毫秒）都排在后面。
    this.restored = new Promise<void>((done) =>
      afterFirstPaint(() => {
        done(Promise.all([this.recipeBook.restore().catch(() => undefined), this.lutBookStore.restore().catch(() => undefined)]).then(() => undefined));
        scheduleIdle(() => {
          void this.gridChunk.load().catch(() => undefined);
          void this.gpuSlot.ensure();
        });
      }),
    );
  }

  /** 只在开发服务器里：两个分包都先加载好（测试和截图照旧同步切屏），标上 data-core，再跑 ?shot= 截图。 */
  async startDev(ready: () => void): Promise<void> {
    if (!import.meta.env.DEV) return ready();
    Object.assign(window, { __gxrBook: this.recipeBook, __gxrLooks: lookCache, __gxrLutBook: this.lutBookStore, __gxrLutGpu: this.lutGpuQueue });
    await Promise.all([this.gridChunk.load(), this.loadEditor(), this.restored]);
    ready();
    const shot = new URLSearchParams(location.search).get('shot');
    if (!shot) return;
    const host: ShotHost = { api: this, shell: this.shell, view: this.view, pageErrors: this.pageErrors, book: this.recipeBook,
      screen: () => this.screen, editor: () => this.editor, exporter: () => this.exporter, ingest: (files) => this.ingest(files) };
    await (await import('./boot_shots')).runShot(shot, host);
  }

  /** 显卡引擎定下来了（建不起来是 null）。设备丢了：关编辑器，放掉挂在旧引擎上的东西，按没有引擎挂回同一张（同没有 WebGPU）。 */
  private useEngine(engine: Engine | null, lost: boolean): void {
    this.engine = engine;
    this.recipeBook.attach(engine);
    this.shell.dataset.gpu = engine ? '1' : '0';
    if (import.meta.env.DEV) (window as unknown as { __gxrEngine?: Engine | null }).__gxrEngine = engine; // 测试看显卡里有几份源
    if (!lost) return;
    const photo = this.screen === 'editor' ? this.editing : null;
    this.closeEditor();
    dropGpuState(this.shell, this.byId.values(), this.lutGpuQueue);
    if (photo && this.editorMod && openable(photo)) this.mountEditor(this.editorMod, photo);
    else if (photo) this.showGrid(true);
    if (this.cardIsOut) this.cardChanged();
  }

  /** 编辑器分包：只加载一次；到了就建导出流程，再等显卡引擎定下来。失败了下次再调会重新请求。 */
  private loadEditor(): Promise<typeof EditorChunk> {
    if (this.editorMod) return Promise.resolve(this.editorMod);
    if (this.editorLoad) return this.editorLoad;
    const task = this.editorChunk.load().then(async (mod) => {
      this.exporter ??= new mod.ExportFlow(this.shell, this, this.perf);
      await this.gpuSlot.ensure();
      this.editorMod ??= mod;
      return mod;
    });
    this.editorLoad = task;
    task.catch(() => { if (this.editorLoad === task) this.editorLoad = null; }); // 下次再请求（Chunk 会换地址）
    return task;
  }

  /** 切屏、换照片、清空：在路上的分包加载作废，开着的加载失败提示关掉。 */
  private navigate(): number {
    this.nav += 1;
    clearChunkError(this.shell);
    return this.nav;
  }

  photos(): Photo[] {
    return this.ordered();
  }

  photo(id: string): Photo | undefined {
    return this.byId.get(id);
  }

  order(): Order {
    return this.sortOrder;
  }

  gap(): GapMinutes {
    return this.gapMinutes;
  }

  ignored(): number {
    return this.ignoredCount;
  }

  hasDat(): boolean {
    return this.recipeBook.dat !== null;
  }

  book(): Book {
    return this.recipeBook;
  }

  lutBook(): LutBook {
    return this.lutBookStore;
  }

  lutGpu(): LutGpu {
    return this.lutGpuQueue;
  }

  pickLut(): void {
    this.lutPicker.click();
  }

  pickDat(): void {
    this.picker.click();
  }

  pickCalib(): void {
    this.calibPicker.click();
  }

  showCalib(): void {
    this.navigate();
    showCalibSheet(this.shell, this);
  }

  exportPhotos(ids: string[], onStart?: () => void): void {
    const want = new Set(ids);
    const list = this.ordered().filter((photo) => want.has(photo.id)).map((photo) => photo.id);
    if (this.editorMod && this.exporter) return this.exporter.open(list, onStart);
    // 图库多选导出时编辑器分包（导出在里面）还没到：等它；这期间切了屏、清空了就作废。
    const ticket = this.navigate();
    this.loadEditor().then(
      () => {
        if (ticket === this.nav) this.exporter?.open(list, onStart);
      },
      () => {
        if (ticket === this.nav) showChunkError(this.shell, 'editor', () => this.exportPhotos(ids, onStart));
      },
    );
  }

  /** 图库里改了多选或打开了面板：在等分包的点开和导出作废（AppApi.dropPending）。 */
  readonly dropPending = (): void => void this.navigate();
  takeCalib(file: File): void {
    void this.recipeBook.importPack(file);
  }

  takeLuts(files: File[]): void {
    void this.lutBookStore.importFiles(files);
  }

  loading(): { done: number; total: number } {
    let done = 0;
    for (const photo of this.byId.values()) if (photo.status !== 'pending' || photo.gone) done += 1;
    return { done, total: this.byId.size };
  }

  uploadRaw(photo: Photo, version: number, layout: RawLayout, bytes: ArrayBuffer, scene: Scene | null): Promise<boolean> {
    const engine = this.engine;
    if (!engine) return Promise.resolve(false);
    const file = photo.dng;
    const prev = this.uploads.get(photo.id) ?? Promise.resolve();
    const run = prev.then(async () => {
      // 轮到这一次时再核对：排队期间重新选过、去掉了、清空了，就不提交，免得把新源换回旧的。
      if (!this.current(photo.id, version, photo, file)) return false;
      await engine.addRaw(`raw:${photo.id}`, layout, bytes, scene);
      // 传的时候设备丢了：这份源不在现在的引擎里。
      if (!this.current(photo.id, version, photo, file) || this.engine !== engine) return false;
      photo.cached = version;
      // 卡已经拔了、这次上传才落地：这张现在能接着用，数字要跟着变。
      if (this.cardIsOut) this.cardChanged();
      return true;
    });
    const tail = run.catch(() => false);
    this.uploads.set(photo.id, tail);
    void tail.then(() => {
      if (this.uploads.get(photo.id) === tail) this.uploads.delete(photo.id);
    });
    return run;
  }

  gpu(): boolean {
    return this.engine !== null;
  }

  session(): number {
    return this.generation;
  }

  cardOut(): boolean {
    return this.cardIsOut;
  }

  async readFull(photo: Photo): Promise<FullRead> {
    const file = photo.dng;
    const version = photo.version;
    if (!file) return { ok: false, gone: false, stale: false };
    const reply = await this.reader.read(file);
    if (!this.current(photo.id, version, photo, file)) return { ok: false, gone: false, stale: true };
    return reply.ok ? { ok: true, bytes: reply.bytes } : { ok: false, gone: reply.gone, stale: false };
  }

  async still(photo: Photo, width: number): Promise<ImageBitmap | null> {
    const file = photo.jpg ?? photo.dng;
    const version = photo.version;
    if (!file) return null;
    const reply = await this.queue.run(file, photo.jpg ? 'jpg' : 'dng', width, true);
    if (!reply.ok) return null;
    if (!this.current(photo.id, version, photo, file)) {
      reply.bitmap?.close();
      return null;
    }
    return reply.bitmap;
  }

  setOrder(order: Order): void {
    this.sortOrder = order;
    saveOrder(order);
    this.grid?.refresh();
  }

  setGap(gap: GapMinutes): void {
    this.gapMinutes = gap;
    saveGap(gap);
    this.grid?.refresh();
  }

  addPhotos(): void {
    this.picker.click();
  }

  showHelp(): void {
    this.navigate(); // 在等编辑器分包的点开或导出作废，不在帮助后面切屏
    renderHelp(this.shell);
  }

  open(id: string): void {
    const photo = this.byId.get(id);
    if (!photo || !openable(photo)) return;
    const ticket = this.navigate();
    const retry = this.gpuSlot.retry(); // 设备丢过：先再建一次引擎，建好了这一张就走显卡（gpu_loss.ts）
    if (this.editorMod && !retry) {
      this.mountEditor(this.editorMod, photo);
      return;
    }
    // 编辑器分包还没到（或在重建引擎）：留在当前这一屏等它。等的时候又点了别的、返回了、清空了，这次就作废。
    const version = photo.version;
    Promise.all([this.loadEditor(), retry]).then(
      ([mod]) => {
        if (ticket === this.nav && this.current(id, version, photo) && openable(photo)) this.mountEditor(mod, photo);
      },
      () => {
        if (ticket === this.nav) showChunkError(this.shell, 'editor', () => this.open(id));
      },
    );
  }

  private mountEditor(mod: typeof EditorChunk, photo: Photo): void {
    this.cancelRawWarm();
    this.grid?.destroy(); this.grid = null;
    this.closeEditor();
    this.resetPerf();
    this.screen = 'editor';
    this.shell.dataset.screen = 'editor';
    this.editor = new mod.EditorView(this.view, this.shell, this, this.engine);
    this.editing = photo;
    this.editor.open(photo);
  }

  back(): void {
    this.warm = null;
    if (this.byId.size) this.showGrid(true);
    else this.showStart();
  }

  takeWarm(id: string, version: number): ArrayBuffer | null {
    const warm = this.warm;
    this.warm = null;
    return warm && warm.id === id && warm.version === version ? warm.bytes : null;
  }

  keepWarm(id: string, version: number, bytes: ArrayBuffer): void {
    if (this.current(id, version)) this.warm = { id, version, bytes };
  }

  /** 这张照片还是同一个对象、同一个版本。重新选过、去掉了、清空了都算过期。 */
  private current(id: string, version: number, photo?: Photo, file?: File | null): boolean {
    const now = this.byId.get(id);
    if (!now || now.version !== version) return false;
    if (photo && now !== photo) return false;
    if (file !== undefined && file !== now.jpg && file !== now.dng) return false;
    return true;
  }

  swipe(id: string, dir: -1 | 1): void {
    const items = this.ordered().filter((photo) => openable(photo));
    const index = items.findIndex((photo) => photo.id === id);
    const next = items[index + dir];
    if (next) this.open(next.id);
  }

  remove(id: string): void {
    const photo = this.byId.get(id);
    if (photo) releaseBitmaps(photo);
    this.byId.delete(id);
    this.lutGpuQueue.clear();
    if (!this.byId.size) this.showStart();
    else this.grid?.refresh();
  }

  clear(): void {
    this.generation += 1;
    this.pendingFiles = [];
    // 导出这一批停下，做好的文件丢掉，晚到的结果不会再交给共享面板。
    this.exporter?.clear();
    this.queue.cancel();
    this.lutGpuQueue.clear();
    this.closeEditor();
    for (const photo of this.byId.values()) releaseBitmaps(photo);
    this.byId.clear();
    this.recipeBook.clearSessionCamFiles();
    this.ignoredCount = 0;
    this.warm = null;
    this.showStart();
  }

  markBad(id: string, version: number): void {
    if (!this.current(id, version)) return;
    const photo = this.byId.get(id);
    if (photo) photo.status = 'bad';
    this.showGrid(true);
    this.grid?.showUnreadable(id);
  }

  markGone(id: string, version: number): void {
    if (!this.current(id, version)) return;
    const photo = this.byId.get(id);
    if (photo) this.lostFile(photo, version);
  }

  /** 一个文件读不了：别的照片还读得到，就只是这一张（挪走、删了），标成读不了；都读不到才按拔卡处理。 */
  private lostFile(photo: Photo, version: number): void {
    if (this.cardIsOut) return this.failCard(photo);
    const session = this.generation;
    othersReadable(this.byId.values(), photo).then((ok) => {
      if (session !== this.generation || !this.current(photo.id, version, photo)) return;
      if (!ok) return this.failCard(photo);
      if (this.screen === 'editor' && this.editing === photo) return this.markBad(photo.id, version);
      photo.status = 'bad';
      this.afterRead(photo);
    }, () => undefined);
  }

  watch(kind: 'scroll' | 'drag', on: boolean): void {
    if (!this.perfOn) return;
    if (on) {
      this.frameWatch ??= watchFrames(this.interval);
      return;
    }
    if (!this.frameWatch) return;
    const stats = this.frameWatch.stop();
    this.frameWatch = null;
    this.perf.hidden = false;
    this.perf.dataset.perf = JSON.stringify(stats);
    this.perf.dataset.kind = kind;
    this.perf.textContent = formatStats(stats);
  }

  take(files: File[]): void {
    const photos = classify(files.map((file) => ({ name: file.name, lastModified: file.lastModified }))).photos.length;
    if (!photos || this.gridChunk.loaded) return this.ingest(files);
    // 网格分包还没到（一般开始页空闲时就预取好了）：照片先记下，到了再导入，失败可以重试。
    this.pendingFiles.push(...files);
    this.takePending();
  }

  private takePending(): void {
    const session = this.generation;
    this.gridChunk.load().then(
      () => {
        if (session !== this.generation) return;
        const files = this.pendingFiles.splice(0);
        if (files.length) this.ingest(files);
      },
      () => {
        if (session === this.generation && this.pendingFiles.length) showChunkError(this.shell, 'grid', () => this.takePending());
      },
    );
  }

  private showStart(): void {
    this.navigate();
    this.cancelRawWarm();
    this.closeEditor();
    this.resetPerf();
    this.screen = 'start';
    this.shell.dataset.screen = 'start';
    this.grid?.destroy(); this.grid = null;
    renderStart(this.view, this);
    this.syncDock();
  }

  private showGrid(remount: boolean): void {
    const mod = this.gridChunk.loaded;
    // 照片都是网格分包到了以后才导入的，有照片就有网格；万一没有，就回开始页。
    if (!mod) return this.showStart();
    this.navigate();
    this.closeEditor();
    this.shell.dataset.screen = 'grid';
    if (this.screen !== 'grid') this.resetPerf();
    if (!this.grid || remount || this.screen !== 'grid') {
      this.grid?.destroy(); this.grid = new mod.GridView(this.view, this);
      this.grid.show();
    } else this.grid.refresh();
    this.screen = 'grid';
    this.syncDock();
    this.prewarmEditor();
  }

  private syncDock(): void {
    this.editorMod?.syncExportDock(this.shell);
    guardUnload(this.byId.size > 0 || this.exporter?.current()?.phase === 'running'); // 电脑上刷新、关页前先问一声
  }

  /**
   * 第一批缩略图（最多 8 张）读完、画出至少一张以后，空出这一帧再预取编辑器分包。
   * 只有 RAW、没有能画的缩略图时：全部读完、再静一会儿（1.5 秒加空闲）才预取。点开照片随时都会按需加载。
   */
  private prewarmEditor(): void {
    if (this.editorMod || this.editorLoad || this.screen !== 'grid') return;
    const { done, total } = this.loading();
    if (!total || done < Math.min(total, 8)) return;
    if (this.view.querySelector('.cell canvas[data-drawn]')) {
      this.cancelRawWarm();
      afterFirstPaint(() => void this.loadEditor().catch(() => undefined));
      return;
    }
    if (done < total || this.rawWarm || ![...this.byId.values()].some((photo) => openable(photo))) return;
    const session = this.generation;
    this.rawWarm = window.setTimeout(() => {
      this.rawWarm = 0;
      scheduleIdle(() => {
        if (session === this.generation && this.screen === 'grid') void this.loadEditor().catch(() => undefined);
      });
    }, 1500);
  }

  private cancelRawWarm(): void {
    window.clearTimeout(this.rawWarm);
    this.rawWarm = 0;
  }

  private resetPerf(): void {
    this.frameWatch?.stop();
    this.frameWatch = null;
    this.perf.textContent = '';
    delete this.perf.dataset.perf;
    delete this.perf.dataset.kind;
  }

  private closeEditor(): void {
    this.editor?.destroy();
    this.editor = null;
    this.shell.dataset.drag = '0';
  }

  /** 网格、编辑器的「第几张」和左右滑都用这一份顺序：同一时间按 id 排，倒序时整份反过来。 */
  private ordered(): Photo[] {
    const items = [...this.byId.values()].map((photo) => ({ id: photo.id, ms: photoMs(photo), photo }));
    return sortIds(items, this.sortOrder).map((item) => item.photo);
  }

  private ingest(files: File[]): void {
    const classified = classify(files.map((file) => ({ name: file.name, lastModified: file.lastModified })));
    this.ignoredCount += classified.ignored;
    const note = unusedNote(classified); // 不是相机照片的文件说一句；一个都没用上也说
    if (note) showToast(this.shell, note, 5000);
    this.recipeBook.ingestCamFiles(
      classified.datIndex !== null ? files[classified.datIndex] : null,
      classified.datCount,
      classified.binIndex !== null ? files[classified.binIndex] : null,
    );
    const generation = this.generation;
    for (const item of classified.photos) {
      const id = photoId(this.byId, item.stem, item.jpgIndex !== null ? files[item.jpgIndex] : null, item.dngIndex !== null ? files[item.dngIndex] : null);
      let photo = this.byId.get(id);
      if (!photo) {
        photo = newPhoto(id, item.stem, item.lastModified);
        this.byId.set(id, photo);
      }
      if (item.jpgIndex !== null) {
        photo.jpg = files[item.jpgIndex];
        photo.diptych = false;
      }
      if (item.dngIndex !== null) {
        if (!photo.diptych) photo.dng = files[item.dngIndex];
        photo.stem = item.stem;
      }
      // 换了 File 就是新版本：之前在路上的读取结果回来时都作废。
      photo.version += 1;
      photo.gone = false;
      photo.frameMeta = null;
      clearCameraRecipe(photo);
      photo.neutral?.bitmap.close();
      photo.neutral = null;
      releaseLooks(photo);
      photo.lastModified = Math.max(photo.lastModified, item.lastModified);
      if (photo.status !== 'pending') {
        photo.bitmap?.close();
        photo.bitmap = null;
        photo.captureMs = null;
        photo.status = 'pending';
      }
      void this.readPhoto(photo, photo.version, generation);
    }
    // 重新选了照片：没有剩下没读完的，就不再算卡被拔出。
    this.cardIsOut = [...this.byId.values()].some((photo) => unread(photo));
    // 只选了相机配方文件（06d 的按钮）：留在这一屏，配方那边自己会刷新。
    if (!classified.photos.length) return;
    if (this.byId.size) this.showGrid(this.screen !== 'grid');
    else this.showStart();
  }

  private async readPhoto(photo: Photo, version: number, generation: number): Promise<void> {
    const file = photo.jpg ?? photo.dng;
    if (!file) {
      photo.status = 'bad';
      this.afterRead(photo);
      return;
    }
    const live = (handle: File | null) => generation === this.generation && this.current(photo.id, version, photo, handle);
    let reply = await this.queue.run(file, photo.jpg ? 'jpg' : 'dng');
    if (!live(file)) {
      dropReply(reply);
      return;
    }
    let from: File = file;
    if (photo.jpg && photo.dng && reply.ok && reply.bitmap === null) {
      // 相机 JPG 解不出来：镜头名和 ISO 还是先记下 JPG 里读到的（桌面先用 JPG 的）。
      photo.jpgMeta = reply.meta;
      if (reply.frame) photo.frame = reply.frame;
      dropReply(reply);
      const dng = photo.dng;
      from = dng;
      reply = await this.queue.run(dng, 'dng');
      if (!live(dng)) {
        dropReply(reply);
        return;
      }
    }
    if (!reply.ok && reply.gone) {
      this.lostFile(photo, version);
      return;
    }
    if (!reply.ok) {
      photo.status = 'bad';
      this.afterRead(photo);
      return;
    }
    if (!reply.bad && from === photo.jpg && reply.diptych) {
      photo.diptych = true;
      photo.dng = null;
      photo.dngMeta = null;
      clearCameraRecipe(photo);
    }
    let captureMs = reply.captureMs;
    let frame = reply.frame;
    if ((captureMs === null || frame === null) && from === photo.jpg && photo.dng) {
      // 相机 JPG 里没有拍摄时间或尺寸：照桌面导入（先 JPG、再 DNG，最后才是文件日期）用 DNG 的。
      const dng = photo.dng;
      const extra = await this.queue.run(dng, 'dng');
      if (!live(dng)) {
        dropReply(extra);
        dropReply(reply);
        return;
      }
      if (extra.ok && !extra.bad) {
        if (captureMs === null) captureMs = extra.captureMs;
        photo.dngMeta = extra.meta;
        if (frame === null) frame = extra.frame;
      }
      dropReply(extra);
    }
    if (captureMs !== null) photo.captureMs = captureMs;
    if (frame) photo.frame = frame;
    if (from === photo.jpg) photo.jpgMeta = reply.meta;
    else photo.dngMeta = reply.meta;
    if (reply.bitmap) {
      photo.bitmap?.close();
      photo.bitmap = reply.bitmap;
    }
    photo.status = reply.bad ? 'bad' : 'ready';
    this.afterRead(photo);
  }

  private afterRead(photo: Photo): void {
    if (this.screen === 'grid') this.grid?.update(photo.id);
    if (photo.status === 'bad' && this.screen === 'grid') this.grid?.showUnreadable(photo.id);
    this.prewarmEditor();
  }

  /**
   * 读的时候文件句柄失效：卡被拔出了。还在排队的、这次读失败的标成读不了；
   * 已经在显卡里（或描述过）的同版本照片照常能开。这个状态一直留着，直到重新选照片。
   */
  private failCard(trigger: Photo): void {
    trigger.gone = true;
    // 卡拔了：只有完整源还在显卡里（同一版本）的照片能接着用。只读过缩略图的也要原文件，
    // 一律标成没读完、需要重新选；缩略图留着，格子上标卡的图标。
    for (const photo of this.byId.values()) {
      const live = this.engine !== null && photo.cached === photo.version && this.engine.hasSource(`raw:${photo.id}`);
      if (!live) {
        photo.gone = true;
        if (photo.cached !== null && photo.cached === photo.version) photo.cached = null;
      }
    }
    this.cardIsOut = true;
    this.queue.cancel();
    if (this.screen === 'editor') this.showGrid(true);
    const { ready, missing } = this.cardCounts();
    this.grid?.showCard(ready, missing);
    this.grid?.refresh();
  }

  /** 能接着用的（同版本、源还在显卡里）和没读完的。 */
  private cardCounts(): { ready: number; missing: number } {
    let ready = 0;
    let missing = 0;
    for (const photo of this.byId.values()) {
      if (photo.cached !== null && this.engine && !this.engine.hasSource(`raw:${photo.id}`)) photo.cached = null;
      if (openable(photo)) ready += 1;
      else if (unread(photo)) missing += 1;
    }
    return { ready, missing };
  }

  /** 拔卡期间缓存变了：重算，刷新格子和副标题；还开着的拔卡提示改数字，关掉了的不再弹。 */
  private cardChanged(): void {
    const { ready, missing } = this.cardCounts();
    if (this.screen !== 'grid') return;
    this.grid?.updateCard(ready, missing);
    this.grid?.refresh();
  }
}

export async function bootApp(root: HTMLElement, pageErrors: string[]): Promise<void> {
  // 核心 Worker 先连上（只 ping，不拉 WASM），和开始页并行。
  const coreReady = warmCore();
  const dom = createBootDom(root);
  const perfOn = new URLSearchParams(location.search).get('perf') === '1';
  const app = new App(dom.shell, dom.view, dom.picker, dom.calibPicker, dom.lutPicker, dom.perf, perfOn, pageErrors);
  bindPickers(dom, app);
  app.start();
  await coreReady;
  // 开发服务器：两个分包和显卡引擎都到了才标 data-core，测试照旧在那之后切屏。
  await app.startDev(() => {
    dom.shell.dataset.core = '1';
  });
}
