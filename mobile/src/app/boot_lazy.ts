// 首屏之后才加载的两个分包（网格、编辑器），它们加载失败时的提示，和开始页的壳 DOM。
// 分包只有 JS：样式全部随首屏加载（见 boot.ts 顶部）；分包也只静态依赖主包（构建时 vite.config.ts 检查）。
// 所以加载失败以后，换个查询串重新请求那一个文件，就能整份拿到。

export type ChunkKey = 'grid' | 'editor';

// 生产构建由 vite.config.ts 的 lazyChunkNames 换成两个分包相对主包的文件名；开发服务器和 Node 单测里保持占位串。
const CHUNK_FILES: string = '__GXR_LAZY_CHUNKS__';

function retryUrl(key: ChunkKey, attempt: number): string | null {
  if (!CHUNK_FILES.startsWith('{')) return null;
  const file = (JSON.parse(CHUNK_FILES) as Partial<Record<ChunkKey, string>>)[key];
  return file ? new URL(`${file}?retry=${attempt}`, import.meta.url).href : null;
}

/** 一个懒加载分包。同一时间只有一次加载在路上；成功一次以后一直用这一份模块，不再请求，也不会有第二份实例。 */
export class Chunk<T> {
  private readonly key: ChunkKey;
  private readonly first: () => Promise<T>;
  private mod: T | null = null;
  private task: Promise<T> | null = null;
  private attempts = 0;

  constructor(key: ChunkKey, first: () => Promise<T>) {
    this.key = key;
    this.first = first;
  }

  get loaded(): T | null {
    return this.mod;
  }

  load(): Promise<T> {
    if (this.mod) return Promise.resolve(this.mod);
    if (this.task) return this.task;
    // 浏览器记住模块地址的失败，同一个地址再 import 不会重新请求：第二次起换查询串。
    const url = this.attempts > 0 ? retryUrl(this.key, this.attempts) : null;
    this.attempts += 1;
    const task = (url ? (import(/* @vite-ignore */ url) as Promise<T>) : this.first()).then(
      (mod) => {
        this.mod = mod;
        this.task = null;
        return mod;
      },
      (err: unknown) => {
        this.task = null;
        throw err;
      },
    );
    this.task = task;
    return task;
  }
}

const CHUNK_ERROR_TITLE: Record<ChunkKey, string> = {
  grid: '照片页未能加载',
  editor: '编辑器未能加载',
};

export function clearChunkError(shell: HTMLElement): void {
  for (const node of shell.querySelectorAll('.load-err-scrim, .load-err-dlg')) node.remove();
}

/** 分包没拿到：「重试」重新请求，「刷新页面」整页重来。切屏、换照片、清空都会先把它关掉。 */
export function showChunkError(shell: HTMLElement, key: ChunkKey, onRetry: () => void): void {
  clearChunkError(shell);
  const scrim = document.createElement('div');
  scrim.className = 'scrim load-err-scrim';
  const dlg = document.createElement('div');
  dlg.className = 'dlg load-err-dlg';
  dlg.dataset.state = 'chunk-load-error';
  dlg.dataset.chunk = key;
  dlg.setAttribute('role', 'alertdialog');
  const heading = document.createElement('h2');
  heading.className = 'bad';
  heading.textContent = CHUNK_ERROR_TITLE[key];
  const msg = document.createElement('p');
  msg.setAttribute('role', 'alert');
  msg.textContent = '网络中断或资源暂时不可用，请重试；若仍打不开，请刷新页面。';
  const acts = document.createElement('div');
  acts.className = 'acts';
  const reload = document.createElement('button');
  reload.type = 'button';
  reload.className = 'btn lg';
  reload.dataset.act = 'reload-app';
  reload.textContent = '刷新页面';
  reload.addEventListener('click', () => location.reload());
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.className = 'btn primary lg';
  retry.dataset.act = 'retry-chunk';
  retry.textContent = '重试';
  retry.addEventListener('click', () => {
    clearChunkError(shell);
    onRetry();
  });
  acts.append(reload, retry);
  dlg.append(heading, msg, acts);
  scrim.addEventListener('click', () => clearChunkError(shell));
  shell.append(scrim, dlg);
}

/** 这一帧画出来以后再跑（两次 rAF，再让出一轮）。 */
export function afterFirstPaint(fn: () => void): void {
  requestAnimationFrame(() => requestAnimationFrame(() => window.setTimeout(fn, 0)));
}

export function scheduleIdle(fn: () => void): void {
  const win = window as unknown as { requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number };
  if (typeof win.requestIdleCallback === 'function') win.requestIdleCallback(fn, { timeout: 400 });
  else window.setTimeout(fn, 60);
}

export interface BootDom {
  shell: HTMLElement;
  view: HTMLElement;
  picker: HTMLInputElement;
  calibPicker: HTMLInputElement;
  lutPicker: HTMLInputElement;
  perf: HTMLElement;
}

function filePicker(className: string, multiple: boolean): HTMLInputElement {
  const input = document.createElement('input');
  input.type = 'file';
  input.multiple = multiple;
  input.className = className;
  return input;
}

export function createBootDom(root: HTMLElement): BootDom {
  const shell = document.createElement('div');
  shell.className = 'app';
  shell.dataset.screen = 'start';
  shell.dataset.drag = '0';
  // 没有 WebGPU 就不会有显卡引擎：先标上，不用等编辑器分包。
  if (!navigator.gpu) shell.dataset.gpu = '0';
  const picker = filePicker('filepick', true);
  // 校准数据 zip 单独一个选择框，也不加 accept（「文件」里按类型过滤会把文件变灰）。
  const calibPicker = filePicker('filepick calibpick', false);
  // .cube LUT 也不加 accept（iOS 「文件」按扩展名过滤会把 .cube 变灰），允许多选。
  const lutPicker = filePicker('lutpick', true);
  const view = document.createElement('div');
  view.className = 'view';
  const perf = document.createElement('p');
  perf.className = 'perf';
  perf.hidden = true;
  shell.append(picker, calibPicker, lutPicker, view, perf);
  root.replaceChildren(shell);
  return { shell, view, picker, calibPicker, lutPicker, perf };
}

export function bindPickers(dom: BootDom, app: { take(files: File[]): void; takeCalib(file: File): void; takeLuts(files: File[]): void }): void {
  dom.picker.addEventListener('change', () => {
    const files = dom.picker.files ? Array.from(dom.picker.files) : [];
    dom.picker.value = '';
    if (files.length) app.take(files);
  });
  dom.calibPicker.addEventListener('change', () => {
    const file = dom.calibPicker.files?.[0] ?? null;
    dom.calibPicker.value = '';
    if (file) app.takeCalib(file);
  });
  dom.lutPicker.addEventListener('change', () => {
    const files = dom.lutPicker.files ? Array.from(dom.lutPicker.files) : [];
    dom.lutPicker.value = '';
    if (files.length) app.takeLuts(files);
  });
}
