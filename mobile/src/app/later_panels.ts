// 裁剪、边框、LUT、曲线四页：按设计稿 03e–03h 摆好静态的控件，这一版全部停用，写「这项下一步做」。
// 这里没有任何算法，只有版式。名字和顺序跟桌面一样（crop.js、frame_panel.js、lut.js、curves.js）。

import { button, el } from './dom';

const SVG = 'http://www.w3.org/2000/svg';

function shell(tab: string): HTMLElement {
  const panel = el('div', 'panel later-panel');
  panel.dataset.later = tab;
  panel.append(el('p', 'later', '这项下一步做'));
  return panel;
}

function chipRow(names: string[], on: string | null): HTMLElement {
  const row = el('div', 'chips');
  for (const name of names) {
    const chip = button(name === on ? 'chip on' : 'chip', name);
    chip.disabled = true;
    row.append(chip);
  }
  return row;
}

function segment(names: string[], on: number, label: string): HTMLElement {
  const seg = el('div', 'seg');
  seg.setAttribute('role', 'group');
  seg.setAttribute('aria-label', label);
  names.forEach((name, index) => {
    const item = button(index === on ? 'on' : '', name);
    item.disabled = true;
    seg.append(item);
  });
  return seg;
}

function switchRow(title: string, detail: string): HTMLElement {
  const row = el('label', 'pad swrow');
  const text = el('div', 'grow');
  text.append(el('b', '', title), el('small', 'dim', detail));
  const input = document.createElement('input');
  input.type = 'checkbox';
  input.className = 'switch';
  input.disabled = true;
  row.append(text, input);
  return row;
}

/** 03e：比例、横竖、参考线、拉直。 */
export function cropPanel(): HTMLElement {
  const panel = shell('crop');
  panel.append(chipRow(['原始', '自由', '1:1', '4:5', '3:2', '16:9', 'XPan 65:24', '小红书 3:4'], '原始'));
  const row = el('div', 'pad row gap8');
  const guide = button('btn', '参考线：三等分');
  guide.disabled = true;
  row.append(segment(['横向', '竖向'], 0, '方向'), guide);
  const straighten = el('div', 'pad straighten');
  const head = el('div', 'row between');
  head.append(el('span', 'dim small', '拉直'), el('span', 'dim small num', '0.0°'));
  const ruler = document.createElement('input');
  ruler.type = 'range';
  ruler.min = '-10';
  ruler.max = '10';
  ruler.step = '0.1';
  ruler.value = '0';
  ruler.disabled = true;
  ruler.setAttribute('aria-label', '拉直');
  const ticks = el('div', 'ticks num');
  for (const text of ['−10°', '−5°', '0°', '5°', '10°']) ticks.append(el('span', '', text));
  straighten.append(head, ruler, ticks);
  panel.append(row, straighten);
  return panel;
}

/** 03f 的样式小卡：底色、照片框和一两条示意线。 */
function frameCard(name: string, paper: string, photo: [number, number, number, number], marks: string[]): HTMLButtonElement {
  const card = button('rc', '');
  card.disabled = true;
  const thumb = el('div', 'th fr');
  thumb.style.background = paper;
  const pic = el('i', 'fp');
  const [left, top, width, height] = photo;
  pic.style.cssText = `left:${left}%;top:${top}%;width:${width}%;height:${height}%`;
  thumb.append(pic);
  for (const css of marks) {
    const mark = el('i', 'fm');
    mark.style.cssText = css;
    thumb.append(mark);
  }
  card.append(thumb, el('div', 'nm', name));
  return card;
}

/** 03f：八种边框（桌面 styleList 的顺序）、颜色、粗细、标志 / 参数 / 日期、时间戳。 */
export function framePanel(): HTMLElement {
  const panel = shell('frame');
  const strip = el('div', 'strip inpanel');
  const paper = '#f4f3f0';
  const cards: Array<[string, string, [number, number, number, number], string[]]> = [
    ['无', '#202020', [11, 9, 78, 82], []],
    ['徕卡水印', paper, [9, 9, 82, 61], ['left:11%;bottom:10%;width:7%;height:9%;border-radius:50%;background:#d6281f', 'left:23%;bottom:12%;width:23%;height:5%;background:#444', 'right:11%;bottom:12%;width:30%;height:5%;background:#999']],
    ['模糊背景', '#3a3a3a', [14, 15, 72, 70], []],
    ['白边', paper, [10, 14, 80, 72], []],
    ['哈苏水印', paper, [9, 9, 82, 61], ['left:34%;bottom:18%;width:32%;height:5%;background:#444', 'left:27%;bottom:9%;width:46%;height:3%;background:#aaa']],
    ['拍立得', paper, [11, 11, 78, 57], []],
    ['胶片', '#111', [11, 18, 78, 64], ['left:0;right:0;top:4%;height:7%;background:repeating-linear-gradient(90deg,#d8d8d8 0 4px,transparent 4px 8px)', 'left:0;right:0;bottom:4%;height:7%;background:repeating-linear-gradient(90deg,#d8d8d8 0 4px,transparent 4px 8px)']],
    ['极简细线', paper, [15, 20, 70, 60], ['left:9%;top:12%;right:9%;bottom:12%;border:1px solid #888;background:transparent']],
  ];
  for (const [name, background, photo, marks] of cards) strip.append(frameCard(name, background, photo, marks));
  const colors = el('div', 'pad row');
  colors.append(el('span', 'dim small', '颜色'));
  ['#f4f3f0', '#141414', '#ece3d0'].forEach((color, index) => {
    const swatch = button(index === 0 ? 'swc on' : 'swc', '');
    swatch.disabled = true;
    swatch.setAttribute('aria-label', ['白色', '黑色', '米色'][index]);
    swatch.append(el('i'));
    (swatch.firstElementChild as HTMLElement).style.background = color;
    colors.append(swatch);
  });
  const weight = segment(['细', '中', '粗'], 1, '粗细');
  weight.classList.add('push');
  colors.append(weight);
  panel.append(strip, colors, chipRow(['标志', '参数', '日期'], null), switchRow('时间戳', '印在照片上：橙色数码管，右下角'));
  return panel;
}

function lutCard(name: string, tint: string | null): HTMLButtonElement {
  const card = button('rc', '');
  card.disabled = true;
  const thumb = el('div', 'th');
  if (tint) thumb.style.background = tint;
  else thumb.textContent = '×';
  card.append(thumb, el('div', 'nm', name));
  return card;
}

/** 03g：不用 LUT 和示例、输入色彩空间、说明、导入。 */
export function lutPanel(): HTMLElement {
  const panel = shell('lut');
  const strip = el('div', 'strip inpanel');
  strip.append(
    lutCard('不用 LUT', null),
    lutCard('胶片暖调', 'linear-gradient(135deg,#6b4a2b,#d9a560)'),
    lutCard('青橙电影', 'linear-gradient(135deg,#1f5a66,#e0904a)'),
    lutCard('黑金', 'linear-gradient(135deg,#111,#b8963e)'),
  );
  const space = button('lrow pad', '');
  space.disabled = true;
  const text = el('div');
  text.append(el('b', '', '输入色彩空间'), el('small', '', 'sRGB · 文件已写明'));
  space.append(text);
  const add = button('btn block', '导入 LUT（.cube 文件）');
  add.disabled = true;
  const addRow = el('div', 'pad');
  addRow.append(add);
  panel.append(strip, space, el('p', 'pad faint small', 'LUT 会拟合成一个配方，只在这台 iPhone 上用。效果是电脑预测，不写入相机。'), addRow);
  return panel;
}

/** 03h：主曲线 / 红 / 绿 / 蓝、重置此通道、曲线图（还没有调过，所以是对角线）。 */
export function curvesPanel(): HTMLElement {
  const panel = shell('curves');
  const row = el('div', 'row curves');
  const column = el('div', 'col');
  for (const [name, color] of [['主曲线', 'var(--text-1)'], ['红', '#ff7a70'], ['绿', '#5cc882'], ['蓝', '#80b6ff']] as const) {
    const item = button(name === '主曲线' ? 'curve-btn on' : 'curve-btn', name);
    item.style.color = color;
    item.disabled = true;
    column.append(item);
  }
  const reset = button('btn', '重置此通道');
  reset.disabled = true;
  column.append(reset);
  const size = 220;
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', `0 0 ${size} ${size}`);
  svg.setAttribute('class', 'curve');
  svg.setAttribute('aria-label', '曲线');
  let paths = '';
  for (let i = 1; i < 4; i++) paths += `<path d="M${(size * i) / 4} 0V${size}M0 ${(size * i) / 4}H${size}" stroke="rgba(255,255,255,.07)" stroke-width="1"/>`;
  paths += `<path d="M0 ${size}L${size} 0" stroke="#666" stroke-width="2"/>`;
  paths += `<circle cx="0" cy="${size}" r="6" fill="#888"/><circle cx="${size}" cy="0" r="6" fill="#888"/>`;
  svg.innerHTML = paths;
  row.append(column, svg);
  panel.append(row);
  return panel;
}
