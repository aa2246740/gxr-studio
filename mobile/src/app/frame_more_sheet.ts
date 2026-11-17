// 03f「更多」按钮打开的底部面板（「更多选项」）：
// 成品比例、画幅（拍立得/胶片）、背景（灯台/黑色）、型号文字「GXR」、标志颜色/大小、题字（拒收中文）。

import { sanitizeEnglishText } from '../lib/fonts';
import {
  FILM_FORMATS,
  INSTANT_FORMATS,
  normalizeFrame,
  OUTPUT_CHOICES,
  restoreFrameSpec,
  STYLE_OPTIONS,
  TEXT_MAX,
  type LogoMode,
  type LogoSize,
  type OutputRatio,
  type TableBg,
} from '../lib/frame_spec';
import { button, el } from './dom';
import { commitFrame } from './frame_state';
import { logoStore } from './logo_store';
import type { Photo } from './model';
import { cancelHostSheets, registerSheet } from './sheet_lifecycle';

function segGroup<T extends string>(
  items: ReadonlyArray<readonly [T, string]>,
  current: T,
  label: string,
  onPick: (value: T) => void,
): HTMLElement {
  const seg = el('div', 'seg');
  seg.setAttribute('role', 'group');
  seg.setAttribute('aria-label', label);
  const buttons: HTMLButtonElement[] = [];
  for (const [value, text] of items) {
    const btn = button(value === current ? 'on' : '', text);
    btn.addEventListener('click', () => {
      buttons.forEach((b) => b.classList.toggle('on', b === btn));
      onPick(value);
    });
    buttons.push(btn);
    seg.append(btn);
  }
  return seg;
}

export function openFrameMoreSheet(
  host: HTMLElement,
  photo: Photo,
  onUpdate: () => void,
  isAlive: () => boolean = () => true,
): void {
  cancelHostSheets(host);
  host.querySelector('.scrim.frame-more-scrim')?.remove();
  host.querySelector('.ms.frame-more-sheet')?.remove();

  const version = photo.version;
  const scrim = el('div', 'scrim frame-more-scrim');
  const sheet = el('div', 'ms frame-sheet frame-more-sheet');
  sheet.dataset.state = 'frame-more';

  let closed = false;
  let timer = 0;
  const stillAlive = (): boolean => !closed && sheet.isConnected && photo.version === version && isAlive();
  const clearTimer = (): void => {
    if (timer) {
      window.clearTimeout(timer);
      timer = 0;
    }
  };

  const hd = el('div', 'hd');
  hd.append(el('h2', '', '更多选项'));
  const done = button('nb bold', '完成');
  hd.append(done);

  const body = el('div', 'ms-body');
  const cur = photo.border;
  const opts = STYLE_OPTIONS[cur.style];

  // 成品比例
  const ratioRow = el('div', 'fsrow');
  const ratioLine = el('label', 'fsline');
  ratioLine.append(el('span', 'fslab', '成品比例'));
  const select = document.createElement('select');
  select.className = 'selbox';
  select.setAttribute('aria-label', '成品比例');
  for (const [value, text] of OUTPUT_CHOICES) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = text;
    if (cur.output_ratio === value) opt.selected = true;
    select.append(opt);
  }
  select.addEventListener('change', () => {
    if (!stillAlive()) return;
    commitFrame(photo, { output_ratio: select.value as OutputRatio }, onUpdate);
  });
  ratioLine.append(select);
  ratioRow.append(ratioLine);
  body.append(ratioRow);

  // 画幅（拍立得 / 胶片）
  if (opts.includes('fmt')) {
    const row = el('div', 'fsrow');
    row.append(el('span', 'fslab', '画幅'));
    const formats = cur.style === 'film' ? FILM_FORMATS : INSTANT_FORMATS;
    row.append(
      segGroup(formats, cur.fmt, '画幅', (fmt) => {
        if (!stillAlive()) return;
        commitFrame(photo, { fmt }, onUpdate);
      }),
    );
    body.append(row);
  }

  // 背景（胶片灯台 / 黑色）
  if (opts.includes('table')) {
    const row = el('div', 'fsrow');
    row.append(el('span', 'fslab', '背景'));
    row.append(
      segGroup<TableBg>(
        [
          ['light', '灯台'],
          ['black', '黑色'],
        ],
        cur.table,
        '背景',
        (table) => {
          if (!stillAlive()) return;
          commitFrame(photo, { table }, onUpdate);
        },
      ),
    );
    body.append(row);
  }

  // 自定义标志颜色 / 大小
  const activeLogo = cur.logo || (cur.show_logo === false ? 'none' : 'ricoh');
  const isCustomLogo = activeLogo !== 'ricoh' && activeLogo !== 'none' && Boolean(logoStore.info(activeLogo));
  let customLogoRows: HTMLElement[] = [];
  if (opts.includes('logo') && isCustomLogo) {
    const modeRow = el('div', 'fsrow');
    modeRow.append(el('span', 'fslab', '标志颜色'));
    modeRow.append(
      segGroup<LogoMode>(
        [
          ['auto', '自动'],
          ['orig', '原色'],
          ['mono', '单色'],
        ],
        cur.logo_mode,
        '标志颜色',
        (logo_mode) => {
          if (!stillAlive()) return;
          commitFrame(photo, { logo_mode }, onUpdate);
        },
      ),
    );
    const sizeRow = el('div', 'fsrow');
    sizeRow.append(el('span', 'fslab', '大小'));
    sizeRow.append(
      segGroup<LogoSize>(
        [
          ['small', '小'],
          ['medium', '中'],
          ['large', '大'],
        ],
        cur.logo_size,
        '大小',
        (logo_size) => {
          if (!stillAlive()) return;
          commitFrame(photo, { logo_size }, onUpdate);
        },
      ),
    );
    customLogoRows = [modeRow, sizeRow];
    body.append(modeRow, sizeRow);
  }

  // 徕卡型号文字「GXR」
  if (cur.style === 'leica' && activeLogo !== 'none') {
    const row = el('div', 'fsrow');
    const swRow = el('label', 'swrow');
    swRow.append(el('span', 'fslab', '型号文字「GXR」'));
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.className = 'switch';
    input.checked = cur.model !== false;
    input.addEventListener('change', () => {
      if (!stillAlive()) return;
      commitFrame(photo, { model: input.checked }, onUpdate);
    });
    swRow.append(input);
    row.append(swRow);
    body.append(row);
  }

  // 题字文字（24 字上限，拒收中文）
  let flushText: (() => void) | null = null;
  let restoreText: (() => void) | null = null;
  if (opts.includes('text')) {
    const row = el('div', 'fsrow');
    row.append(el('span', 'fslab', '文字'));
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'txtbox';
    input.maxLength = TEXT_MAX;
    input.value = cur.text;
    input.placeholder = '英文或数字（最多 24 字）';
    input.setAttribute('aria-label', '文字');

    const err = el('p', 'ferr');
    err.hidden = true;
    err.setAttribute('role', 'status');

    let initialText = cur.text;
    let composing = false;
    const applyLive = () => {
      clearTimer();
      if (!stillAlive()) return;
      const checked = sanitizeEnglishText(input.value, TEXT_MAX);
      if (input.value !== checked.text) input.value = checked.text;
      if (checked.rejectedCjk) {
        err.textContent = checked.error ?? '';
        err.hidden = false;
      } else {
        err.hidden = true;
      }
      photo.border = normalizeFrame({ ...photo.border, text: checked.text });
      timer = window.setTimeout(() => {
        timer = 0;
        if (stillAlive()) onUpdate();
      }, 140);
    };
    flushText = () => {
      clearTimer();
      composing = false;
      if (!stillAlive()) return;
      const checked = sanitizeEnglishText(input.value, TEXT_MAX);
      const clean = checked.text.trim();
      if (input.value !== clean) input.value = clean;
      if (checked.rejectedCjk) {
        err.textContent = checked.error ?? '';
        err.hidden = false;
      }
      const prevDraftText = photo.border.text;
      const next = normalizeFrame({ ...photo.border, text: clean });
      photo.border = next;
      if (next.text !== initialText) {
        photo.rev += 1;
        initialText = next.text;
        onUpdate();
      } else if (next.text !== prevDraftText) {
        onUpdate();
      }
    };
    restoreText = () => {
      photo.border = restoreFrameSpec(photo.border, initialText).spec;
    };

    input.addEventListener('compositionstart', () => {
      composing = true;
    });
    input.addEventListener('input', (e) => {
      if (composing || (e instanceof InputEvent && e.isComposing)) return;
      applyLive();
    });
    input.addEventListener('compositionend', () => {
      composing = false;
      applyLive();
    });
    input.addEventListener('change', () => {
      if (composing) return;
      flushText?.();
    });
    row.append(input, err);
    body.append(row);
  }

  const unsubLogo = logoStore.onChange(() => {
    if (!stillAlive() || !customLogoRows.length) return;
    const curLogo = photo.border.logo || (photo.border.show_logo === false ? 'none' : 'ricoh');
    if (curLogo === 'ricoh' || curLogo === 'none' || !logoStore.info(curLogo)) {
      for (const r of customLogoRows) r.remove();
      customLogoRows = [];
    }
  });

  let unregister = () => {};
  const close = () => {
    if (closed) return;
    flushText?.();
    closed = true;
    clearTimer();
    unsubLogo();
    unregister();
    scrim.remove();
    sheet.remove();
  };
  const cancel = () => {
    if (closed) return;
    closed = true;
    clearTimer();
    unsubLogo();
    if (photo.version === version && isAlive()) {
      restoreText?.();
    }
    unregister();
    scrim.remove();
    sheet.remove();
  };
  unregister = registerSheet(host, cancel);
  done.addEventListener('click', close);
  scrim.addEventListener('click', close);

  sheet.append(el('div', 'grab'), hd, body);
  host.append(scrim, sheet);
}
