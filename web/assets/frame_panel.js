// 边框标签（新版后端）：样式卡片（浏览器用一张小底图加版面画，见 finish_cards.js）、选项、设为默认。
// 点样式、改选项，大图上的边框马上变（浏览器自己画，见 stage_finish.js），保存在后面写，不等服务器出图。
// 旧版后端：显示「暂不可用」。样式名和选项名按 COPY.md 3.4 的固定写法，不用接口里的任意名字。

import { h, toast, toastError } from './dom.js';
import { persist, beginChange, endChange, styleOptions } from './preview.js';
import { saveDefaults } from './caps.js';
import { normalizeFrame, frameBody, defaultsPayload, clipText, TEXT_MAX } from './finish.js';
import { seg, toggle, swatch, notReady } from './controls.js';
import { paintCards } from './finish_cards.js';
import { liveText } from './live_text.js';
import { cropRatio } from './finish_layout.js';
import { frameSizeLine, ratioText } from './output_size.js';

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
const CANVAS = ['1:1', '3:4', '2:3', '4:5', '3:2', '9:16'];

function styleList(ed) {
  const list = ed.frames && ed.frames.styles;
  const arr = Array.isArray(list) ? list : Object.values(list || {});
  return arr.filter((s) => s && s.id).map((s) => ({ id: s.id, name: FRAME_NAMES[s.id] || s.name || s.id, options: s.options || [], defaults: s.defaults || null }));
}

// 改一项：界面和大图马上变，保存另外排队
function change(view, patch) {
  const ed = view.ed;
  if (!ed) return; // 面板上的计时器到点时，这张照片已经关了：改动早就在 ed 里，由 save 写下来
  beginChange(view);
  ed.frame = normalizeFrame({ ...ed.frame, ...patch });
  ed.frameFrom = 'photo'; // 改过就是照片自己的设置，不再继承默认值
  view.onChange?.();
}

function save(view, ed) {
  persist(view, { frame: frameBody(ed.frame, styleOptions(ed, ed.frame.style)) }).then(() => endChange(view));
}

function setFrame(view, patch) {
  change(view, patch);
  save(view, view.ed);
  view.redrawSide?.();
}

export function framePanel(view) {
  const ed = view.ed;
  if (!ed.v2 || !ed.frames) return h('div', { class: 'pane' }, h('h2', {}, '边框'), notReady('边框'));
  const styles = styleList(ed);
  if (!styles.length) return h('div', { class: 'pane' }, h('h2', {}, '边框'), notReady('边框'));
  const cur = ed.frame;
  const opts = styleOptions(ed, cur.style);

  const items = [];
  const cards = h('div', { class: 'fcards' }, ...styles.map((s) => {
    const thumb = h('div', { class: 'thumb skel', 'aria-busy': 'true' });
    items.push({ style: s, holder: thumb });
    return h('button', {
      type: 'button', class: `fcard${s.id === 'none' ? ' none' : ''}`, 'aria-pressed': String(cur.style === s.id),
      onclick: () => {
        if (cur.style !== s.id) setFrame(view, { style: s.id });
      },
    }, thumb, h('div', { class: 'nm' }, s.name));
  }));
  paintCards(view, items);

  const sizeLine = frameSizeLine(view);
  const rows = [h('div', { class: 'pane-head' }, h('h2', {}, '边框'), sizeLine), cards];
  if (cur.style !== 'none') {
    if (cur.style !== 'canvas') {
      const c = view.crop;
      const ratio = c ? cropRatio(c.preset, c.rect.w * c.frame.width, c.rect.h * c.frame.height) : null;
      rows.push(seg([['crop', `跟裁剪${ratio ? ` ${ratioText(ratio)}` : ''}`], ['frame', '边框原样']],
        cur.keep_ratio !== false ? 'crop' : 'frame', (value) => setFrame(view, { keep_ratio: value === 'crop' }), { label: '成品比例' }));
      if (!ratio) rows.push(h('p', { class: 'small dim' }, '先在「裁剪」选择比例，成品会连边框一起保持比例。'));
    }
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
      // 边输入边变，停下来再保存。这个输入框不重搭面板（重搭会丢掉光标）
      liveText(input, {
        apply: () => { change(view, { text: clipText(input.value) }); sizeLine.update(); },
        save: () => {
          change(view, { text: ed.frame.text.trim() }); // 前后的空格输入时留着，写下来时去掉
          sizeLine.update();
          save(view, ed);
        },
      });
      rows.push(h('div', { class: 'sec' }, '文字'), input);
    }
    if (opts.includes('canvas_ratio')) {
      const ratios = seg(CANVAS.map((r) => [r, r]), cur.canvas_ratio, (r) => setFrame(view, { canvas_ratio: r }), { label: '画布比例' });
      ratios.style.gridTemplateColumns = '1fr'; // 六个比例用整行，窄面板里数字也不截断
      rows.push(ratios);
    }
  }

  // 跟在样式和选项后面滚动，避免窄屏或矮窗口时盖住「画布」卡片。
  rows.push(h('div', { class: 'foot' }, h('button', { type: 'button', class: 'btn sm primary', onclick: () => setDefaults(view) }, '设为默认')));
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
