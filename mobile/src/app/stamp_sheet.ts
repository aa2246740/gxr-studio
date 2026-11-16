// 时间戳底部面板（选项、顺序与文案严格对齐桌面 `web/assets/stamp_panel.js`）：
// 开关、印记落点（拍立得）、内容、格式、显示时间、指定日期（YYYY-MM-DD + 重置 + 使用文件修改日期）、文字（拒收中文）、字体、颜色、大小、位置、发光。

import { CJK_REFUSE_MESSAGE, clipText, hasCjk, stripCjk } from '../lib/fonts';
import {
  isDateString,
  normalizeStamp,
  STAMP_COLOR_HEX,
  STAMP_COLOR_NAMES,
  STAMP_CONTENT,
  STAMP_CORNERS,
  STAMP_FONTS,
  STAMP_FORMATS,
  STAMP_SIZES,
  stampDateText,
  TEXT_MAX,
  type StampColor,
  type StampCorner,
  type StampDateFormat,
  type StampFont,
  type StampPlacement,
  type StampSize,
} from '../lib/frame_spec';
import { button, el } from './dom';
import { commitStamp, isMtimeDate, photoIsoDate } from './frame_state';
import type { Photo } from './model';

function segGroup<T extends string>(
  items: ReadonlyArray<readonly [T, string]>,
  current: T,
  label: string,
  onPick: (value: T) => void,
): HTMLElement {
  const seg = el('div', 'seg');
  seg.setAttribute('role', 'group');
  seg.setAttribute('aria-label', label);
  for (const [value, text] of items) {
    const btn = button(value === current ? 'on' : '', text);
    btn.addEventListener('click', () => onPick(value));
    seg.append(btn);
  }
  return seg;
}

function swRow(labelText: string, checked: boolean, onChange: (on: boolean) => void): HTMLElement {
  const row = el('label', 'swrow');
  row.append(el('span', 'fslab', labelText));
  const input = document.createElement('input');
  input.type = 'checkbox';
  input.className = 'switch';
  input.checked = checked;
  input.setAttribute('aria-label', labelText);
  input.addEventListener('change', () => onChange(input.checked));
  row.append(input);
  return row;
}

export function openStampSheet(
  host: HTMLElement,
  photo: Photo,
  onUpdate: () => void,
): void {
  host.querySelector('.scrim.stamp-scrim')?.remove();
  host.querySelector('.ms.stamp-sheet')?.remove();

  const scrim = el('div', 'scrim stamp-scrim');
  const sheet = el('div', 'ms frame-sheet stamp-sheet');
  sheet.dataset.state = 'stamp';

  const hd = el('div', 'hd');
  hd.append(el('h2', '', '时间戳'));
  const done = button('nb bold', '完成');
  hd.append(done);

  const body = el('div', 'ms-body');
  let initialText = photo.stamp.text;
  let flushText: (() => void) | null = null;

  const update = (patch: Partial<typeof photo.stamp>): void => {
    if (commitStamp(photo, patch, onUpdate)) renderBody();
  };

  const renderBody = (): void => {
    body.replaceChildren();
    flushText = null;
    const st = photo.stamp;
    const iso = photoIsoDate(photo);
    const hasDate = st.content !== 'text';
    const hasText = st.content !== 'date';

    const topRow = el('div', 'fsrow');
    topRow.append(
      swRow('时间戳', st.enabled, (enabled) => {
        update({ enabled });
      }),
    );
    body.append(topRow);

    const inner = el('div', st.enabled ? '' : 'dimmed-block');

    // 印记落点（拍立得）
    if (photo.border.style === 'instant') {
      const row = el('div', 'fsrow');
      row.append(el('span', 'fslab', '印记落点'));
      row.append(
        segGroup<StampPlacement>(
          [
            ['photo', '照片上'],
            ['border', '拍立得边框上'],
          ],
          st.placement || 'photo',
          '印记落点',
          (placement) => update({ placement }),
        ),
      );
      inner.append(row);
    } else if (st.placement === 'border') {
      const noteRow = el('div', 'fsrow');
      noteRow.append(el('p', 'ffine', '选择「拍立得」边框后，印记会放在底边上；其他样式放在照片上。'));
      inner.append(noteRow);
    }

    // 内容
    const contentRow = el('div', 'fsrow');
    contentRow.append(el('span', 'fslab', '内容'));
    contentRow.append(
      segGroup(STAMP_CONTENT, st.content, '内容', (content) => {
        update({ content });
      }),
    );
    inner.append(contentRow);

    // 日期相关：格式、显示时间、指定日期
    if (hasDate) {
      const fmtRow = el('div', 'fsrow');
      const fmtLine = el('label', 'fsline');
      fmtLine.append(el('span', 'fslab', '格式'));
      const sel = document.createElement('select');
      sel.className = 'selbox';
      sel.setAttribute('aria-label', '格式');
      for (const id of STAMP_FORMATS) {
        const opt = document.createElement('option');
        opt.value = id;
        opt.textContent = stampDateText(id, iso, '14:05', false);
        if (id === st.date_format) opt.selected = true;
        sel.append(opt);
      }
      sel.addEventListener('change', () => {
        update({ date_format: sel.value as StampDateFormat });
      });
      fmtLine.append(sel);
      fmtRow.append(fmtLine);
      inner.append(fmtRow);

      const timeRow = el('div', 'fsrow');
      timeRow.append(
        swRow('显示时间', st.show_time, (show_time) => {
          update({ show_time });
        }),
      );
      inner.append(timeRow);

      const dateRow = el('div', 'fsrow');
      const dateLine = el('div', 'fsline');
      dateLine.append(el('span', 'fslab', '指定日期'));
      const right = el('div', 'sws');
      const dateInput = document.createElement('input');
      dateInput.type = 'date';
      dateInput.className = 'datebox';
      dateInput.value = st.date_override || iso;
      dateInput.setAttribute('aria-label', '指定日期');
      dateInput.addEventListener('change', () => {
        if (isDateString(dateInput.value)) update({ date_override: dateInput.value });
      });
      right.append(dateInput);
      if (st.date_override) {
        const reset = button('btn', '重置');
        reset.addEventListener('click', () => update({ date_override: null }));
        right.append(reset);
      }
      dateLine.append(right);
      dateRow.append(dateLine);
      if (isMtimeDate(photo)) {
        dateRow.append(el('p', 'ffine', '使用文件修改日期'));
      }
      inner.append(dateRow);
    }

    // 文字（24 字上限，拒收中文，不重构输入框以保光标）
    if (hasText) {
      const textRow = el('div', 'fsrow');
      textRow.append(el('span', 'fslab', '文字'));
      const input = document.createElement('input');
      input.type = 'text';
      input.className = 'txtbox';
      input.maxLength = TEXT_MAX;
      input.value = st.text;
      input.placeholder = '英文或数字（最多 24 字）';
      input.setAttribute('aria-label', '文字');

      const err = el('p', 'ferr');
      err.hidden = true;
      err.setAttribute('role', 'status');

      let timer = 0;
      const applyLive = () => {
        window.clearTimeout(timer);
        const raw = input.value;
        if (hasCjk(raw)) {
          const clean = clipText(stripCjk(raw), TEXT_MAX);
          input.value = clean;
          err.textContent = CJK_REFUSE_MESSAGE;
          err.hidden = false;
          return;
        }
        const clipped = clipText(raw, TEXT_MAX);
        if (clipped !== raw) input.value = clipped;
        err.hidden = true;
        photo.stamp = normalizeStamp({ ...photo.stamp, text: clipped });
        timer = window.setTimeout(() => onUpdate(), 120);
      };
      flushText = () => {
        window.clearTimeout(timer);
        const clean = clipText(stripCjk(input.value), TEXT_MAX).trim();
        if (input.value !== clean) input.value = clean;
        const next = normalizeStamp({ ...photo.stamp, text: clean });
        if (next.text !== initialText) {
          photo.stamp = next;
          photo.rev += 1;
          initialText = next.text;
          onUpdate();
        }
      };

      input.addEventListener('input', applyLive);
      input.addEventListener('compositionend', applyLive);
      input.addEventListener('change', flushText);
      textRow.append(input, err);
      inner.append(textRow);
    }

    // 字体
    const fontRow = el('div', 'fsrow');
    fontRow.append(el('span', 'fslab', '字体'));
    fontRow.append(
      segGroup(
        Object.entries(STAMP_FONTS) as Array<[StampFont, string]>,
        st.font,
        '字体',
        (font) => update({ font }),
      ),
    );
    inner.append(fontRow);

    // 颜色
    const colorRow = el('div', 'fsrow');
    const colorLine = el('div', 'fsline');
    colorLine.append(el('span', 'fslab', '颜色'));
    const swatches = el('div', 'sws');
    (Object.keys(STAMP_COLOR_NAMES) as StampColor[]).forEach((c) => {
      const sw = button(st.color === c ? 'swc on' : 'swc', '');
      sw.dataset.color = c;
      sw.setAttribute('aria-label', STAMP_COLOR_NAMES[c]);
      const dot = el('i');
      dot.style.background = STAMP_COLOR_HEX[c];
      sw.append(dot);
      sw.addEventListener('click', () => update({ color: c }));
      swatches.append(sw);
    });
    colorLine.append(swatches);
    colorRow.append(colorLine);
    inner.append(colorRow);

    // 大小
    const sizeRow = el('div', 'fsrow');
    sizeRow.append(el('span', 'fslab', '大小'));
    sizeRow.append(
      segGroup(
        Object.entries(STAMP_SIZES) as Array<[StampSize, string]>,
        st.size,
        '大小',
        (size) => update({ size }),
      ),
    );
    inner.append(sizeRow);

    // 位置
    const cornerRow = el('div', 'fsrow');
    cornerRow.append(el('span', 'fslab', '位置'));
    cornerRow.append(
      segGroup(
        Object.entries(STAMP_CORNERS) as Array<[StampCorner, string]>,
        st.corner,
        '位置',
        (corner) => update({ corner }),
      ),
    );
    inner.append(cornerRow);

    // 发光
    const glowRow = el('div', 'fsrow');
    glowRow.append(
      swRow('发光', st.glow, (glow) => {
        update({ glow });
      }),
    );
    inner.append(glowRow);

    body.append(inner);
  };

  renderBody();

  const close = () => {
    flushText?.();
    scrim.remove();
    sheet.remove();
  };
  done.addEventListener('click', close);
  scrim.addEventListener('click', close);

  sheet.append(el('div', 'grab'), hd, body);
  host.append(scrim, sheet);
}
