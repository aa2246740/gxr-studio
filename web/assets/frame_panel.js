// 边框标签（新版后端）：样式卡片（缩略图由后端用当前照片渲染，POST render size=thumb）、选项、设为默认。
// 旧版后端：显示「暂不可用」。样式名和选项名按 COPY.md 3.4 的固定写法，不用接口里的任意名字。

import { h, toast, toastError } from './dom.js';
import { apiBlob } from './api.js';
import { on } from './state.js';
import { persist, schedulePreview, beginChange, endChange, styleOptions, saveStatus } from './preview.js';
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
const THUMB_KEEP = 96; // 缓存的卡片图上限；超出的最旧一张释放
const flights = new Map(); // 缩略图键 → { promise, views }：面板重搭、换走又换回来时同一张不重复请求，等它的视图都记下
const queue = [];
let running = 0;
let waiting = null;

// 卡片图是次要的：这张照片有保存在路上、或大图正在出图时先让路，空下来再请求
function busy(view) {
  return saveStatus(view.photo.id) === 'saving' || !!(view.ed && view.ed.busy);
}

// 还在等这张图的视图（换照片会关掉旧的视图；换回来的新视图会接着等同一张）
function alive(entry) {
  for (const v of entry.views) if (!v.disposed) return v;
  return null;
}

function pump() {
  clearTimeout(waiting);
  waiting = null;
  while (running < 2 && queue.length) {
    const view = alive(queue[0].entry);
    // 等它的视图全关了才放弃：只关了旧的、新视图还在等时照常请求，不把新面板的卡片一起拒掉（PR #39 复审）
    if (!view) {
      queue.shift().skip();
      continue;
    }
    if (busy(view)) {
      waiting = setTimeout(pump, 120);
      return;
    }
    const job = queue.shift();
    running += 1;
    job.run().finally(() => {
      running -= 1;
      pump();
    });
  }
}

on('save-status', () => {
  if (queue.length) pump(); // 保存写完了：让路的卡片接着请求
});

function remember(key, url) {
  THUMB_CACHE.set(key, url);
  while (THUMB_CACHE.size > THUMB_KEEP) {
    const oldest = THUMB_CACHE.keys().next().value;
    URL.revokeObjectURL(THUMB_CACHE.get(oldest));
    THUMB_CACHE.delete(oldest);
  }
}

function fetchThumb(view, body, key) {
  const known = flights.get(key);
  if (known) {
    known.views.add(view);
    return known.promise;
  }
  const photoId = view.photo.id; // 键里就有照片 id：等这张图的视图都是同一张照片
  const entry = { views: new Set([view]), promise: null };
  entry.promise = new Promise((resolve, reject) => {
    queue.push({
      entry,
      skip: () => reject(new Error('closed')),
      run: async () => {
        try {
          const { blob } = await apiBlob(`/api/photos/${photoId}/render?stage=final&size=card`, { body });
          const url = URL.createObjectURL(blob);
          remember(key, url);
          resolve(url);
        } catch (e) {
          reject(e);
        }
      },
    });
  }).finally(() => {
    if (flights.get(key) === entry) flights.delete(key);
  });
  flights.set(key, entry);
  pump();
  return entry.promise;
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
  // 还在渲染时卡片是一块轻轻呼吸的底色（不放「…」）；出来了淡入
  const done = () => {
    holder.classList.remove('skel');
    holder.removeAttribute('aria-busy');
  };
  // 不看 isConnected：面板还在搭（卡片还没挂上页面）时图就到了，也要填进去，不然一直停在骨架（AA01，PR #39 复审）
  const show = (url) => {
    done();
    holder.replaceChildren(h('img', { src: url, alt: '', draggable: 'false' }));
  };
  // 缓存里有：卡片还没挂到页面上（面板正在搭），直接填进去，不看 isConnected，否则会一直停在骨架（AA01）
  if (THUMB_CACHE.has(key)) {
    done();
    holder.replaceChildren(h('img', { src: THUMB_CACHE.get(key), alt: '', draggable: 'false' }));
    return;
  }
  fetchThumb(view, body, key).then(show, () => {
    done();
    holder.replaceChildren(h('span', { class: 'dim small' }, '没有预览'));
  });
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
    const thumb = h('div', { class: 'thumb skel', 'aria-busy': 'true' });
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
      rows.push(h('div', { class: 'seg-row' }, h('span', { class: 'seg-label' }, '颜色'), h('div', { class: 'swrow' },
        ...['white', 'black', 'cream'].map((c) => swatch(COLOR_HEX[c], cur.color === c, () => setFrame(view, { color: c }), COLOR_NAMES[c])))));
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
  rows.push(h('div', { class: 'sticky-foot' }, h('button', { type: 'button', class: 'btn sm primary', onclick: () => setDefaults(view) }, '设为默认')));
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
