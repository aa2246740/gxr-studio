// 边框标签（新版后端）：样式卡片（缩略图由后端用当前照片渲染，POST render size=thumb）、选项、设为默认。
// 旧版后端：显示「暂不可用」。样式名和选项名按 COPY.md 3.4 的固定写法，不用接口里的任意名字。

import { h, toast, toastError } from './dom.js';
import { apiBlob } from './api.js';
import { persist, schedulePreview, beginChange, endChange, styleOptions } from './preview.js';
import { saveDefaults } from './caps.js';
import { normalizeFrame, frameBody, defaultsPayload, DEFAULT_FRAME, clipText, TEXT_MAX } from './finish.js';
import { seg, toggle, swatch, notReady } from './controls.js';

export const FRAME_NAMES = {
  none: '无',
  gallery: '白边',
  leica: '徕卡水印',
  hasselblad: '哈苏水印',
  film: '胶片',
  instant: '拍立得',
  canvas: '画布',
};
const COLOR_HEX = { white: '#ffffff', black: '#111214', cream: '#f2ead8' };
const COLOR_NAMES = { white: '白', black: '黑', cream: '米白' };
const WEIGHT_NAMES = { thin: '细', medium: '中', thick: '粗' };
const CANVAS = ['1:1', '4:5', '3:2', '9:16'];

const THUMB_CACHE = new Map();
const queue = [];
let running = 0;

function pump() {
  while (running < 2 && queue.length) {
    const job = queue.shift();
    running += 1;
    job().finally(() => {
      running -= 1;
      pump();
    });
  }
}

function styleList(ed) {
  const list = ed.frames && ed.frames.styles;
  const arr = Array.isArray(list) ? list : Object.values(list || {});
  return arr.filter((s) => s && s.id).map((s) => ({ id: s.id, name: FRAME_NAMES[s.id] || s.name || s.id, options: s.options || [], defaults: s.defaults || null }));
}

// 一张卡片的缩略图：用当前照片、这个样式的边框，由后端渲染（不另外做假图）
function loadThumb(view, style, holder) {
  const ed = view.ed;
  const base = style.defaults ? { ...DEFAULT_FRAME, ...style.defaults } : { ...DEFAULT_FRAME };
  const body = { frame: frameBody({ ...base, style: style.id }, style.options) };
  const key = `${view.photo.id}|${ed.rev}|${JSON.stringify(body)}`;
  const show = (url) => {
    if (!holder.isConnected) return;
    holder.replaceChildren(h('img', { src: url, alt: '', draggable: 'false' }));
  };
  if (THUMB_CACHE.has(key)) {
    show(THUMB_CACHE.get(key));
    return;
  }
  queue.push(async () => {
    try {
      const { blob } = await apiBlob(`/api/photos/${view.photo.id}/render?stage=final&size=thumb`, { body });
      const url = URL.createObjectURL(blob);
      THUMB_CACHE.set(key, url);
      show(url);
    } catch {
      if (holder.isConnected) holder.replaceChildren(h('span', { class: 'dim small' }, '—'));
    }
  });
  pump();
}

function setFrame(view, patch) {
  const ed = view.ed;
  beginChange(view);
  ed.frame = normalizeFrame({ ...ed.frame, ...patch });
  ed.frameFrom = 'photo'; // 改过就是照片自己的设置，不再继承默认值
  persist(view, { frame: frameBody(ed.frame, styleOptions(ed, ed.frame.style)) }).then(() => endChange(view));
  schedulePreview(view, { full: true });
  view.redrawSide?.();
}

export function framePanel(view) {
  const ed = view.ed;
  if (!ed.v2 || !ed.frames) return h('div', { class: 'pane' }, h('h2', {}, '边框'), notReady('边框'));
  const styles = styleList(ed);
  if (!styles.length) return h('div', { class: 'pane' }, h('h2', {}, '边框'), notReady('边框'));
  const cur = ed.frame;
  const opts = styleOptions(ed, cur.style);

  const cards = h('div', { class: 'fcards' }, ...styles.map((s) => {
    const thumb = h('div', { class: 'thumb' }, h('span', { class: 'dim small' }, '…'));
    loadThumb(view, s, thumb);
    return h('button', {
      type: 'button', class: `fcard${s.id === 'none' ? ' none' : ''}`, 'aria-pressed': String(cur.style === s.id),
      onclick: () => {
        if (cur.style !== s.id) setFrame(view, { style: s.id });
      },
    }, thumb, h('div', { class: 'nm' }, s.name));
  }));

  const rows = [h('div', { class: 'pane-head' }, h('h2', {}, '边框')), cards];
  if (cur.style !== 'none') {
    if (opts.includes('color')) {
      // 颜色只用色块（不再加文字按钮）；鼠标停在色块上看颜色名，选中的色块有外圈
      rows.push(h('div', { class: 'seg' }, h('span', { class: 'small dim seg-label' }, '颜色'),
        ...['white', 'black', 'cream'].map((c) => swatch(COLOR_HEX[c], cur.color === c, () => setFrame(view, { color: c }), COLOR_NAMES[c]))));
    }
    if (opts.includes('weight')) {
      rows.push(seg(['thin', 'medium', 'thick'].map((w) => [w, WEIGHT_NAMES[w]]), cur.weight, (w) => setFrame(view, { weight: w }), { label: '宽度' }));
    }
    const shows = [];
    if (opts.includes('show_logo')) shows.push(['show_logo', '标志']);
    if (opts.includes('show_params')) shows.push(['show_params', '参数']);
    if (opts.includes('show_date')) shows.push(['show_date', '日期']);
    // 胶片边框的帧号由样式自带，不单独做开关（COPY 3.9）
    if (shows.length) {
      rows.push(h('div', { class: 'sec' }, '显示'));
      for (const [key, label] of shows) rows.push(toggle(label, !!cur[key], (on) => setFrame(view, { [key]: on })));
    }
    if (opts.includes('text')) {
      const input = h('input', { type: 'text', value: cur.text, maxlength: String(TEXT_MAX), 'aria-label': '文字' });
      input.addEventListener('change', () => setFrame(view, { text: clipText(input.value.trim()) }));
      rows.push(h('div', { class: 'sec' }, '文字'), input);
    }
    if (opts.includes('canvas_ratio')) {
      rows.push(seg(CANVAS.map((r) => [r, r]), cur.canvas_ratio, (r) => setFrame(view, { canvas_ratio: r }), { label: '画布比例' }));
    }
  }

  // 底部按钮固定在面板底边（和裁切页一样）
  rows.push(h('div', { class: 'sticky-foot' }, h('button', { type: 'button', class: 'primary', onclick: () => setDefaults(view) }, '设为默认')));
  return h('div', { class: 'pane' }, ...rows);
}

export async function setDefaults(view) {
  const ed = view.ed;
  try {
    const body = defaultsPayload(ed.frame, ed.stamp);
    await saveDefaults(body);
    ed.defaults = body;
    toast('已设为默认', 'ok');
  } catch (e) {
    toastError(e, '无法保存修改。请重试。');
  }
}
