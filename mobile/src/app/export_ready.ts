// 44pt 顶槽导出条（与收起态 / 网格态 40pt 贴底条共用）：全部 5 种单行态渲染、VoiceOver 文案、250ms 拖动防误触。

import { hdrResultText, type HdrRow } from '../lib/hdr_text';
import { isComputer, readyWord, saveAria } from '../lib/save_mode';
import { isDragTapGuarded } from './drag_guard';
import { button, el } from './dom';
import type { Failure, Phase } from './export_batch';

export const NO_SHARE = '这个浏览器打不开系统的共享面板。';
/** 电脑上一次下载几个文件时，链接留着兜底（浏览器可能拦下后面几个）。 */
export const DOWNLOAD_BACKUP = '已经开始下载。哪个文件没下下来，点下面的文件名再下一次。';

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

/** 主按钮文案：单张「第 N 张做好了 · 存进相册」，多张「K 张做好了 · 存进相册」，多批时追加批号。电脑上是「下载」。 */
export function readyShareLabel(positions: ReadonlyArray<number | null>, chunk: number, totalChunks: number): string {
  const n = positions.length;
  const only = n === 1 ? positions[0] : null;
  const word = readyWord();
  const base = n === 1 && typeof only === 'number' && only > 0 ? `第 ${only} 张做好了 · ${word}` : `${n} 张做好了 · ${word}`;
  if (totalChunks <= 1) return base;
  return `${base}（第 ${chunk + 1} 批，共 ${totalChunks} 批）`;
}

/** 供 VoiceOver 区分「存进相册」（打开系统分享）与底栏「存到相册」（开始导出）。 */
export function readyShareAriaLabel(positions: ReadonlyArray<number | null>, chunk: number, totalChunks: number): string {
  const n = positions.length;
  const only = n === 1 ? positions[0] : null;
  const act = isComputer() ? '下载' : '存进相册，打开系统分享';
  const base = n === 1 && typeof only === 'number' && only > 0 ? `第 ${only} 张做好了，${act}` : `${n} 张做好了，${act}`;
  if (totalChunks <= 1) return base;
  return `${base}，第 ${chunk + 1} 批，共 ${totalChunks} 批`;
}

/** 运行中（0 张已做好）主胶囊文案。 */
export function runningBarLabel(current: number, total: number, hdr: boolean, queued: number): string {
  const base = total > 1 ? `正在导出 ${current} / ${total}` : '正在导出';
  const withHdr = hdr ? `${base} · HDR` : base;
  return queued > 0 ? `${withHdr}（另排 ${queued} 张）` : withHdr;
}

/** 全部失败（0 张已做好）主胶囊文案。 */
export function totalFailureLabel(
  failures: readonly Failure[],
  positionOf: (id: string) => number | null,
): string {
  if (failures.length === 1) {
    const pos = positionOf(failures[0].id);
    if (typeof pos === 'number' && pos > 0) return `第 ${pos} 张没做成`;
  }
  return `${failures.length} 张没做成`;
}

/** 多张就绪时列出具体照片位置（「第 3 张、第 4 张」或「第 1、2、3、4 张等 6 张」）；单张双联出多文件时注明文件数。 */
export function readyPositionsSummary(positions: ReadonlyArray<number | null>): string {
  const nums = positions.filter((p): p is number => typeof p === 'number' && p > 0).sort((a, b) => a - b);
  if (!nums.length) return '';
  const unique = [...new Set(nums)];
  const multiFile = positions.length > unique.length;
  if (unique.length === 1) return multiFile ? `第 ${unique[0]} 张（${positions.length} 个文件）` : `第 ${unique[0]} 张`;
  if (unique.length <= 4) {
    const listed = unique.map((p) => `第 ${p} 张`).join('、');
    return multiFile ? `${listed}（共 ${positions.length} 个文件）` : listed;
  }
  return `第 ${unique.slice(0, 4).join('、')} 张等 ${positions.length} ${multiFile ? '个文件' : '张'}`;
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

/** 失败 / 已停止汇总文案（供详情与单元测试使用）；队列里有待导出照片时追加「另排 N 张」。 */
export function readyBarSummary(phase: Phase, ok: number, failureCount: number, savedCount: number, queuedCount = 0): string {
  const made = ok > 0 || savedCount > 0;
  let main: string;
  if (phase === 'stopped') main = ok ? `已停止。做好的 ${ok} 张还没存，点「${readyWord()}」。` : '已停止。';
  else if (!made) main = `${failureCount} 张都没做成`;
  else if (!ok) main = `${failureCount} 张没做成`;
  else main = `做好 ${ok} 张，${failureCount} 张没做成`;
  return queuedCount > 0 ? `${main}（另排 ${queuedCount} 张）` : main;
}

/** VoiceOver 状态切换播报（仅在就绪 / 失败 / 停止状态转换时返回，运行中进度跳动返回 null）。 */
export function stateAnnouncement(
  phase: Phase,
  entries: readonly ReadyEntry[],
  failures: readonly Failure[],
  positionOf: (id: string) => number | null,
): { key: string; text: string } | null {
  if (phase === 'running') return null;
  const ok = entries.length;
  if (phase === 'stopped') {
    return {
      key: `stopped:${ok}`,
      text: ok > 0 ? `导出已停止，做好的 ${ok} 张还没存` : '导出已停止',
    };
  }
  if (ok > 0 && !failures.length) {
    const only = ok === 1 ? entries[0].position : null;
    const text =
      ok === 1 && typeof only === 'number' && only > 0
        ? `第 ${only} 张做好了，可${readyWord()}`
        : `${ok} 张做好了，可${readyWord()}`;
    return { key: `ready:${entries.map((e) => e.id).join(',')}`, text };
  }
  if (ok > 0 && failures.length > 0) {
    return {
      key: `partial:${ok}:${failures.map((f) => f.id).join(',')}`,
      text: `做好 ${ok} 张，${failures.length} 张没做成`,
    };
  }
  if (failures.length > 0) {
    return {
      key: `fail:${failures.map((f) => f.id).join(',')}`,
      text: totalFailureLabel(failures, positionOf),
    };
  }
  return null;
}

export interface RunningBarRender {
  dock: HTMLElement;
  done: number;
  total: number;
  current: number;
  hdr: boolean;
  queued: number;
  carried: readonly ReadyEntry[];
  carryChunk: number;
  carryTotalChunks: number;
  detailsOpen: boolean;
  onShareCarryover: () => void;
  onStop: () => void;
  onToggleDetails: () => void;
}

export interface ReadyBarRender {
  dock: HTMLElement;
  phase: Phase;
  entries: readonly ReadyEntry[];
  failures: readonly Failure[];
  failurePosition: (id: string) => number | null;
  chunk: number;
  totalChunks: number;
  detailsOpen: boolean;
  onShare: () => void;
  onRetry: () => void;
  onToggleDetails: () => void;
  onDismiss: () => void;
}

function guardedClick(btn: HTMLButtonElement, fn: () => void): void {
  btn.addEventListener('click', () => {
    if (isDragTapGuarded()) return;
    fn();
  });
}

function pillButton(btnCls: string, pillCls: string, act: string, onClick: () => void): {
  btn: HTMLButtonElement;
  pill: HTMLElement;
} {
  const btn = button(`es-btn ${btnCls}`, '');
  btn.dataset.act = act;
  const pill = el('span', `es-pill ${pillCls}`);
  btn.append(pill);
  guardedClick(btn, onClick);
  return { btn, pill };
}

/** 渲染运行态单行条：0 张完成显示「正在导出 + 停止」，已有完成显示「蓝色存进相册 + 导出中」。 */
export function paintRunningSlot(opts: RunningBarRender): void {
  const { dock, carried, detailsOpen } = opts;
  dock.dataset.visualState = carried.length ? 'running-ready' : 'running';

  if (carried.length) {
    const positions = carried.map((e) => e.position);
    const label = readyShareLabel(positions, opts.carryChunk, opts.carryTotalChunks);
    const main = pillButton('es-main', 'es-blue', 'share-ready', opts.onShareCarryover);
    main.btn.setAttribute('aria-label', readyShareAriaLabel(positions, opts.carryChunk, opts.carryTotalChunks));
    main.pill.append(exportIco(EXPORT_ICON.export, 'es-ico'), el('span', 'es-txt', label));

    const side = pillButton('es-side es-run-pill', 'es-dark', 'toggle-details', opts.onToggleDetails);
    side.btn.setAttribute('aria-expanded', detailsOpen ? 'true' : 'false');
    side.pill.append(el('span', 'es-spin'), el('span', 'es-txt', '导出中'));
    dock.append(main.btn, side.btn);
    return;
  }

  const label = runningBarLabel(opts.current, opts.total, opts.hdr, opts.queued);
  const main = pillButton('es-main', 'es-dark', 'toggle-details', opts.onToggleDetails);
  main.btn.setAttribute('aria-expanded', detailsOpen ? 'true' : 'false');
  main.pill.append(el('span', 'es-spin'), el('span', 'es-txt num', label));

  const stop = pillButton('es-side es-stop-pill', 'es-dark', 'stop', opts.onStop);
  stop.pill.append(el('span', 'es-txt', '停止'));
  dock.append(main.btn, stop.btn);
}

/** 渲染就绪 / 部分失败 / 全部失败 / 已停止单行条（永远 <= 44pt；详情由独立底部工作表承担）。 */
export function paintReadyBar(opts: ReadyBarRender): void {
  const { dock, phase, entries, failures, chunk, totalChunks, detailsOpen } = opts;
  const ok = entries.length;
  const positions = entries.map((e) => e.position);
  const { hdrNotes, camNotes } = collectReadyNotes(entries);
  const noteCount = hdrNotes.length + camNotes.length;

  // 有已做好成片（纯就绪 / 部分失败 / 已停止且有成片）：左侧蓝色直达「存进相册」，右侧「详情」或红框「M 张失败」。
  if (ok > 0) {
    const label = readyShareLabel(positions, chunk, totalChunks);
    const main = pillButton('es-main', 'es-blue', 'share', opts.onShare);
    main.btn.setAttribute('aria-label', readyShareAriaLabel(positions, chunk, totalChunks));
    main.pill.append(exportIco(EXPORT_ICON.export, 'es-ico'), el('span', 'es-txt', label));

    if (failures.length > 0) {
      dock.dataset.visualState = 'partial-fail';
      const side = pillButton('es-side es-fail-side', 'es-fail-outline', 'toggle-details', opts.onToggleDetails);
      side.btn.setAttribute('aria-expanded', detailsOpen ? 'true' : 'false');
      side.pill.append(el('span', 'es-txt num', `${failures.length} 张失败`));
      dock.append(main.btn, side.btn);
      return;
    }

    dock.dataset.visualState = phase === 'stopped' ? 'stopped-ready' : 'ready';
    const side = pillButton('es-side es-detail-side', 'es-dark', 'toggle-details', opts.onToggleDetails);
    side.btn.setAttribute('aria-expanded', detailsOpen ? 'true' : 'false');
    if (noteCount > 0) {
      side.pill.append(el('i', 'es-warn-dot'), el('span', 'es-txt num', `详情 (${noteCount})`));
    } else {
      side.pill.append(el('span', 'es-txt', '详情'));
    }
    dock.append(main.btn, side.btn);
    return;
  }

  // 0 张已做好（全部失败或已停止）：左侧红底（或暗底）条可点开详情，右侧「重试」+「×」。
  const hasFail = failures.length > 0;
  dock.dataset.visualState = hasFail ? 'total-fail' : 'stopped';
  const main = pillButton('es-main', hasFail ? 'es-danger' : 'es-dark', 'toggle-details', opts.onToggleDetails);
  main.btn.setAttribute('aria-expanded', detailsOpen ? 'true' : 'false');
  if (hasFail) {
    main.pill.append(
      exportIco(EXPORT_ICON.alert, 'es-ico es-alert-ico'),
      el('span', 'es-txt', totalFailureLabel(failures, opts.failurePosition)),
    );
  } else {
    main.pill.append(el('span', 'es-txt', '已停止'));
  }
  dock.append(main.btn);

  if (hasFail) {
    const retry = pillButton('es-side es-retry-side', 'es-dark', 'retry', opts.onRetry);
    retry.pill.append(el('span', 'es-txt', '重试'));
    dock.append(retry.btn);
  }

  const close = pillButton('es-icon', 'es-dark', 'dismiss', opts.onDismiss);
  close.btn.setAttribute('aria-label', '关闭');
  close.pill.append(exportIco(EXPORT_ICON.x, 'es-ico'));
  dock.append(close.btn);
}

/** 挂载导出条到展开单带的 44pt 顶槽，或收起态/网格态的 40pt 贴底槽；返回当前占据的 .band（若在顶槽）。 */
export function mountExportDock(
  shell: HTMLElement,
  dock: HTMLElement,
  prevBand: HTMLElement | null,
): HTMLElement | null {
  const editorOpen = shell.dataset.screen === 'editor';
  const slot = editorOpen
    ? shell.querySelector<HTMLElement>('.editor .band[data-collapsed="0"] .band-export-slot')
    : null;
  if (slot) {
    const band = slot.closest<HTMLElement>('.band');
    if (prevBand && prevBand !== band) prevBand.removeAttribute('data-export-slot');
    if (dock.parentElement !== slot) slot.append(dock);
    dock.dataset.dockMode = 'slot';
    if (band) {
      band.dataset.exportSlot = '1';
      return band;
    }
    return null;
  }
  prevBand?.removeAttribute('data-export-slot');
  const perf = shell.querySelector('.perf');
  if (dock.parentElement !== shell) shell.insertBefore(dock, perf);
  dock.dataset.dockMode = 'flush';
  return null;
}

export type PhotoExportState = 'idle' | 'busy' | 'ready';

const READY_BLUE_STATES = new Set(['ready', 'stopped-ready', 'partial-fail', 'running-ready']);

/** 就绪条显示蓝色「存进相册」时把底栏「存到相册」降为次级非蓝按钮，避免同屏两个相近蓝按钮。 */
export function syncExportSaveButton(
  shell: HTMLElement,
  dock: HTMLElement | null,
  photoState: (stem: string) => PhotoExportState,
): void {
  const btn = shell.querySelector<HTMLButtonElement>('.screen.editor .toolbar [data-act="export"]');
  if (!btn) return;
  const readyVisible = Boolean(dock?.isConnected && READY_BLUE_STATES.has(dock.dataset.visualState ?? ''));
  if (!readyVisible) {
    btn.classList.add('primary');
    btn.classList.remove('export-demoted', 'export-done');
    btn.setAttribute('aria-label', saveAria());
    btn.removeAttribute('data-export-state');
    return;
  }
  const stem = shell.querySelector<HTMLElement>('.screen.editor')?.dataset.photo ?? '';
  const state: PhotoExportState = stem ? photoState(stem) : 'idle';
  btn.classList.remove('primary');
  btn.classList.add('export-demoted');
  btn.classList.toggle('export-done', state === 'ready');
  if (state === 'ready') {
    btn.setAttribute('aria-label', saveAria('ready'));
    btn.dataset.exportState = 'ready-current';
  } else if (state === 'busy') {
    btn.setAttribute('aria-label', saveAria('busy'));
    btn.dataset.exportState = 'running-current';
  } else {
    btn.setAttribute('aria-label', saveAria());
    btn.dataset.exportState = 'ready-other';
  }
}

/** 更新导出内存与耗时观测数据到壳属性及性能条。 */
export function paintExportMemory(
  shell: HTMLElement,
  perf: HTMLElement | null,
  marks: { wasm: number; js: number; workers: number },
  stagedHigh: number,
  times: readonly number[],
): void {
  const mib = (v: number) => Math.round((v / 1048576) * 10) / 10;
  const data = { wasm: marks.wasm, js: marks.js, staged: stagedHigh, workers: marks.workers, times };
  shell.dataset.exportMem = JSON.stringify(data);
  if (perf && !perf.hidden) {
    const avg = times.length ? `${Math.round(times.reduce((a, b) => a + b, 0) / times.length)} ms` : '—';
    perf.textContent = `导出 WASM ${mib(marks.wasm)} MiB · JS ${mib(marks.js)} MiB · 待存 ${mib(stagedHigh)} MiB · 每张 ${avg}`;
  }
}
