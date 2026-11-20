// 就绪条与导出状态栏（独立浮在底栏上方）：单张/多张文案、第 N 张列表、折叠逐张附注，不改编辑器按钮本身。

import { hdrResultText, type HdrRow } from '../lib/hdr_text';
import { button, el } from './dom';
import type { Failure, Phase } from './export_batch';

export const NO_SHARE = '这个浏览器打不开系统的共享面板。';

export const EXPORT_ICON = {
  export: 'M12 15V3M7.5 7.5 12 3l4.5 4.5M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7',
  check: 'M5 12.5l4.5 4.5L19 7',
  info: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8h.01',
  x: 'M6 6l12 12M18 6 6 18',
  alert: 'M12 4 2.5 20h19L12 4zM12 10v4.5M12 17.5h.01',
} as const;

const NS = 'http://www.w3.org/2000/svg';

export function exportIco(d: string, cls = ''): SVGSVGElement {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', `ico ${cls}`.trim());
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(NS, 'path');
  path.setAttribute('d', d);
  svg.append(path);
  return svg;
}

export interface ReadyEntry {
  id: string;
  stem: string;
  file: File;
  position: number | null;
  hdrRequested: boolean;
  result?: Record<string, unknown>;
  cameraRecipeNote?: string;
}

/** 独立就绪条主按钮文案：单张「第 N 张做好了 · 存进相册」，多张「N 张做好了 · 存进相册」，多批时追加批号。 */
export function readyShareLabel(positions: ReadonlyArray<number | null>, chunk: number, totalChunks: number): string {
  const n = positions.length;
  const only = n === 1 ? positions[0] : null;
  const base = n === 1 && typeof only === 'number' && only > 0 ? `第 ${only} 张做好了 · 存进相册` : `${n} 张做好了 · 存进相册`;
  if (totalChunks <= 1) return base;
  return `${base}（第 ${chunk + 1} 批，共 ${totalChunks} 批）`;
}

/** 多张就绪时列出具体照片位置（「第 3 张、第 4 张」或「第 1、2、3、4 张等 6 张」）。 */
export function readyPositionsSummary(positions: ReadonlyArray<number | null>): string {
  const nums = positions.filter((p): p is number => typeof p === 'number' && p > 0);
  if (!nums.length) return '';
  if (nums.length === 1) return `第 ${nums[0]} 张`;
  if (nums.length <= 4) return nums.map((p) => `第 ${p} 张`).join('、');
  return `第 ${nums.slice(0, 4).join('、')} 张等 ${positions.length} 张`;
}

/** 汇总已做好条目的 HDR 摘要行与逐张附注（HDR 降级说明、缺失相机配方文件说明）。 */
export function collectReadyNotes(entries: readonly ReadyEntry[]): {
  hdrLine: string;
  hdrNotes: string[];
  camNotes: string[];
} {
  const hdrRows: HdrRow[] = [];
  const camNotes: string[] = [];
  for (const entry of entries) {
    if (entry.hdrRequested) hdrRows.push({ stem: entry.stem, result: entry.result });
    if (entry.cameraRecipeNote) camNotes.push(`${entry.stem}：${entry.cameraRecipeNote}`);
  }
  const { line, notes } = hdrRows.length ? hdrResultText(hdrRows) : { line: '', notes: [] };
  return { hdrLine: line, hdrNotes: notes, camNotes };
}

/** 失败 / 已停止条主标题文案；队列里还有待导出照片时追加「另排 N 张」。 */
export function readyBarSummary(phase: Phase, ok: number, failureCount: number, savedCount: number, queuedCount = 0): string {
  const made = ok > 0 || savedCount > 0;
  let main: string;
  if (phase === 'stopped') main = ok ? `已停止。做好的 ${ok} 张还没存，点「存进相册」。` : '已停止。';
  else if (!made) main = `${failureCount} 张都没做成`;
  else if (!ok) main = `${failureCount} 张没做成`;
  else main = `做好 ${ok} 张，${failureCount} 张没做成`;
  return queuedCount > 0 ? `${main}（另排 ${queuedCount} 张）` : main;
}

export interface ReadyBarRender {
  dock: HTMLElement;
  phase: Phase;
  entries: readonly ReadyEntry[];
  failures: readonly Failure[];
  savedCount: number;
  queuedCount?: number;
  chunk: number;
  totalChunks: number;
  detailsOpen: boolean;
  noShare: boolean;
  thumbs: HTMLElement | null;
  onShare: () => void;
  onRetry: () => void;
  onToggleDetails: () => void;
  onDismiss: () => void;
  onFinishNoShare: () => void;
  trackUrl: (url: string) => void;
}

function renderDetailsDrawer(
  dock: HTMLElement,
  hdrNotes: readonly string[],
  camNotes: readonly string[],
  open: boolean,
  posLine = '',
  hdrLine = '',
): void {
  if (!posLine && !hdrLine && !hdrNotes.length && !camNotes.length) return;
  const drawer = el('div', 'ready-details');
  drawer.dataset.state = 'ready-details';
  drawer.hidden = !open;
  if (posLine || hdrLine) {
    const meta = el('div', 'ready-meta');
    if (posLine) {
      const who = el('small', 'ready-pos', posLine);
      who.dataset.state = 'ready-positions';
      meta.append(who);
    }
    if (hdrLine) {
      const hl = el('small', 'hdrline', hdrLine);
      hl.dataset.state = 'hdr-line';
      meta.append(hl);
    }
    drawer.append(meta);
  }
  if (hdrNotes.length) {
    const list = el('ul', 'why hdrnotes');
    for (const note of hdrNotes) list.append(el('li', '', note));
    drawer.append(list);
  }
  if (camNotes.length) {
    const list = el('ul', 'why camnotes');
    list.dataset.state = 'camera-recipe-notes';
    for (const note of camNotes) list.append(el('li', '', note));
    drawer.append(list);
  }
  dock.append(drawer);
}

function renderNoShareLinks(
  dock: HTMLElement,
  entries: readonly ReadyEntry[],
  trackUrl: (url: string) => void,
  onDone: () => void,
): void {
  const box = el('div', 'links');
  box.dataset.state = 'no-share';
  box.append(el('p', 'dim', NO_SHARE));
  for (const entry of entries) {
    const a = el('a', '', entry.file.name);
    const url = URL.createObjectURL(entry.file);
    trackUrl(url);
    a.href = url;
    a.download = entry.file.name;
    box.append(a);
  }
  const done = button('btn lg block', '关闭');
  done.addEventListener('click', onDone);
  box.append(done);
  dock.append(box);
}

/** 渲染就绪条 / 失败条 / 已停止条（无「好」确认屏）。 */
export function paintReadyBar(opts: ReadyBarRender): void {
  const { dock, phase, entries, failures, savedCount, chunk, totalChunks, detailsOpen, noShare } = opts;
  const ok = entries.length;
  const positions = entries.map((e) => e.position);
  const { hdrLine, hdrNotes, camNotes } = collectReadyNotes(entries);
  const noteCount = hdrNotes.length + camNotes.length;
  const shareText = ok ? readyShareLabel(positions, chunk, totalChunks) : '';

  const makeShareBtn = (block: boolean): HTMLButtonElement => {
    const btn = button(block ? 'btn primary lg block ready-go' : 'fbtn fill-acc ready-go', '');
    btn.dataset.act = 'share';
    btn.append(exportIco(EXPORT_ICON.export, 'md'), document.createTextNode(shareText));
    btn.addEventListener('click', opts.onShare);
    return btn;
  };

  const makeDetailsBtn = (count: number): HTMLButtonElement => {
    const label = detailsOpen ? '收起' : count > 0 ? `详情 (${count})` : '详情';
    const btn = button('fbtn fill-ctl ready-more', label);
    btn.dataset.act = 'toggle-details';
    btn.setAttribute('aria-expanded', detailsOpen ? 'true' : 'false');
    btn.addEventListener('click', opts.onToggleDetails);
    return btn;
  };

  // 纯就绪态（无失败、非停止）：折叠时单行 <= 52pt 浮在底栏上方，详情（第 N 张列表、HDR 摘要、逐张附注）收入可展开抽屉。
  if (ok > 0 && !failures.length && phase !== 'stopped') {
    const posLine = entries.length > 1 ? readyPositionsSummary(positions) : '';
    const hasDetails = Boolean(posLine || hdrLine || noteCount > 0);
    dock.classList.toggle('ready-compact', !detailsOpen && !noShare);
    const row = el('div', 'ready-row');
    row.append(makeShareBtn(true));
    if (hasDetails) row.append(makeDetailsBtn(noteCount));
    dock.append(row);
    renderDetailsDrawer(dock, hdrNotes, camNotes, detailsOpen, posLine, hdrLine);
    if (noShare) renderNoShareLinks(dock, entries, opts.trackUrl, opts.onFinishNoShare);
    return;
  }

  // 有失败或已停止：保留失败详情、重试与保存已做好文件。
  const top = el('div', 'top');
  const tx = el('div', 'tx');
  const ic = el('span', `ic ${failures.length ? 'warn' : 'ok'}`);
  ic.append(exportIco(failures.length ? EXPORT_ICON.alert : EXPORT_ICON.check));
  const made = ok > 0 || savedCount > 0;
  const main = readyBarSummary(phase, ok, failures.length, savedCount, opts.queuedCount ?? 0);
  tx.append(el('b', '', main));
  if (ok > 1) {
    const posLine = readyPositionsSummary(positions);
    if (posLine) {
      const who = el('small', 'ready-pos', posLine);
      who.dataset.state = 'ready-positions';
      tx.append(who);
    }
  }
  if (hdrLine) {
    const hl = el('small', 'hdrline', hdrLine);
    hl.dataset.state = 'hdr-line';
    tx.append(hl);
  }
  if (failures.length) {
    const why = el('ul', 'why');
    for (const f of failures) why.append(el('li', '', `${f.stem}：${f.message}`));
    tx.append(why);
  }
  top.append(ic, tx);
  if (failures.length) {
    const label = !made ? '重试' : failures.length === 1 ? '重试这张' : `重试这 ${failures.length} 张`;
    const retry = button('fbtn fill-ctl', label);
    retry.dataset.act = 'retry';
    retry.addEventListener('click', opts.onRetry);
    top.append(retry);
  }
  if (!ok) {
    const close = button('fbtn fill-ctl', '关闭');
    close.dataset.act = 'dismiss';
    close.addEventListener('click', opts.onDismiss);
    top.append(close);
  }
  if (noteCount > 0) top.append(makeDetailsBtn(noteCount));
  dock.append(top);
  renderDetailsDrawer(dock, hdrNotes, camNotes, detailsOpen);
  if (opts.thumbs) dock.append(opts.thumbs);
  if (ok > 0) dock.append(makeShareBtn(true));
  if (noShare && ok > 0) renderNoShareLinks(dock, entries, opts.trackUrl, opts.onFinishNoShare);
}
