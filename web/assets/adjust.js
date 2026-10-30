// 大图「调整」标签（仅电脑）：曝光、色温、色调、对比、高光、阴影，以及可以拖动的主曲线（最多 16 个点）。
// 这些只在电脑上生效，不会写进相机配方。

import { h, toast } from './dom.js';
import { CURVE_MAX, adjustBody, curveBody, persist, schedulePreview } from './preview.js';

const DIAG = [[0, 0], [255, 255]];
const HIT = 14; // 点按在 14 个单位（曲线坐标 0–255）以内算点中了

const TONE_ROWS = [
  ['contrast', '画面对比'],
  ['highlight', '高光'],
  ['shadow', '阴影'],
];

export function adjustPanel(view) {
  const ed = view.ed;
  const nodes = [
    h('h2', {}, '调整（仅电脑）'),
    h('p', { class: 'warn-text small' }, '⚠ 这里的调整只在电脑上生效，不会写进相机配方。'),
    exposureRow(view),
    h('div', { class: 'field' }, h('span', { class: 'small dim' }, '白平衡（电脑上的近似）'),
      intRow(view, '色温', 'warm', ed.adj.warm),
      intRow(view, '色调', 'tint', ed.adj.tint)),
    h('div', { class: 'field' }, h('span', { class: 'small dim' }, '画面（电脑上的调子）'),
      TONE_ROWS.map(([key, label]) => intRow(view, label, key, ed.adj[key]))),
    h('div', { class: 'field' }, h('span', { class: 'small dim' }, '曲线（亮 ↔ 暗）：点空白处加点，拖动点来改，双击中间的点删掉。两端固定在左下和右上。'),
      curveEditor(view)),
    resetButton(view),
  ];
  return h('div', { class: 'edit-panel' }, ...nodes);
}

function exposureRow(view) {
  const ed = view.ed;
  const out = h('output', {}, fmtEv(ed.adj.exposure_ev));
  const input = h('input', {
    type: 'range',
    min: -5,
    max: 5,
    step: 0.1,
    value: ed.adj.exposure_ev,
    'aria-label': '曝光',
    title: '双击归零',
    oninput: (e) => {
      ed.adj.exposure_ev = Math.round(Number(e.target.value) * 10) / 10;
      out.textContent = fmtEv(ed.adj.exposure_ev);
      schedulePreview(view);
    },
    onchange: () => saveAdjust(view),
    ondblclick: () => {
      ed.adj.exposure_ev = 0;
      input.value = 0;
      out.textContent = fmtEv(0);
      saveAdjust(view);
    },
  });
  return h('div', { class: 'knob-row' }, h('div', { class: 'knob-head' }, h('span', {}, '曝光（EV）'), h('span', { class: 'knob-tag' }, '仅电脑')), h('div', { class: 'field-row' }, input, out));
}

function intRow(view, label, key, value) {
  const ed = view.ed;
  const out = h('output', {}, fmtSigned(value));
  const input = h('input', {
    type: 'range',
    min: -100,
    max: 100,
    step: 1,
    value,
    'aria-label': label,
    title: '双击归零',
    oninput: (e) => {
      ed.adj[key] = Number(e.target.value);
      out.textContent = fmtSigned(ed.adj[key]);
      schedulePreview(view);
    },
    onchange: () => saveAdjust(view),
    ondblclick: () => {
      ed.adj[key] = 0;
      input.value = 0;
      out.textContent = fmtSigned(0);
      saveAdjust(view);
    },
  });
  return h('div', { class: 'knob-row' }, h('div', { class: 'knob-head' }, h('span', {}, label), h('span', { class: 'knob-tag' }, '仅电脑')), h('div', { class: 'field-row' }, input, out));
}

function saveAdjust(view) {
  persist(view, { adjust: adjustBody(view.ed) });
  schedulePreview(view, { final: true });
}

function fmtEv(v) {
  return `${v > 0 ? '+' : ''}${v.toFixed(1)} EV`;
}

function fmtSigned(v) {
  return v > 0 ? `+${v}` : `${v}`;
}

function isIdentity(points) {
  return points.every(([x, y]) => x === y);
}

// 曲线编辑：SVG 坐标 0–255，y 向上。ed.curve 为 null 表示恒等直线（没有曲线）。
function curveEditor(view) {
  const ed = view.ed;
  // 用 innerHTML 生成 SVG，省得在代码里写命名空间地址（界面必须完全离线，见 test_web_smoke）
  const holder = h('div', {});
  holder.innerHTML = '<svg viewBox="0 0 255 255" class="curve" role="group" aria-label="主曲线"></svg>';
  const svg = holder.firstChild;
  const count = h('span', { class: 'small dim' });

  const points = () => (ed.curve ? ed.curve : DIAG).map((p) => [...p]);
  let working = null; // 拖动期间的工作副本
  let drag = null;

  const pts = () => working || points();

  const draw = () => {
    const list = pts();
    const line = list.map(([x, y]) => `${x},${255 - y}`).join(' ');
    const dots = list.map(([x, y], i) => `<circle cx="${x}" cy="${255 - y}" r="6" class="curve-point" data-i="${i}"></circle>`).join('');
    svg.innerHTML =
      '<rect x="0" y="0" width="255" height="255" class="curve-bg"></rect>' +
      '<line x1="0" y1="255" x2="255" y2="0" class="curve-diag"></line>' +
      `<polyline points="${line}" class="curve-line"></polyline>` +
      dots;
    count.textContent = `${list.length} / ${CURVE_MAX} 个点`;
  };

  const toPoint = (e) => {
    const r = svg.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * 255;
    const y = 255 - ((e.clientY - r.top) / r.height) * 255;
    return [Math.round(Math.min(255, Math.max(0, x))), Math.round(Math.min(255, Math.max(0, y)))];
  };

  const nearest = ([x, y]) => {
    let best = -1;
    let bestD = HIT;
    pts().forEach(([px, py], i) => {
      const d = Math.hypot(px - x, py - y);
      if (d <= bestD) {
        best = i;
        bestD = d;
      }
    });
    return best;
  };

  const commit = (changed) => {
    if (!changed) return;
    const list = working || points();
    working = null;
    ed.curve = isIdentity(list) ? null : list;
    persist(view, { curve: curveBody(view.ed) });
    schedulePreview(view, { final: true });
    draw();
  };

  svg.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    const p = toPoint(e);
    let i = nearest(p);
    let added = false;
    working = points();
    if (i < 0) {
      if (working.length >= CURVE_MAX) {
        toast(`最多 ${CURVE_MAX} 个点`, 'info');
        working = null;
        return;
      }
      i = working.findIndex(([x]) => x > p[0]);
      if (i < 0) i = working.length;
      working.splice(i, 0, p);
      added = true;
      draw();
    }
    drag = { i, added, moved: false };
    svg.setPointerCapture(e.pointerId);
    e.preventDefault();
  });

  svg.addEventListener('pointermove', (e) => {
    if (!drag || !working) return;
    const [x, y] = toPoint(e);
    const { i } = drag;
    const last = working.length - 1;
    const lo = i === 0 ? 0 : working[i - 1][0] + 1;
    const hi = i === last ? 255 : working[i + 1][0] - 1;
    const nx = i === 0 || i === last ? working[i][0] : Math.min(hi, Math.max(lo, x));
    const ny = Math.min(255, Math.max(0, y));
    if (nx !== working[i][0] || ny !== working[i][1]) drag.moved = true;
    working[i] = [nx, ny];
    draw();
    schedulePreview(view);
  });

  const end = () => {
    if (!drag) return;
    const changed = drag.moved || drag.added;
    drag = null;
    commit(changed);
  };
  svg.addEventListener('pointerup', end);
  svg.addEventListener('pointercancel', end);

  svg.addEventListener('dblclick', (e) => {
    const i = nearest(toPoint(e));
    const list = points();
    if (i <= 0 || i >= list.length - 1 || list.length <= 2) return;
    list.splice(i, 1);
    working = list;
    commit(true);
  });

  draw();
  return h('div', { class: 'curve-wrap' }, svg, count);
}

function resetButton(view) {
  let armed = false;
  let timer = null;
  const btn = h('button', { class: 'btn', type: 'button', title: '清掉画面调子和曲线，配方不动' }, '全部归零');
  btn.addEventListener('click', () => {
    if (!armed) {
      armed = true;
      btn.textContent = '再点一次：全部归零';
      btn.classList.add('danger');
      clearTimeout(timer);
      timer = setTimeout(() => {
        armed = false;
        btn.textContent = '全部归零';
        btn.classList.remove('danger');
      }, 4000);
      return;
    }
    clearTimeout(timer);
    const ed = view.ed;
    Object.assign(ed.adj, { exposure_ev: 0, warm: 0, tint: 0, contrast: 0, highlight: 0, shadow: 0 });
    ed.curve = null;
    persist(view, { adjust: adjustBody(ed), curve: null });
    schedulePreview(view, { final: true });
    ed.redraw?.();
  });
  return btn;
}
