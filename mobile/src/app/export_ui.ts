// 导出进相册：导出面板（05a）→ 点「存到相册」面板收起，进度贴在底部、不挡操作（05b）→ 做好了（05c）或有失败（06f）
// → 再点一次「存到相册」打开系统共享面板（05d）→ 面板关了（05e）。Safari 只认刚发生的那次点击，批量做完那次点击
// 已经失效，所以做好后不自动打开共享面板（DESIGN-MOBILE §5）。

import { ExportCore, calibPack, type RunResult } from '../core/export_api';
import { unitOfLens } from '../lib/calib';
import { mergeMeta } from '../lib/exif';
import { buildRequest, buildSide, type QualityTier, type RequestPhoto, type SizeTier } from '../lib/export_request';
import { formatClock, formatDay } from '../lib/group';
import { shelf, shelves, type Card } from '../lib/recipes';
import { MANY_SIZES, SIZE_FAILED, SIZE_PENDING, shareChunks, shareLabel, sizeText, SHARE_MAX_BYTES, SHARE_MAX_FILES } from '../lib/share_plan';
import type { Book } from './book';
import { button, el } from './dom';
import { ExportBatch, type BatchHost, type BatchItem, type Staged } from './export_batch';
import { setExportPaused } from './export_pause';
import { photoMs, type Photo } from './model';
import './export.css';

declare const __GXR_BUILD__: string;

const SIZES: Array<[SizeTier, string]> = [
  ['original', '原始尺寸'],
  ['large', '大'],
  ['medium', '中'],
  ['small', '小'],
];
const QUALITIES: Array<[QualityTier, string]> = [
  ['high', '高'],
  ['medium', '中'],
  ['low', '低'],
];
const HDR_LINE = '在 iPhone 上看，亮处会更亮，更接近你当时看到的；别的设备照常显示。';
const NO_DAT_GUIDE = '未导入相机配方文件时，导出不会带上相机配方效果：有相机 JPG 的照片导出相机 JPG；只有 RAW、又套用了配方的照片可能导不出。';
const NO_SHARE = '这个浏览器打不开系统的共享面板。';

const NS = 'http://www.w3.org/2000/svg';
const PATH = {
  export: 'M12 15V3M7.5 7.5 12 3l4.5 4.5M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7',
  check: 'M5 12.5l4.5 4.5L19 7',
  x: 'M6 6l12 12M18 6 6 18',
  alert: 'M12 4 2.5 20h19L12 4zM12 10v4.5M12 17.5h.01',
};

function ico(d: string, cls = ''): SVGSVGElement {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', `ico ${cls}`.trim());
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(NS, 'path');
  path.setAttribute('d', d);
  svg.append(path);
  return svg;
}

/** 导出这边要的 App 接口。 */
export interface ExportApp {
  photos(): Photo[];
  photo(id: string): Photo | undefined;
  session(): number;
  book(): Book;
  markGone(id: string, version: number): void;
}

/** 只在开发服务器里：测试改 binding、分批上限、在第几张模拟陷阱，读批次和内存。生产包里没有这段。 */
interface DevExport {
  binding?: string;
  chunkFiles?: number;
  trapAt?: number;
  flow?: ExportFlow;
  core?: ExportCore;
}

function dev(): DevExport | null {
  if (!import.meta.env.DEV) return null;
  const scope = window as unknown as { __devExport?: DevExport };
  scope.__devExport ??= {};
  return scope.__devExport;
}

function cardsFor(book: Book, photo: Photo): Card[] {
  const dat = book.dat;
  if (!dat) return [];
  const unit = unitOfLens(mergeMeta(photo.jpgMeta, photo.dngMeta).lens) ?? 'jlaunch';
  return shelf(shelves(dat.listing, unit), 'all');
}

function requestPhoto(photo: Photo): RequestPhoto {
  const meta = mergeMeta(photo.jpgMeta, photo.dngMeta);
  const source = photo.jpg ?? photo.dng;
  return {
    stem: photo.stem,
    dngName: photo.dng?.name ?? null,
    jpgName: photo.jpg?.name ?? null,
    lens: meta.lens,
    iso: meta.iso,
    choice: photo.choice,
    levels: photo.levels,
    knobs: photo.knobs,
    sourceModified: source?.lastModified ?? photo.lastModified,
  };
}

function timeRange(photos: Photo[]): string {
  if (!photos.length) return 'JPG';
  const times = photos.map((p) => photoMs(p));
  const lo = Math.min(...times);
  const hi = Math.max(...times);
  const a = formatClock(lo);
  const b = formatClock(hi);
  // 跨了天就带上日期，免得「18:51–11:46」看起来像倒着的。
  if (formatDay(lo) !== formatDay(hi)) return `${formatDay(lo)} ${a} – ${formatDay(hi)} ${b} · JPG`;
  return `${a === b ? a : `${a}–${b}`} · JPG`;
}

function seg<T extends string>(items: Array<[T, string]>, value: T, pick: (v: T) => void): HTMLElement {
  const box = el('div', 'seg');
  box.setAttribute('role', 'group');
  for (const [v, label] of items) {
    const item = button(v === value ? 'on' : '', label);
    item.dataset.value = v;
    item.addEventListener('click', () => {
      for (const node of box.children) node.classList.toggle('on', node === item);
      pick(v);
    });
    box.append(item);
  }
  return box;
}

type View = 'progress' | 'saved' | 'noshare';

export class ExportFlow {
  readonly core = new ExportCore();
  private batch: ExportBatch | null = null;
  private dock: HTMLElement | null = null;
  private sheet: HTMLElement[] = [];
  private view: View = 'progress';
  private chunk = 0;
  private chunks: number[][] = [];
  private savedCount = 0;
  private wake: { release(): Promise<void> } | null = null;
  private sizeSeq = 0;

  constructor(
    private readonly shell: HTMLElement,
    private readonly app: ExportApp,
    private readonly perf: HTMLElement | null,
  ) {
    const hooks = dev();
    if (hooks) {
      hooks.flow = this;
      hooks.core = this.core;
    }
  }

  current(): ExportBatch | null {
    return this.batch;
  }

  /** 05a：「导出 N 张」（选择栏）和「导出这张」（编辑器）都打开它。`ids` 已经按网格的顺序排好。 */
  open(ids: string[], onStart?: () => void): void {
    const photos = ids.map((id) => this.app.photo(id)).filter((p): p is Photo => !!p);
    if (!photos.length) return;
    this.closeSheet();
    let size: SizeTier = 'original';
    let quality: QualityTier = 'high';
    const n = photos.length;
    const scrim = el('div', 'scrim');
    scrim.addEventListener('click', () => this.closeSheet());
    const sheet = el('div', 'ms export');
    sheet.dataset.state = 'export-sheet';
    const head = el('div', 'row');
    const cancel = button('nb', '取消');
    cancel.addEventListener('click', () => this.closeSheet());
    head.append(el('h2', '', `导出 ${n} 张`), cancel);
    const sizeLine = el('span', 'num size', n === 1 ? SIZE_PENDING : MANY_SIZES);
    const sizeRow = el('div', 'frow');
    const sizeLab = el('div', 'lab');
    sizeLab.append(el('b', '', '尺寸'), sizeLine);
    let sized: { sizes: Record<string, [number, number]>; ratio: [number, number] | null } | null = null;
    const paintSize = () => {
      if (n !== 1) return;
      if (sized) sizeLine.textContent = sizeText(sized.sizes[size] ?? sized.sizes.original, sized.ratio);
    };
    sizeRow.append(
      sizeLab,
      seg(SIZES, size, (v) => {
        size = v;
        paintSize();
      }),
    );
    const qualityRow = el('div', 'frow');
    const qualityLab = el('div', 'lab');
    qualityLab.append(el('b', '', '品质'));
    qualityRow.append(
      qualityLab,
      seg(QUALITIES, quality, (v) => {
        quality = v;
      }),
    );
    // HDR 照设计稿摆着，开关停用，写「这项下一步做」（M1 的做法），等 ui-m3-hdr。
    const hdrRow = el('div', 'frow');
    const hdr = el('div', 'hdrrow');
    const hdrText = el('div', 'tx');
    hdrText.append(el('b', '', 'HDR'), el('small', '', HDR_LINE));
    const sw = document.createElement('input');
    sw.type = 'checkbox';
    sw.className = 'switch';
    sw.disabled = true;
    sw.setAttribute('aria-label', 'HDR');
    hdr.append(hdrText, el('span', 'later', '这项下一步做'), sw);
    hdrRow.append(hdr);
    const form = el('div');
    form.style.marginTop = '10px';
    form.append(sizeRow, qualityRow, hdrRow);
    if (!this.app.book().dat) {
      const guide = el('p', 'guide', NO_DAT_GUIDE);
      guide.dataset.state = 'no-dat-guide';
      form.append(guide);
    }
    const go = button('btn primary lg block go', '');
    go.append(ico(PATH.export, 'md'), document.createTextNode('存到相册'));
    go.addEventListener('click', () => {
      this.closeSheet();
      onStart?.();
      this.start(photos, { size, quality });
    });
    const foot = el('p', 'foot', `做好后再点一次「存到相册」，在系统面板里点「${n === 1 ? '存储图像' : `存储 ${n} 张图像`}」。`);
    sheet.append(el('div', 'grab'), head, el('p', 'lead', timeRange(photos)), form, go, foot);
    this.shell.append(scrim, sheet);
    this.sheet = [scrim, sheet];
    if (n === 1) {
      const photo = photos[0];
      const seq = ++this.sizeSeq;
      const side = buildSide(requestPhoto(photo), cardsFor(this.app.book(), photo));
      const useJpg = photo.jpg !== null && (!side.recipe || photo.dng === null);
      void this.core.sizes({ dng: photo.dng, jpg: photo.jpg, side, useJpg }).then((reply) => {
        if (!this.batch || this.batch.phase === 'closed') this.core.release();
        if (seq !== this.sizeSeq || !sheet.isConnected) return;
        if (!reply.ok || !reply.json) {
          sizeLine.textContent = SIZE_FAILED;
          return;
        }
        sized = JSON.parse(reply.json) as typeof sized;
        paintSize();
      });
    }
  }

  /** 清空照片：这一批停下，文件全丢，晚到的结果不会再到共享这一步。 */
  clear(): void {
    this.closeSheet();
    this.batch?.cancel();
    this.batch = null;
    this.removeDock();
  }

  private closeSheet(): void {
    this.sizeSeq += 1;
    for (const node of this.sheet) node.remove();
    this.sheet = [];
  }

  /** 点「存到相册」：把这几张、它们的文件句柄、版本和请求定下来。新的一批会放掉上一批还没交出去的文件。 */
  private start(photos: Photo[], options: { size: SizeTier; quality: QualityTier }): void {
    this.batch?.cancel();
    const hooks = dev();
    const book = this.app.book();
    const items: BatchItem[] = photos.map((photo) => ({
      id: photo.id,
      stem: photo.stem,
      version: photo.version,
      dng: photo.dng,
      jpg: photo.jpg,
      req: buildRequest(requestPhoto(photo), cardsFor(book, photo), options, __GXR_BUILD__, hooks?.binding),
    }));
    const pack = calibPack(book.pack?.files ?? null);
    this.core.begin({ dat: book.dat ? book.dat.read.bytes.slice(0) : null, calibIndex: pack.calibIndex, calibBlob: pack.calibBlob });
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
    };
    this.view = 'progress';
    this.chunk = 0;
    this.chunks = [];
    this.savedCount = 0;
    this.batch = new ExportBatch(items, host);
    this.batch.start();
  }

  private run(item: BatchItem, index: number): Promise<RunResult> {
    const trap = import.meta.env.DEV && dev()?.trapAt === index;
    return this.core.run({ dng: item.dng, jpg: item.jpg, req: item.req, ...(trap ? { devTrap: true } : {}) });
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
    this.dock?.remove();
    this.dock = null;
    this.shell.removeAttribute('data-export');
  }

  /** 贴底的那一栏，按批次的状态画。 */
  private paint(): void {
    const batch = this.batch;
    if (!batch || batch.phase === 'closed') {
      if (batch === this.batch) this.removeDock();
      return;
    }
    if (!this.dock) {
      this.dock = el('div', 'pdock');
      const perf = this.shell.querySelector('.perf');
      this.shell.insertBefore(this.dock, perf);
    }
    const dock = this.dock;
    dock.replaceChildren();
    this.shell.dataset.export = batch.phase;
    dock.dataset.phase = batch.phase;
    this.paintMemory(batch);
    if (batch.phase === 'running') {
      this.chunks = [];
      this.paintRunning(dock, batch);
      return;
    }
    if (this.view === 'saved') {
      this.paintSaved(dock, batch);
      return;
    }
    if (!this.chunks.length && batch.staged.length) {
      const limit = dev()?.chunkFiles ?? SHARE_MAX_FILES;
      this.chunks = shareChunks(
        batch.staged.map((s) => s.file.size),
        limit,
        SHARE_MAX_BYTES,
      );
      this.chunk = 0;
    }
    this.paintReady(dock, batch);
  }

  private paintMemory(batch: ExportBatch): void {
    const marks = this.core.marks;
    const mib = (v: number) => Math.round((v / 1048576) * 10) / 10;
    const data = { wasm: marks.wasm, js: marks.js, staged: batch.stagedHigh, workers: marks.workers, times: batch.times };
    this.shell.dataset.exportMem = JSON.stringify(data);
    if (this.perf && !this.perf.hidden) {
      const avg = batch.times.length ? Math.round(batch.times.reduce((a, b) => a + b, 0) / batch.times.length) : 0;
      this.perf.textContent = `导出 WASM ${mib(marks.wasm)} MiB · JS ${mib(marks.js)} MiB · 待存 ${mib(batch.stagedHigh)} MiB · 每张 ${avg} ms`;
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
      else if (state === 'ok' || state === 'fail') {
        const st = el('span', 'st');
        st.append(ico(state === 'ok' ? PATH.check : PATH.x));
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
    tx.append(el('b', 'num', `正在导出 ${current} / ${total}`), el('small', '', '可以接着看照片。'));
    const stop = button('fbtn fill-ctl', '停止');
    stop.addEventListener('click', () => batch.stop());
    top.append(el('span', 'spin'), tx, stop);
    const bar = el('div', 'bar');
    const fill = el('i');
    fill.style.width = `${total ? (done / total) * 100 : 0}%`;
    bar.append(fill);
    dock.append(top, bar, this.thumbs(batch));
  }

  private paintReady(dock: HTMLElement, batch: ExportBatch): void {
    const ok = batch.staged.length;
    const failed = [...batch.failures.values()];
    const top = el('div', 'top');
    const tx = el('div', 'tx');
    const icon = el('span', `ic ${failed.length ? 'warn' : 'ok'}`);
    icon.append(ico(failed.length ? PATH.alert : PATH.check));
    let main: string;
    if (batch.phase === 'stopped') main = `已停止。做好的 ${ok} 张还没存，点「存到相册」。`;
    else if (!failed.length) main = `${ok} 张已做好`;
    else if (!batch.okCount()) main = `${failed.length} 张都没做成`;
    else main = `做好 ${batch.okCount()} 张，${failed.length} 张没做成`;
    tx.append(el('b', '', main));
    if (failed.length) {
      const why = el('ul', 'why');
      for (const f of failed) why.append(el('li', '', `${f.stem}：${f.message}`));
      tx.append(why);
    }
    top.append(icon, tx);
    if (failed.length) {
      const label = !batch.okCount() ? '重试' : failed.length === 1 ? '重试这张' : `重试这 ${failed.length} 张`;
      const retry = button('fbtn fill-ctl', label);
      retry.dataset.act = 'retry';
      retry.addEventListener('click', () => {
        this.chunks = [];
        batch.retry();
      });
      top.append(retry);
    }
    const share = ok ? this.shareButton(batch, failed.length > 0 || batch.phase === 'stopped') : null;
    if (share && !failed.length && batch.phase !== 'stopped') top.append(share);
    dock.append(top);
    if (failed.length || batch.phase === 'stopped') dock.append(this.thumbs(batch));
    if (share && (failed.length || batch.phase === 'stopped')) dock.append(share);
    if (!ok && !failed.length) {
      const close = button('btn lg block', '好');
      close.addEventListener('click', () => batch.release());
      dock.append(close);
    }
    if (this.view === 'noshare') this.paintLinks(dock, batch);
  }

  /** 05c 的「存到相册」。分几批时写第几批；点下去在这次点击里同步调用 navigator.share。 */
  private shareButton(batch: ExportBatch, block: boolean): HTMLButtonElement {
    const files = this.chunkFiles(batch);
    const base = block ? `存 ${batch.staged.length} 张到相册` : '存到相册';
    const label = shareLabel(this.chunk, this.chunks.length, base);
    const btn = button(block ? 'btn primary lg block' : 'fbtn fill-acc', '');
    btn.dataset.act = 'share';
    btn.append(ico(PATH.export, 'md'), document.createTextNode(label));
    btn.addEventListener('click', () => this.share(batch, files));
    return btn;
  }

  private chunkFiles(batch: ExportBatch): Staged[] {
    const pick = this.chunks[this.chunk] ?? [];
    return pick.map((i) => batch.staged[i]).filter((s): s is Staged => !!s);
  }

  /** 必须在点击里同步调用，前面不能有 await：共享面板要这次点击的用户激活。 */
  private share(batch: ExportBatch, staged: Staged[]): void {
    const files = staged.map((s) => s.file);
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
        this.savedCount += files.length;
        if (this.chunk + 1 < this.chunks.length) {
          this.chunk += 1;
          this.paint();
          return;
        }
        this.view = 'saved';
        this.paint();
      },
      () => {
        // 关掉了面板（AbortError）或别的失败：留在 05c，文件还在。
      },
    );
  }

  private paintLinks(dock: HTMLElement, batch: ExportBatch): void {
    const box = el('div', 'links');
    box.dataset.state = 'no-share';
    box.append(el('p', 'dim', NO_SHARE));
    for (const s of batch.staged) {
      const a = el('a', '', s.file.name);
      a.href = URL.createObjectURL(s.file);
      a.download = s.file.name;
      box.append(a);
    }
    const done = button('btn lg block', '好');
    done.addEventListener('click', () => this.finishShare(batch));
    box.append(done);
    dock.append(box);
  }

  /** 05e：只知道面板在某个操作后关了，不知道是不是点了「存储图像」，所以不说「已存到相册」（机主决定 2）。 */
  private paintSaved(dock: HTMLElement, batch: ExportBatch): void {
    const n = this.savedCount;
    const top = el('div', 'top');
    const icon = el('span', 'ic ok');
    icon.append(ico(PATH.check));
    const tx = el('div', 'tx');
    tx.append(el('b', '', `好了：${n} 张`), el('small', '', `在面板里点了「${n === 1 ? '存储图像' : `存储 ${n} 张图像`}」，就已经在相册里。`));
    const done = button('fbtn fill-ctl', '好');
    done.dataset.act = 'done';
    done.addEventListener('click', () => this.finishShare(batch));
    top.append(icon, tx, done);
    dock.append(top);
  }

  private finishShare(batch: ExportBatch): void {
    for (const a of this.dock?.querySelectorAll<HTMLAnchorElement>('a[download]') ?? []) URL.revokeObjectURL(a.href);
    this.view = 'progress';
    this.savedCount = 0;
    this.chunks = [];
    this.chunk = 0;
    batch.release();
    this.paint();
  }
}
