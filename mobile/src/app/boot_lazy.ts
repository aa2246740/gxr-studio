// 启动首屏之后按需加载、壳 DOM 构建与空闲预热的辅助函数（纯拆分，不改变行为）。

import type { Engine } from '../../../frontend/src/index';
import type { EditorView } from './editor';
import type { ExportApp, ExportFlow } from './export_ui';
import type { GridView } from './grid';

export type LazyChunkKey = 'grid' | 'editor' | 'export_ui' | 'off_image_sheet';

interface ChunkEntry {
  js: string;
  css: string[];
}

// 生产构建由 vite.config.ts 的 lazyChunkMap 替换为真实分包映射；dev 或 Node 单测下保持占位串。
const RAW_LAZY_CHUNKS: string = '__GXR_LAZY_CHUNKS__';
let parsedLazyChunks: Record<string, ChunkEntry> | null | undefined;
const resolvedChunks = new Map<LazyChunkKey, unknown>();
const inflightChunks = new Map<LazyChunkKey, Promise<unknown>>();
const failedChunks = new Set<LazyChunkKey>();
let retrySeq = 0;

function prodChunkEntry(key: LazyChunkKey): ChunkEntry | null {
  if (parsedLazyChunks === undefined) {
    try {
      parsedLazyChunks = RAW_LAZY_CHUNKS.startsWith('{') ? (JSON.parse(RAW_LAZY_CHUNKS) as Record<string, ChunkEntry>) : null;
    } catch {
      parsedLazyChunks = null;
    }
  }
  return parsedLazyChunks?.[key] ?? null;
}

function linkMatchesCss(link: HTMLLinkElement, file: string): boolean {
  const raw = link.getAttribute('href') || link.href || '';
  if (!raw) return false;
  try {
    const pathname = new URL(raw, location.href).pathname;
    return pathname === `/${file}` || pathname.endsWith(`/${file}`);
  } catch {
    return raw.includes(file);
  }
}

function hasLoadedStylesheet(link: HTMLLinkElement): boolean {
  if (!link.sheet) return false;
  try {
    return link.sheet.cssRules.length > 0;
  } catch {
    // Chromium 对加载失败的同源 <link rel="stylesheet"> 仍会挂上只读抛 SecurityError 的占位 sheet，不能误判为已加载。
    try {
      return new URL(link.href, location.href).origin !== location.origin;
    } catch {
      return false;
    }
  }
}

async function ensureStyles(files: string[], baseUrl: URL): Promise<void> {
  if (typeof document === 'undefined' || !files.length) return;
  const tasks: Array<Promise<void>> = [];
  for (const file of files) {
    const existing = [...document.querySelectorAll<HTMLLinkElement>('link[rel="stylesheet"]')].filter((link) =>
      linkMatchesCss(link, file),
    );
    if (existing.some(hasLoadedStylesheet)) continue;
    const hadStale = existing.length > 0;
    for (const stale of existing) stale.remove();
    const target = new URL(file, baseUrl);
    if (hadStale) {
      retrySeq += 1;
      target.searchParams.set('retry', String(retrySeq));
    }
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = target.href;
    tasks.push(
      new Promise<void>((resolve, reject) => {
        link.addEventListener('load', () => resolve(), { once: true });
        link.addEventListener(
          'error',
          () => {
            link.remove();
            reject(new Error(`样式加载失败：${file}`));
          },
          { once: true },
        );
      }),
    );
    document.head.append(link);
  }
  if (tasks.length) await Promise.all(tasks);
}

/**
 * 懒加载分包：并发去重并在首次成功后永久复用同一模块实例（无论来自首次加载还是重试）；
 * 重试时先恢复缺失样式并优先尝试原 URL（处理仅样式失败而 JS 已落定的情况），仅原 URL 失败才换 `?retry=N`。
 */
export async function importChunk<T>(key: LazyChunkKey, primary: () => Promise<T>, devPath: string): Promise<T> {
  if (resolvedChunks.has(key)) return resolvedChunks.get(key) as T;
  const inflight = inflightChunks.get(key);
  if (inflight) return inflight as Promise<T>;
  const task = (async (): Promise<T> => {
    const baseUrl = typeof location !== 'undefined' ? new URL(import.meta.env.BASE_URL || '/', location.href) : null;
    const entry = import.meta.env.DEV ? { js: devPath, css: [] } : prodChunkEntry(key);
    let mod: T;
    if (!failedChunks.has(key)) {
      try {
        mod = await primary();
        // Vite 8 预加载在仅 CSS 失败时可能吞错并返回 JS 模块；落定前显式核对样式表并在缺失时重拉。
        if (entry && baseUrl) await ensureStyles(entry.css, baseUrl);
      } catch (err) {
        failedChunks.add(key);
        throw err;
      }
    } else if (!entry || !baseUrl) {
      mod = await primary();
    } else {
      await ensureStyles(entry.css, baseUrl);
      const baseTarget = new URL(entry.js, baseUrl);
      try {
        mod = (await import(/* @vite-ignore */ baseTarget.href)) as T;
      } catch {
        retrySeq += 1;
        const retryTarget = new URL(entry.js, baseUrl);
        retryTarget.searchParams.set('retry', String(retrySeq));
        mod = (await import(/* @vite-ignore */ retryTarget.href)) as T;
      }
    }
    resolvedChunks.set(key, mod);
    return mod;
  })();
  inflightChunks.set(key, task);
  try {
    return await task;
  } finally {
    if (inflightChunks.get(key) === task) inflightChunks.delete(key);
  }
}

export function clearChunkLoadError(shell: HTMLElement): void {
  for (const node of shell.querySelectorAll('.load-err-scrim, .load-err-dlg')) node.remove();
}

const CHUNK_ERROR_COPY: Record<'grid' | 'editor' | 'export', { title: string; message: string; retryAct: string }> = {
  grid: {
    title: '照片页未能加载',
    message: '网络中断或资源暂时不可用，请重试；若仍打不开，请刷新页面。',
    retryAct: 'retry-grid',
  },
  editor: {
    title: '编辑器未能加载',
    message: '网络中断或资源暂时不可用，请重试；若仍打不开，请刷新页面。',
    retryAct: 'retry-editor',
  },
  export: {
    title: '导出模块未能加载',
    message: '网络中断或资源暂时不可用，请重试；若仍打不开，请刷新页面。',
    retryAct: 'retry-export',
  },
};

export function showChunkLoadError(shell: HTMLElement, kind: 'grid' | 'editor' | 'export', onRetry: () => void): void {
  const opts = CHUNK_ERROR_COPY[kind];
  clearChunkLoadError(shell);
  const scrim = document.createElement('div');
  scrim.className = 'scrim load-err-scrim';
  const dlg = document.createElement('div');
  dlg.className = 'dlg load-err-dlg';
  dlg.dataset.state = 'chunk-load-error';
  dlg.setAttribute('role', 'alertdialog');
  const heading = document.createElement('h2');
  heading.className = 'bad';
  heading.textContent = opts.title;
  const msg = document.createElement('p');
  msg.setAttribute('role', 'alert');
  msg.textContent = opts.message;
  const acts = document.createElement('div');
  acts.className = 'acts';
  const reloadBtn = document.createElement('button');
  reloadBtn.type = 'button';
  reloadBtn.className = 'btn lg';
  reloadBtn.dataset.act = 'reload-app';
  reloadBtn.textContent = '刷新页面';
  reloadBtn.addEventListener('click', () => location.reload());
  const retryBtn = document.createElement('button');
  retryBtn.type = 'button';
  retryBtn.className = 'btn primary lg';
  retryBtn.dataset.act = opts.retryAct;
  retryBtn.textContent = '重试';
  retryBtn.addEventListener('click', () => {
    clearChunkLoadError(shell);
    onRetry();
  });
  acts.append(reloadBtn, retryBtn);
  dlg.append(heading, msg, acts);
  scrim.addEventListener('click', () => clearChunkLoadError(shell));
  shell.append(scrim, dlg);
}

export function afterFirstPaint(fn: () => void): void {
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      window.setTimeout(fn, 0);
    });
  });
}

export function scheduleIdle(fn: () => void): void {
  const win = window as unknown as { requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number };
  if (typeof win.requestIdleCallback === 'function') win.requestIdleCallback(fn, { timeout: 400 });
  else window.setTimeout(fn, 60);
}

/** 延迟一段静默时间后再进入空闲回调，用于无缩略图纯 RAW 导入落定后的兜底预热。 */
export function scheduleSettledIdle(delayMs: number, fn: () => void): () => void {
  let cancelled = false;
  const timer = window.setTimeout(() => {
    if (cancelled) return;
    scheduleIdle(() => {
      if (!cancelled) fn();
    });
  }, delayMs);
  return () => {
    cancelled = true;
    window.clearTimeout(timer);
  };
}

function makeInput(className: string, multiple: boolean): HTMLInputElement {
  const input = document.createElement('input');
  input.type = 'file';
  input.multiple = multiple;
  input.className = className;
  return input;
}

export interface BootDom {
  shell: HTMLElement;
  view: HTMLElement;
  picker: HTMLInputElement;
  calibPicker: HTMLInputElement;
  lutPicker: HTMLInputElement;
  perf: HTMLElement;
}

export function createBootDom(root: HTMLElement): BootDom {
  const shell = document.createElement('div');
  shell.className = 'app';
  shell.dataset.screen = 'start';
  shell.dataset.drag = '0';
  if (!('gpu' in navigator) || !navigator.gpu) shell.dataset.gpu = '0';
  // 校准数据与 .cube LUT 都不加 accept（iOS 「文件」按扩展名过滤会把文件变灰）。
  const picker = makeInput('filepick', true);
  const calibPicker = makeInput('filepick calibpick', false);
  const lutPicker = makeInput('lutpick', true);
  const view = document.createElement('div');
  view.className = 'view';
  const perf = document.createElement('p');
  perf.className = 'perf';
  perf.hidden = true;
  shell.append(picker, calibPicker, lutPicker, view, perf);
  root.replaceChildren(shell);
  return { shell, view, picker, calibPicker, lutPicker, perf };
}

export function bindPickers(
  dom: BootDom,
  handlers: { take(files: File[]): void; takeCalib(file: File): void; takeLuts(files: File[]): void },
): void {
  dom.picker.addEventListener('change', () => {
    const files = dom.picker.files ? Array.from(dom.picker.files) : [];
    dom.picker.value = '';
    if (files.length) handlers.take(files);
  });
  dom.calibPicker.addEventListener('change', () => {
    const file = dom.calibPicker.files?.[0] ?? null;
    dom.calibPicker.value = '';
    if (file) handlers.takeCalib(file);
  });
  dom.lutPicker.addEventListener('change', () => {
    const files = dom.lutPicker.files ? Array.from(dom.lutPicker.files) : [];
    dom.lutPicker.value = '';
    if (files.length) handlers.takeLuts(files);
  });
}

export async function createGpuEngine(
  shell: HTMLElement,
  reportError: (err: unknown) => void,
): Promise<Engine | null> {
  if (!('gpu' in navigator) || !navigator.gpu) return null;
  try {
    const [{ Engine }, { neutralBake, NEUTRAL_BAKE_KEY }] = await Promise.all([
      import('../../../frontend/src/index'),
      import('../lib/neutral'),
    ]);
    const engine = await Engine.create();
    if (engine) {
      const bake = neutralBake();
      engine.addBake(NEUTRAL_BAKE_KEY, bake.meta, bake.data);
      engine.onFrame = () => {
        shell.dataset.frame = '1';
      };
    }
    return engine;
  } catch (err) {
    reportError(err);
    return null;
  }
}

export class LazyModules {
  gridCtor: (typeof GridView) | null = null;
  editorCtor: (typeof EditorView) | null = null;
  exporterInst: ExportFlow | null = null;
  private gridLoad: Promise<typeof GridView> | null = null;
  private editorLoad: Promise<typeof EditorView> | null = null;
  private exporterLoad: Promise<ExportFlow> | null = null;
  private syncDockFn: ((shell: HTMLElement) => void) | null = null;
  private editorWarmed = false;
  private cancelRawWarm: (() => void) | null = null;

  cancelPendingWarm(): void {
    this.cancelRawWarm?.();
    this.cancelRawWarm = null;
  }

  /** 等可见缩略图绘出（且首批最多 8 张完成）后再预热编辑与导出分包；无预览纯 RAW 仅在全部落定并空闲后兜底预热。 */
  maybeWarmAfterThumbs(opts: {
    active: () => boolean;
    loading: () => { done: number; total: number };
    hasDrawnThumb: () => boolean;
    hasOpenable: () => boolean;
    warmExporter: () => Promise<ExportFlow>;
  }): void {
    if (this.editorWarmed || !opts.active()) return;
    const { done, total } = opts.loading();
    if (!total || done < Math.min(total, 8)) {
      this.cancelPendingWarm();
      return;
    }
    const triggerWarm = () => {
      if (this.editorWarmed) return;
      this.cancelPendingWarm();
      this.editorWarmed = true;
      afterFirstPaint(() => {
        void this.loadEditor().catch(() => undefined);
        scheduleIdle(() => {
          void opts.warmExporter().catch(() => undefined);
        });
      });
    };
    if (opts.hasDrawnThumb()) {
      triggerWarm();
      return;
    }
    if (done < total) {
      this.cancelPendingWarm();
      return;
    }
    if (this.cancelRawWarm || !opts.hasOpenable()) return;
    this.cancelRawWarm = scheduleSettledIdle(1500, () => {
      this.cancelRawWarm = null;
      const cur = opts.loading();
      if (opts.active() && cur.total > 0 && cur.done >= cur.total) triggerWarm();
    });
  }

  loadGrid(): Promise<typeof GridView> {
    if (this.gridCtor) return Promise.resolve(this.gridCtor);
    this.gridLoad ??= importChunk('grid', () => import('./grid'), 'src/app/grid.ts')
      .then((m) => (this.gridCtor = m.GridView))
      .catch((err) => {
        this.gridLoad = null;
        throw err;
      });
    return this.gridLoad;
  }

  loadEditor(): Promise<typeof EditorView> {
    if (this.editorCtor) return Promise.resolve(this.editorCtor);
    this.editorLoad ??= importChunk('editor', () => import('./editor'), 'src/app/editor.ts')
      .then((m) => (this.editorCtor = m.EditorView))
      .catch((err) => {
        this.editorLoad = null;
        throw err;
      });
    return this.editorLoad;
  }

  ensureExporter(shell: HTMLElement, app: ExportApp, perf: HTMLElement): Promise<ExportFlow> {
    if (this.exporterInst) return Promise.resolve(this.exporterInst);
    this.exporterLoad ??= importChunk('export_ui', () => import('./export_ui'), 'src/app/export_ui.ts')
      .then((m) => {
        this.syncDockFn = m.syncExportDock;
        this.exporterInst ??= new m.ExportFlow(shell, app, perf);
        return this.exporterInst;
      })
      .catch((err) => {
        this.exporterLoad = null;
        throw err;
      });
    return this.exporterLoad;
  }

  bindShell(shell: HTMLElement, app: ExportApp, perf: HTMLElement): void {
    boundShells.set(shell, { lazy: this, app, perf });
  }

  syncDock(shell: HTMLElement): void {
    this.syncDockFn?.(shell);
  }
}

interface BoundShell {
  lazy: LazyModules;
  app: ExportApp;
  perf: HTMLElement;
}

const boundShells = new WeakMap<HTMLElement, BoundShell>();

/** 编辑器与菜单共用同一导出模块与同一个 ExportFlow 实例，避免 editor 静态依赖 export_ui 导致单次失败卡死。 */
export function syncExportDock(shell: HTMLElement): void {
  boundShells.get(shell)?.lazy.syncDock(shell);
}

export async function loadExportOptionsOpener(
  shell: HTMLElement,
): Promise<(ids: string[], onStart?: () => void) => boolean> {
  const bound = boundShells.get(shell);
  if (bound) {
    const flow = await bound.lazy.ensureExporter(shell, bound.app, bound.perf);
    return (ids, onStart) => flow.openOptions(ids, onStart);
  }
  const mod = await importChunk('export_ui', () => import('./export_ui'), 'src/app/export_ui.ts');
  return (ids, onStart) => mod.openExportOptions(shell, ids, onStart);
}

export function openExportOptions(shell: HTMLElement, ids: string[], onStart?: () => void): boolean {
  const bound = boundShells.get(shell);
  if (!bound) return false;
  if (bound.lazy.exporterInst) return bound.lazy.exporterInst.openOptions(ids, onStart);
  const session = bound.app.session();
  void bound.lazy
    .ensureExporter(shell, bound.app, bound.perf)
    .then((flow) => { if (session === bound.app.session()) flow.openOptions(ids, onStart); })
    .catch(() => showChunkLoadError(shell, 'export', () => { if (session === bound.app.session()) openExportOptions(shell, ids, onStart); }));
  return true;
}

export function startQuickExport(shell: HTMLElement, ids: string[], onStart?: () => void): boolean {
  const bound = boundShells.get(shell);
  if (!bound) return false;
  if (bound.lazy.exporterInst) return bound.lazy.exporterInst.startQuick(ids, onStart);
  const session = bound.app.session();
  void bound.lazy
    .ensureExporter(shell, bound.app, bound.perf)
    .then((flow) => { if (session === bound.app.session()) flow.startQuick(ids, onStart); })
    .catch(() => showChunkLoadError(shell, 'export', () => { if (session === bound.app.session()) startQuickExport(shell, ids, onStart); }));
  return true;
}
