// 时间戳标签（新版后端）。文字按 COPY.md 3.5 逐字使用；格式下拉直接列出示例，不写格式代码。
// 选项的 id 和后端 /api/frames 的 stamp 对象对应；接口没有 stamp 时显示「暂不可用」，不写假选项。

import { h } from './dom.js';
import { beginChange, endChange, persist, schedulePreview } from './preview.js';
import { normalizeStamp, stampDateText, isDateString, clipText, TEXT_MAX } from './finish.js';
import { seg, toggle, swatch, notReady } from './controls.js';

const FONT_NAMES = { seg7: '数码管', typewriter: '打字机', sans: '无衬线' };
const COLOR_NAMES = { orange: '橙', red: '红', yellow: '黄', white: '白' };
const COLOR_HEX = { orange: '#ff8a2a', red: '#ff3b2f', yellow: '#ffd23a', white: '#fff4e6' };
const SIZE_NAMES = { small: '小', medium: '中', large: '大' };
const CORNER_NAMES = { tl: '左上', tr: '右上', bl: '左下', br: '右下' };
const CONTENT = [['date', '日期'], ['text', '文字'], ['date+text', '日期和文字']];

function listOf(x) {
  return Array.isArray(x) ? x : Object.values(x || {});
}

function isoDate(view) {
  const d = view.photo.date;
  if (isDateString(d)) return d;
  const c = view.photo.captured_at;
  return c && isDateString(String(c).slice(0, 10)) ? String(c).slice(0, 10) : '1970-01-01';
}

function update(view, patch) {
  const ed = view.ed;
  beginChange(view);
  ed.stamp = normalizeStamp({ ...ed.stamp, ...patch });
  ed.stampFrom = 'photo';
  persist(view, { stamp: ed.stamp }).then(() => endChange(view));
  schedulePreview(view, { full: true });
  view.redrawSide?.();
}

export function stampPanel(view) {
  const ed = view.ed;
  const api = ed.frames && ed.frames.stamp;
  if (!ed.v2 || !api) return h('div', { class: 'pane' }, h('h2', {}, '时间戳'), notReady('时间戳'));
  const st = ed.stamp;
  const formats = listOf(api.formats);
  const iso = isoDate(view);
  const hasDate = st.content !== 'text';
  const hasText = st.content !== 'date';

  const rows = [
    h('div', { class: 'pane-head' }, h('h2', {}, '时间戳')),
    toggle('时间戳', !!st.enabled, (on) => update(view, { enabled: on })),
  ];
  const body = [];
  body.push(seg(CONTENT.map(([id, label]) => [id, label]), st.content, (c) => update(view, { content: c }), { label: '内容' }));

  if (hasDate) {
    const sel = h('select', { 'aria-label': '格式' }, ...formats.map((f) => h('option', {
      value: f.id, selected: f.id === st.date_format ? 'selected' : null,
    }, stampDateText(f.id, iso, '14:05', false))));
    sel.value = st.date_format;
    sel.addEventListener('change', () => update(view, { date_format: sel.value }));
    body.push(h('div', { class: 'field-line' }, h('span', { class: 'small dim' }, '格式'), sel));
    body.push(toggle('显示时间', !!st.show_time, (on) => update(view, { show_time: on })));
    const dateInput = h('input', { type: 'date', value: st.date_override || iso, 'aria-label': '指定日期' });
    dateInput.addEventListener('change', () => {
      if (isDateString(dateInput.value)) update(view, { date_override: dateInput.value });
    });
    body.push(h('div', { class: 'field-line' },
      h('span', { class: 'small dim' }, '指定日期'),
      dateInput,
      st.date_override ? h('button', { type: 'button', onclick: () => update(view, { date_override: null }) }, '重置') : null,
    ));
    if (view.photo.date_source === 'mtime') body.push(h('p', { class: 'fine' }, '使用文件修改日期'));
  }
  if (hasText) {
    const input = h('input', { type: 'text', value: st.text, maxlength: String(api.text_max_length || TEXT_MAX), 'aria-label': '文字' });
    input.addEventListener('change', () => update(view, { text: clipText(input.value.trim(), api.text_max_length || TEXT_MAX) }));
    body.push(h('div', { class: 'sec' }, '文字'), input);
  }

  body.push(seg(Object.keys(FONT_NAMES).map((k) => [k, FONT_NAMES[k]]), st.font, (f) => update(view, { font: f }), { label: '字体' }));
  body.push(h('div', { class: 'swrow' }, h('span', { class: 'small dim' }, '颜色'),
    ...Object.keys(COLOR_NAMES).map((c) => swatch(COLOR_HEX[c], st.color === c, () => update(view, { color: c }), COLOR_NAMES[c]))));
  body.push(seg(Object.keys(SIZE_NAMES).map((k) => [k, SIZE_NAMES[k]]), st.size, (s) => update(view, { size: s }), { label: '大小' }));
  body.push(seg(Object.keys(CORNER_NAMES).map((k) => [k, CORNER_NAMES[k]]), st.corner, (c) => update(view, { corner: c }), { label: '位置' }));
  body.push(toggle('发光', !!st.glow, (on) => update(view, { glow: on })));

  rows.push(h('div', { class: st.enabled ? null : 'dimmed-block' }, ...body));
  return h('div', { class: 'pane' }, ...rows);
}
