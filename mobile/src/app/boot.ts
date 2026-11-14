// 手机上的三屏：起始、网格、编辑。不碰历史记录，返回就是回到网格。

import { Engine, type RawLayout, type Scene } from '../../../frontend/src/index';
import { neutralBake, NEUTRAL_BAKE_KEY } from '../lib/neutral';
import { classify, sortIds } from '../lib/group';
import { loadGap, loadOrder, saveGap, saveOrder } from '../lib/prefs';
import { formatStats, measureInterval, watchFrames, type PerfStats } from '../ui/perf';
import { warmCore } from '../core/api';
import { Book } from './book';
import { showCalibSheet } from './calib_ui';
import { EditorView } from './editor';
import { GridView } from './grid';
import { openable, photoMs, releaseBitmaps, releaseLooks, unread, zeroKnobs, type AppApi, type FullRead, type Photo } from './model';
import { ThumbQueue } from './queue';
import { renderHelp, renderStart } from './start';
import type { GapMinutes, Order } from '../lib/group';
import type { ThumbFail, ThumbOk } from '../workers/thumb';
import './shell.css';

type Reply = ThumbOk | ThumbFail;

class App implements AppApi {
  private readonly byId = new Map<string, Photo>();
  private sortOrder: Order = loadOrder();
  private gapMinutes: GapMinutes = loadGap();
  private ignoredCount = 0;
  private dat: File | null = null;
  private generation = 0;
  private screen: 'start' | 'grid' | 'editor' = 'start';
  private grid: GridView | null = null;
  private editor: EditorView | null = null;
  private engine: Engine | null = null;
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
  private readonly recipeBook = new Book(async (file) => {
    const reply = await this.reader.read(file);
    return reply.ok ? reply.bytes : null;
  });

  constructor(
    private readonly shell: HTMLElement,
    private readonly view: HTMLElement,
    private readonly picker: HTMLInputElement,
    private readonly calibPicker: HTMLInputElement,
    private readonly perf: HTMLElement,
    private readonly perfOn: boolean,
    private readonly pageErrors: string[],
  ) {}

  async start(engine: Engine | null): Promise<void> {
    this.engine = engine;
    this.recipeBook.attach(engine);
    // 相机配方文件、校准数据变了：网格的那一行跟着变。编辑器和校准卡自己订阅。
    this.recipeBook.onChange(() => {
      if (this.screen === 'grid') this.grid?.refresh();
    });
    const restored = this.recipeBook.restore().catch(() => undefined);
    if (this.perfOn) {
      this.perf.hidden = false;
      void measureInterval().then((value) => {
        this.interval = value;
      });
    }
    this.showStart();
    if (!import.meta.env.DEV) return;
    (window as unknown as { __gxrBook?: Book }).__gxrBook = this.recipeBook;
    await restored;
    const shot = new URLSearchParams(location.search).get('shot');
    if (!shot) return;
    const { sampleFiles, reportShot, aliasFile } = await import('./shots');
    // UI-M2 的几屏：校准数据和相机配方文件也走样本端点（截图用的临时样本目录里起了 zz- 开头的别名）。
    const recipeShot = ['recipe', 'nocal', 'nodat', 'jpg'].includes(shot);
    if (shot === 'calib' || shot === 'recipe') {
      const pack = await aliasFile('zz-calib.dng', 'GXR 校准数据.zip');
      if (pack) await this.recipeBook.importPack(pack);
    } else if (recipeShot) await this.recipeBook.removePack();
    if (shot !== 'start' && shot !== 'calib') {
      let files = shot === 'jpg' ? [] : await sampleFiles();
      const extra = shot === 'jpg' ? await aliasFile('zz-jpg.dng', 'R9990002.JPG') : shot === 'recipe' || shot === 'nocal' ? await aliasFile('zz-dat.dng', 'GXRCE.DAT') : null;
      if (extra) files = [...files, extra];
      if (files.length) {
        this.ingest(files);
        await this.settle();
      }
      for (let i = 0; i < 100 && (this.recipeBook.datBusy || this.recipeBook.packBusy); i++) await new Promise((done) => setTimeout(done, 100));
    }
    if ((shot === 'editor' || shot === 'drag' || recipeShot) && this.byId.size) {
      const first = this.ordered()[0];
      if (first) this.open(first.id);
      await this.editor?.settled;
      if (shot === 'drag') this.editor?.forceDrag();
      if (recipeShot) {
        this.view.querySelector<HTMLButtonElement>('.tabs button')?.click();
        await new Promise((done) => setTimeout(done, 1500));
        if (shot === 'recipe') this.view.querySelector<HTMLButtonElement>('.strip[data-recipes] .rc[data-card="builtin:7"]')?.click();
        await new Promise((done) => setTimeout(done, 800));
      }
    }
    await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
    this.shell.dataset.shot = shot;
    await reportShot(shot, {
      screen: this.screen,
      gpu: this.engine !== null,
      drag: this.shell.dataset.drag,
      photos: this.ordered().map((photo) => ({
        stem: photo.stem,
        status: photo.status,
        jpg: photo.jpg !== null,
        bitmap: photo.bitmap ? [photo.bitmap.width, photo.bitmap.height] : null,
        captureMs: photo.captureMs,
      })),
      errors: this.pageErrors.slice(0, 10),
    });
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
    return this.dat !== null;
  }

  book(): Book {
    return this.recipeBook;
  }

  pickDat(): void {
    this.picker.click();
  }

  pickCalib(): void {
    this.calibPicker.click();
  }

  showCalib(): void {
    showCalibSheet(this.shell, this);
  }

  takeCalib(file: File): void {
    void this.recipeBook.importPack(file);
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
      if (!this.current(photo.id, version, photo, file)) return false;
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
    renderHelp(this.shell);
  }

  open(id: string): void {
    const photo = this.byId.get(id);
    if (!photo || !openable(photo)) return;
    this.closeEditor();
    this.screen = 'editor';
    this.shell.dataset.screen = 'editor';
    this.editor = new EditorView(this.view, this.shell, this, this.engine);
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
    if (!this.byId.size) this.showStart();
    else this.grid?.refresh();
  }

  clear(): void {
    this.generation += 1;
    this.queue.cancel();
    this.closeEditor();
    for (const photo of this.byId.values()) releaseBitmaps(photo);
    this.byId.clear();
    this.dat = null;
    this.recipeBook.clearDat();
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
    if (photo) this.failCard(photo);
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
    this.ingest(files);
  }

  private showStart(): void {
    this.closeEditor();
    this.screen = 'start';
    this.shell.dataset.screen = 'start';
    this.grid = null;
    renderStart(this.view, this);
  }

  private showGrid(remount: boolean): void {
    this.closeEditor();
    this.shell.dataset.screen = 'grid';
    if (!this.grid || remount || this.screen !== 'grid') {
      this.grid = new GridView(this.view, this);
      this.grid.show();
    } else this.grid.refresh();
    this.screen = 'grid';
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
    if (classified.datIndex !== null) {
      this.dat = files[classified.datIndex];
      void this.recipeBook.setDat(this.dat, classified.datCount);
    }
    const generation = this.generation;
    for (const item of classified.photos) {
      const id = item.stem.toLowerCase();
      let photo = this.byId.get(id);
      if (!photo) {
        photo = {
          id,
          stem: item.stem,
          version: 0,
          dng: null,
          jpg: null,
          lastModified: item.lastModified,
          captureMs: null,
          status: 'pending',
          gone: false,
          cached: null,
          checked: null,
          bitmap: null,
          neutral: null,
          knobs: zeroKnobs(),
          jpgMeta: null,
          dngMeta: null,
          choice: null,
          looks: new Map(),
        };
        this.byId.set(id, photo);
      }
      if (item.dngIndex !== null) {
        photo.dng = files[item.dngIndex];
        photo.stem = item.stem;
      }
      if (item.jpgIndex !== null) photo.jpg = files[item.jpgIndex];
      // 换了 File 就是新版本：之前在路上的读取结果回来时都作废。
      photo.version += 1;
      photo.gone = false;
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
      this.drop(reply);
      return;
    }
    let from: File = file;
    if (photo.jpg && photo.dng && reply.ok && reply.bitmap === null) {
      // 相机 JPG 解不出来：镜头名和 ISO 还是先记下 JPG 里读到的（桌面先用 JPG 的）。
      photo.jpgMeta = reply.meta;
      this.drop(reply);
      const dng = photo.dng;
      from = dng;
      reply = await this.queue.run(dng, 'dng');
      if (!live(dng)) {
        this.drop(reply);
        return;
      }
    }
    if (!reply.ok && reply.gone) {
      this.failCard(photo);
      return;
    }
    if (!reply.ok) {
      photo.status = 'bad';
      this.afterRead(photo);
      return;
    }
    if (reply.captureMs !== null) photo.captureMs = reply.captureMs;
    if (from === photo.jpg) photo.jpgMeta = reply.meta;
    else photo.dngMeta = reply.meta;
    if (reply.bitmap) {
      photo.bitmap?.close();
      photo.bitmap = reply.bitmap;
    }
    photo.status = reply.bad ? 'bad' : 'ready';
    this.afterRead(photo);
  }

  private drop(reply: Reply): void {
    if (reply.ok && reply.bitmap) reply.bitmap.close();
  }

  private afterRead(photo: Photo): void {
    if (this.screen === 'grid') this.grid?.refresh();
    if (photo.status === 'bad' && this.screen === 'grid') this.grid?.showUnreadable(photo.id);
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

  private settle(): Promise<void> {
    return new Promise((resolve) => {
      const deadline = performance.now() + 20_000;
      const tick = () => {
        const { done, total } = this.loading();
        if ((total > 0 && done >= total) || performance.now() > deadline) resolve();
        else window.setTimeout(tick, 200);
      };
      tick();
    });
  }

  reportEngine(err: unknown): void {
    this.pageErrors.push(err instanceof Error ? err.message : String(err));
  }
}

export async function bootApp(root: HTMLElement, pageErrors: string[]): Promise<void> {
  const shell = document.createElement('div');
  shell.className = 'app';
  shell.dataset.screen = 'start';
  shell.dataset.drag = '0';
  const picker = document.createElement('input');
  picker.type = 'file';
  picker.multiple = true;
  picker.className = 'filepick';
  // 校准数据 zip 单独一个选择框，也不加 accept（「文件」里按类型过滤会把文件变灰）。
  const calibPicker = document.createElement('input');
  calibPicker.type = 'file';
  calibPicker.className = 'filepick calibpick';
  const view = document.createElement('div');
  view.className = 'view';
  const perf = document.createElement('p');
  perf.className = 'perf';
  perf.hidden = true;
  shell.append(picker, calibPicker, view, perf);
  root.replaceChildren(shell);
  const coreReady = warmCore();

  const perfOn = new URLSearchParams(location.search).get('perf') === '1';
  const app = new App(shell, view, picker, calibPicker, perf, perfOn, pageErrors);
  picker.addEventListener('change', () => {
    const files = picker.files ? Array.from(picker.files) : [];
    picker.value = '';
    if (files.length) app.take(files);
  });
  calibPicker.addEventListener('change', () => {
    const file = calibPicker.files?.[0] ?? null;
    calibPicker.value = '';
    if (file) app.takeCalib(file);
  });

  let engine: Engine | null = null;
  if (navigator.gpu) {
    try {
      engine = await Engine.create();
    } catch (err) {
      app.reportEngine(err);
      engine = null;
    }
  }
  if (engine) {
    const bake = neutralBake();
    engine.addBake(NEUTRAL_BAKE_KEY, bake.meta, bake.data);
    engine.onFrame = () => {
      shell.dataset.frame = '1';
    };
  }
  shell.dataset.gpu = engine ? '1' : '0';
  // 只在开发服务器里：测试要看显卡里有几份源，确认重开同一张不会多出一份。
  if (import.meta.env.DEV) (window as unknown as { __gxrEngine?: Engine | null }).__gxrEngine = engine;
  await coreReady;
  shell.dataset.core = '1';
  await app.start(engine);
}
