// 把文件或文件夹拖进窗口（2026-11-26，电脑浏览器；iPad 分屏拖进来也走这里，手机没有文件拖放）。
// 带文件的拖动在每一屏都在 window 上取消 dragover / drop，浏览器就不会拿拖进来的照片换掉整个应用。
// 开始页和网格：松开后和「选照片」走同一个 take(files)。编辑器里不导入，只提示回到照片列表。
// 不带文件的拖动（文字、链接、页面自己的滑块）一概不管。

import { showToast } from './toast';

/** 拖进文件夹时最多往下走几层、最多收几个文件。到了上限就停下，提示一句。 */
export const DROP_MAX_DEPTH = 4;
export const DROP_MAX_FILES = 5000;
export const DROP_CAP_NOTE = `文件夹里的文件太多，只读了前 ${DROP_MAX_FILES} 个（最多往下 ${DROP_MAX_DEPTH} 层）`;
export const DROP_EDITOR_NOTE = '回到照片列表再拖进来';
export const DROP_HINT = '松开就能导入照片和相机配方文件';

/** `webkitGetAsEntry()` 给的条目里用得到的那几样（Node 单测用假的一棵树）。 */
export interface DropEntry {
  readonly name: string;
  readonly isFile: boolean;
  readonly isDirectory: boolean;
  file?(ok: (file: File) => void, fail?: (err: unknown) => void): void;
  createReader?(): { readEntries(ok: (entries: DropEntry[]) => void, fail?: (err: unknown) => void): void };
}

export interface Walked {
  files: File[];
  /** 文件太多或文件夹太深，有没读的。 */
  capped: boolean;
}

function readFile(entry: DropEntry): Promise<File | null> {
  return new Promise((done) => {
    if (!entry.file) return done(null);
    entry.file(done, () => done(null));
  });
}

/** 一个文件夹里的全部条目：readEntries 一次只给一批，读到空批为止。读不了就当空的。 */
async function readDir(entry: DropEntry): Promise<DropEntry[]> {
  const reader = entry.createReader?.();
  if (!reader) return [];
  const all: DropEntry[] = [];
  for (;;) {
    const batch = await new Promise<DropEntry[]>((done) => reader.readEntries(done, () => done([])));
    if (!batch.length) return all;
    all.push(...batch);
  }
}

/**
 * 拖进来的条目按顺序展开成文件。`.` 开头的（.Trashes、._R0001.JPG 之类）跳过。
 * 拖进来的那一层是第 0 层，文件夹里的东西最多到第 `maxDepth` 层；文件收满 `maxFiles` 个就停。
 */
export async function walkEntries(entries: readonly DropEntry[], maxDepth = DROP_MAX_DEPTH, maxFiles = DROP_MAX_FILES): Promise<Walked> {
  const files: File[] = [];
  let capped = false;
  const visit = async (list: readonly DropEntry[], depth: number): Promise<void> => {
    for (const entry of list) {
      if (files.length >= maxFiles) {
        capped = true;
        return;
      }
      if (entry.name.startsWith('.')) continue;
      if (entry.isFile) {
        const file = await readFile(entry);
        if (file) files.push(file);
      } else if (entry.isDirectory) {
        if (depth >= maxDepth) {
          capped = true;
          continue;
        }
        await visit(await readDir(entry), depth + 1);
      }
    }
  };
  await visit(entries, 0);
  return { files, capped };
}

function hasFiles(event: DragEvent): boolean {
  const types = event.dataTransfer?.types;
  return Boolean(types && Array.from(types).includes('Files'));
}

/** 松开那一刻同步取下条目（之后 DataTransfer 就空了），再慢慢展开文件夹。没有 webkitGetAsEntry 时只拿顶层文件。 */
function droppedFiles(data: DataTransfer): Promise<Walked> {
  const entries: DropEntry[] = [];
  const loose: File[] = [];
  for (const item of Array.from(data.items ?? [])) {
    if (item.kind !== 'file') continue;
    const entry = typeof item.webkitGetAsEntry === 'function' ? (item.webkitGetAsEntry() as DropEntry | null) : null;
    if (entry) entries.push(entry);
    else {
      const file = item.getAsFile();
      if (file && !file.name.startsWith('.')) loose.push(file);
    }
  }
  if (!entries.length && !loose.length) loose.push(...Array.from(data.files ?? []).filter((file) => !file.name.startsWith('.')));
  return walkEntries(entries).then((walked) => ({ files: [...loose, ...walked.files], capped: walked.capped }));
}

/** 在 window 上接住带文件的拖动。`take` 就是「选照片」那条路；屏幕看 shell 的 data-screen。 */
export function bindFileDrop(shell: HTMLElement, take: (files: File[]) => void): void {
  const overlay = document.createElement('div');
  overlay.className = 'filedrop';
  overlay.setAttribute('aria-hidden', 'true');
  overlay.textContent = DROP_HINT;
  shell.append(overlay);
  let leaveTimer = 0;
  const show = (on: boolean) => {
    window.clearTimeout(leaveTimer);
    leaveTimer = 0;
    shell.dataset.filedrop = on ? '1' : '0';
  };
  const takes = () => shell.dataset.screen === 'start' || shell.dataset.screen === 'grid';
  const over = (event: DragEvent) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
    show(takes());
  };
  window.addEventListener('dragenter', over);
  window.addEventListener('dragover', over);
  // 指针在页面里的元素之间移动也会有 dragleave：等一小会儿，没有接着的 dragover 才算真的离开了窗口。
  window.addEventListener('dragleave', (event) => {
    if (!hasFiles(event) || shell.dataset.filedrop !== '1') return;
    window.clearTimeout(leaveTimer);
    leaveTimer = window.setTimeout(() => show(false), 120);
  });
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && shell.dataset.filedrop === '1') show(false);
  });
  window.addEventListener('drop', (event) => {
    if (!hasFiles(event) || !event.dataTransfer) return;
    event.preventDefault();
    show(false);
    if (!takes()) {
      showToast(shell, DROP_EDITOR_NOTE);
      return;
    }
    droppedFiles(event.dataTransfer).then(
      ({ files, capped }) => {
        if (files.length && takes()) take(files); // 展开文件夹期间点开了照片：这次作废
        if (capped) showToast(shell, DROP_CAP_NOTE, 5000);
      },
      () => undefined,
    );
  });
}
