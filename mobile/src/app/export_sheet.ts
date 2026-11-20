// 导出「详情」底部工作表：压在 56pt 底栏上方（最高 240pt，0px 不改照片矩形），模态对话框 + 遮罩点关 + 焦点围栏 + 直达「存进相册」。

import { HDR_PROGRESS } from '../lib/hdr_text';
import { button, el } from './dom';
import type { Failure, Phase } from './export_batch';
import {
  collectReadyNotes,
  EXPORT_ICON,
  exportIco,
  NO_SHARE,
  readyBarSummary,
  readyPositionsSummary,
  readyShareAriaLabel,
  readyShareLabel,
  type ReadyEntry,
} from './export_ready';
import { registerSheet } from './sheet_lifecycle';

export interface ExportSheetData {
  phase: Phase;
  running: {
    done: number;
    total: number;
    current: number;
    hdr: boolean;
    queued: number;
  } | null;
  entries: readonly ReadyEntry[];
  failures: readonly Failure[];
  failurePosition: (id: string) => number | null;
  chunk: number;
  totalChunks: number;
  noShare: boolean;
  thumbs: HTMLElement | null;
  onShare: () => void;
  onStop: () => void;
  onRetry: () => void;
  onClearFailures: () => void;
  onFinishNoShare: () => void;
  onClose: () => void;
  trackUrl: (url: string) => void;
}

const FOCUSABLE = 'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';

export class ExportDetailsSheet {
  private scrim: HTMLElement | null = null;
  private sheet: HTMLElement | null = null;
  private body: HTMLElement | null = null;
  private foot: HTMLElement | null = null;
  private unregister: (() => void) | null = null;
  private onKey: ((ev: KeyboardEvent) => void) | null = null;
  private onCloseCb: (() => void) | null = null;
  private returnFocus: HTMLElement | null = null;

  constructor(private readonly shell: HTMLElement) {}

  isOpen(): boolean {
    return this.sheet !== null && this.sheet.isConnected;
  }

  /** 打开或原地更新「详情」工作表（进度刷新时不抢焦点）。 */
  render(data: ExportSheetData): void {
    this.onCloseCb = data.onClose;
    const freshlyOpened = !this.isOpen();
    if (freshlyOpened) {
      this.returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      const scrim = el('div', 'scrim export-details-scrim');
      const sheet = el('div', 'ms export-sheet');
      sheet.dataset.state = 'export-details';
      sheet.setAttribute('role', 'dialog');
      sheet.setAttribute('aria-modal', 'true');
      sheet.setAttribute('aria-label', '导出队列');
      sheet.tabIndex = -1;

      const hd = el('div', 'es-sheet-hd');
      hd.append(el('h2', '', '导出队列'));
      const closeBtn = button('nb es-sheet-close', '收起');
      closeBtn.dataset.act = 'close-details';
      closeBtn.addEventListener('click', () => this.requestClose());
      hd.append(closeBtn);

      const body = el('div', 'es-sheet-body');
      const foot = el('div', 'es-sheet-foot');
      sheet.append(hd, body, foot);

      scrim.addEventListener('click', () => this.requestClose());
      this.onKey = (ev: KeyboardEvent) => this.handleKeydown(ev);
      document.addEventListener('keydown', this.onKey);
      this.unregister = registerSheet(this.shell, () => this.requestClose());

      this.scrim = scrim;
      this.sheet = sheet;
      this.body = body;
      this.foot = foot;
      this.shell.append(scrim, sheet);
    }

    if (!this.body || !this.foot || !this.sheet) return;
    this.body.replaceChildren();
    this.foot.replaceChildren();

    const { running, entries, failures, noShare } = data;
    const { hdrLine, hdrNotes, camNotes } = collectReadyNotes(entries);

    // 1) 运行中进度 + HDR 时长说明 + 停止
    if (running) {
      const sec = el('div', 'es-sec es-sec-run');
      const top = el('div', 'es-run-top');
      const titleText = running.queued
        ? `正在导出 ${running.current} / ${running.total}（另排 ${running.queued} 张）`
        : `正在导出 ${running.current} / ${running.total}`;
      const tx = el('div', 'es-run-tx');
      tx.append(el('b', 'num', titleText), el('small', '', running.hdr ? HDR_PROGRESS : '可以接着看照片。'));
      const stopBtn = button('fbtn fill-ctl es-sheet-stop', '停止');
      stopBtn.dataset.act = 'stop';
      stopBtn.addEventListener('click', data.onStop);
      top.append(el('span', 'es-spin'), tx, stopBtn);
      const bar = el('div', 'bar');
      const fill = el('i');
      fill.style.width = `${running.total ? (running.done / running.total) * 100 : 0}%`;
      bar.append(fill);
      sec.append(top, bar);
      this.body.append(sec);
    }

    // 2) 已做好列表与 SDR / HDR / 相机配方附注
    const posLine = entries.length ? readyPositionsSummary(entries.map((e) => e.position)) : '';
    if (posLine || hdrLine || hdrNotes.length || camNotes.length) {
      const sec = el('div', 'es-sec es-sec-notes');
      if (posLine || hdrLine) {
        const meta = el('div', 'ready-meta');
        if (posLine) {
          const who = el('small', 'ready-pos', entries.length > 1 ? `已做好：${posLine}` : `已做好：${posLine}`);
          who.dataset.state = 'ready-positions';
          meta.append(who);
        }
        if (hdrLine) {
          const hl = el('small', 'hdrline', hdrLine);
          hl.dataset.state = 'hdr-line';
          meta.append(hl);
        }
        sec.append(meta);
      }
      if (hdrNotes.length) {
        const list = el('ul', 'why hdrnotes');
        for (const note of hdrNotes) list.append(el('li', '', note));
        sec.append(list);
      }
      if (camNotes.length) {
        const list = el('ul', 'why camnotes');
        list.dataset.state = 'camera-recipe-notes';
        for (const note of camNotes) list.append(el('li', '', note));
        sec.append(list);
      }
      this.body.append(sec);
    }

    // 3) 缩略图（运行中或存在失败/已停止时展示）
    if (data.thumbs) {
      this.body.append(data.thumbs);
    }

    // 4) 失败项与 重试 / 清除（或已停止提示）
    if (failures.length) {
      const sec = el('div', 'es-sec es-sec-fail');
      sec.append(el('p', 'dim small es-summary', readyBarSummary(data.phase, entries.length, failures.length, 0, running?.queued ?? 0)));
      const list = el('ul', 'why failnotes');
      list.dataset.state = 'failure-notes';
      for (const f of failures) {
        const pos = data.failurePosition(f.id);
        const prefix = pos ? `第 ${pos} 张` : f.stem;
        list.append(el('li', '', `${prefix}：${f.message}`));
      }
      const acts = el('div', 'es-fail-acts');
      const retry = button('fbtn fill-ctl', failures.length === 1 ? '重试这张' : `重试这 ${failures.length} 张`);
      retry.dataset.act = 'retry';
      retry.addEventListener('click', data.onRetry);
      const clear = button('fbtn fill-ctl es-clear-btn', '清除');
      clear.dataset.act = 'dismiss';
      clear.addEventListener('click', data.onClearFailures);
      acts.append(retry, clear);
      sec.append(list, acts);
      this.body.append(sec);
    } else if (data.phase === 'stopped') {
      const sec = el('div', 'es-sec');
      sec.append(el('p', 'dim small es-summary', readyBarSummary(data.phase, entries.length, 0, 0, running?.queued ?? 0)));
      this.body.append(sec);
    }

    // 5) 无系统共享面板时的下载链接兜底
    if (noShare && entries.length) {
      const box = el('div', 'links');
      box.dataset.state = 'no-share';
      box.append(el('p', 'dim small', NO_SHARE));
      for (const entry of entries) {
        const a = el('a', '', entry.file.name);
        const url = URL.createObjectURL(entry.file);
        data.trackUrl(url);
        a.href = url;
        a.download = entry.file.name;
        box.append(a);
      }
      const done = button('btn block', '关闭');
      done.addEventListener('click', data.onFinishNoShare);
      box.append(done);
      this.body.append(box);
    }

    // 6) 底部直达「存进相册」按钮（同步触发 navigator.share，保持用户激活）
    if (entries.length && !noShare) {
      const positions = entries.map((e) => e.position);
      const label = readyShareLabel(positions, data.chunk, data.totalChunks);
      const cta = button('btn primary block es-sheet-cta', '');
      cta.dataset.act = running ? 'share-ready' : 'share';
      cta.setAttribute('aria-label', readyShareAriaLabel(positions, data.chunk, data.totalChunks));
      cta.append(exportIco(EXPORT_ICON.export, 'md'), document.createTextNode(label));
      cta.addEventListener('click', data.onShare);
      this.foot.hidden = false;
      this.foot.append(cta);
    } else {
      this.foot.hidden = true;
    }

    if (freshlyOpened) {
      const first = this.sheet.querySelector<HTMLElement>(FOCUSABLE) ?? this.sheet;
      first.focus({ preventScroll: true });
    }
  }

  close(restoreFocus = false): void {
    const unreg = this.unregister;
    this.unregister = null;
    unreg?.();
    if (this.onKey) {
      document.removeEventListener('keydown', this.onKey);
      this.onKey = null;
    }
    this.scrim?.remove();
    this.sheet?.remove();
    this.scrim = null;
    this.sheet = null;
    this.body = null;
    this.foot = null;
    this.onCloseCb = null;
    if (restoreFocus && this.returnFocus?.isConnected) {
      this.returnFocus.focus({ preventScroll: true });
    }
    this.returnFocus = null;
  }

  private requestClose(): void {
    const cb = this.onCloseCb;
    this.close(true);
    cb?.();
  }

  private handleKeydown(ev: KeyboardEvent): void {
    if (!this.sheet) return;
    if (ev.key === 'Escape') {
      ev.preventDefault();
      this.requestClose();
      return;
    }
    if (ev.key !== 'Tab') return;
    const nodes = [...this.sheet.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((n) => n.offsetParent !== null);
    if (!nodes.length) {
      ev.preventDefault();
      this.sheet.focus();
      return;
    }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = document.activeElement;
    if (ev.shiftKey && (active === first || !this.sheet.contains(active))) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && (active === last || !this.sheet.contains(active))) {
      ev.preventDefault();
      first.focus();
    }
  }
}
