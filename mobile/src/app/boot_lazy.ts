// 启动首屏之后按需加载、壳 DOM 构建与空闲预热的辅助函数（纯拆分，不改变行为）。

import type { Engine } from '../../../frontend/src/index';
import type { EditorView } from './editor';
import type { ExportApp, ExportFlow } from './export_ui';
import type { GridView } from './grid';

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

  loadGrid(): Promise<typeof GridView> {
    if (this.gridCtor) return Promise.resolve(this.gridCtor);
    this.gridLoad ??= import('./grid')
      .then((m) => (this.gridCtor = m.GridView))
      .catch((err) => {
        this.gridLoad = null;
        throw err;
      });
    return this.gridLoad;
  }

  loadEditor(): Promise<typeof EditorView> {
    if (this.editorCtor) return Promise.resolve(this.editorCtor);
    this.editorLoad ??= import('./editor')
      .then((m) => (this.editorCtor = m.EditorView))
      .catch((err) => {
        this.editorLoad = null;
        throw err;
      });
    return this.editorLoad;
  }

  ensureExporter(shell: HTMLElement, app: ExportApp, perf: HTMLElement): Promise<ExportFlow> {
    if (this.exporterInst) return Promise.resolve(this.exporterInst);
    this.exporterLoad ??= import('./export_ui')
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

  syncDock(shell: HTMLElement): void {
    this.syncDockFn?.(shell);
  }
}
