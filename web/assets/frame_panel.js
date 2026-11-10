// 边框标签（新版后端）：样式卡片（浏览器用一张小底图加版面画，见 finish_cards.js）、选项、设为默认。
// 点样式、改选项，大图上的边框马上变（浏览器自己画，见 stage_finish.js），保存在后面写，不等服务器出图。
// 旧版后端：显示「暂不可用」。样式名和选项名按 COPY.md 3.4 的固定写法，不用接口里的任意名字。

import { h, toast, toastError } from './dom.js';
import { persist, beginChange, endChange, styleOptions } from './preview.js';
import { saveDefaults } from './caps.js';
import { normalizeFrame, frameBody, defaultsPayload, selectFrame, clipText, TEXT_MAX } from './finish.js';
import { seg, toggle, swatch, notReady } from './controls.js';
import { paintCards } from './finish_cards.js';
import { liveText } from './live_text.js';
import { frameSizeLine } from './output_size.js';

export const FRAME_NAMES = {
  none: '无',
  gallery: '白边',
  leica: '徕卡水印',
  hasselblad: '哈苏水印',
  film: '胶片',
  instant: '拍立得',
  blur_fill: '模糊背景',
  thin_keyline: '极简细线',
  cinema: '电影宽银幕',
  exhibition_label: '展签',
  slide_mount: '幻灯片',
  darkroom: '暗房相纸',
  postage_stamp: '邮票齿孔',
};
const COLOR_HEX = { white: '#ffffff', black: '#0e0e10', cream: '#f4efe6' };
const COLOR_NAMES = { white: '白', black: '黑', cream: '米白' };
const WEIGHT_NAMES = { thin: '细', medium: '中', thick: '粗' };
export const OUTPUT_CHOICES = [
  ['1:1', '1:1 · 方图'],
  ['4:5', '4:5 · Instagram 竖图'],
  ['3:4', '3:4 · 小红书'],
  ['2:3', '2:3 · 竖幅'],
  ['9:16', '9:16 · 快拍 · 短视频'],
  ['16:9', '16:9 · 横屏视频'],
  ['3:2', '3:2 · 横幅'],
  ['original', '照片原比例'],
];

function styleList(ed) {
  const list = ed.frames && ed.frames.styles;
  const arr = Array.isArray(list) ? list : Object.values(list || {});
  const order = ['none', 'leica', 'blur_fill', 'gallery', 'hasselblad', 'instant', 'film', 'thin_keyline', 'cinema', 'exhibition_label', 'slide_mount', 'darkroom', 'postage_stamp'];
  return arr.filter((s) => s && order.includes(s.id)).sort((a,b)=>order.indexOf(a.id)-order.indexOf(b.id)).map((s) => ({ id: s.id, name: FRAME_NAMES[s.id] || s.name || s.id, options: s.options || [], defaults: s.defaults || null }));
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
      type: 'button', class: `fcard${s.id === 'none' ? ' none' : ''}`, 'data-style': s.id, 'aria-pressed': String(cur.style === s.id),
      onclick: () => {
        if (ed.frame.style !== s.id) setFrame(view, selectFrame(ed.frame, s.id));
      },
    }, thumb, h('div', { class: 'nm' }, s.name));
  }));
  paintCards(view, items);

  const sizeLine = frameSizeLine(view);
  const rows = [h('div', { class: 'pane-head' }, h('h2', {}, '边框'), sizeLine), cards];
  if (cur.style !== 'none') {
    const ratios = h('select', { 'aria-label': '成品比例', onchange: (event) => setFrame(view, { output_ratio: event.target.value }) },
      ...OUTPUT_CHOICES.map(([value, label]) => h('option', { value, selected: cur.output_ratio === value }, label)));
    rows.push(h('label', { class: 'field-row' }, h('span', {}, '成品比例'), ratios));
    if (opts.includes('color')) {
      // 颜色只用色块（不再加文字按钮）；鼠标停在色块上看颜色名，选中的色块有外圈
      rows.push(h('div', { class: 'seg-row' }, h('span', { class: 'seg-label' }, '颜色'), h('div', { class: 'swrow' },
        ...(cur.style === 'slide_mount' ? ['cream', 'black'] : ['white', 'black', 'cream']).map((c) => swatch(COLOR_HEX[c], cur.color === c, () => setFrame(view, { color: c }), COLOR_NAMES[c])))));
    }
    if (opts.includes('weight')) {
      rows.push(seg(['thin', 'medium', 'thick'].map((w) => [w, WEIGHT_NAMES[w]]), cur.weight, (w) => setFrame(view, { weight: w }), { label: '宽度' }));
    }
    if (opts.includes('fmt')) {
      const formats=cur.style==='film'?[['135','135'],['120','120']]:[['polaroid','宝丽来'],['instax_square','方形'],['instax_wide','宽幅'],['instax_mini','迷你']];
      rows.push(seg(formats,cur.fmt,fmt=>setFrame(view,{fmt}),{label:'画幅'}));
    }
    if (opts.includes('table')) rows.push(seg([['light','灯台'],['black','黑色']],cur.table,table=>setFrame(view,{table}),{label:'背景'}));
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
      const updateText = (text) => {
        change(view, { text });
        sizeLine.update();
        if (cards.isConnected) paintCards(view, items);
      };
      liveText(input, {
        apply: () => updateText(clipText(input.value)),
        save: () => {
          updateText(ed.frame.text.trim()); // 前后的空格输入时留着，写下来时去掉
          save(view, ed);
        },
      });
      rows.push(h('div', { class: 'sec' }, '文字'), input);
    }
  }

  // 跟在样式和选项后面滚动，避免窄屏或矮窗口时盖住样式卡片。
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
