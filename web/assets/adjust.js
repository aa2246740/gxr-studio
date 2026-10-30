// 大图「调整」标签（仅电脑）：曝光、对比度、高光、阴影、黑点、饱和度、自然饱和度、色温、色调、暗角，以及曲线。
// 这些只在电脑上生效，不会写进相机配方。黑点、饱和度、自然饱和度、暗角要新版后端，旧版显示「暂不可用」。
// 曲线拖动时，工作副本直接写进 ed.curve，预览请求用的就是拖动中的这条曲线（DIAG 原因 4）；松手才保存。

import { h } from './dom.js';
import { CURVE_MAX, adjustBody, curveBody, persist, schedulePreview, beginChange, endChange } from './preview.js';
import { slider, notReady } from './controls.js';

const DIAG = [[0, 0], [255, 255]];
const HIT = 14; // 点按在 14 个单位（曲线坐标 0–255）以内算点中了
const NS = (() => { const t = document.createElement('template'); t.innerHTML = '<svg></svg>'; return t.content.firstElementChild.namespaceURI; })();

const fmtEv = (v) => `${v > 0 ? '+' : ''}${Number(v).toFixed(1)}`;
const fmtSigned = (v) => (v > 0 ? `+${v}` : `${v}`);

// 一个滑块：拖动中只改 ed.adj 并请求预览；松手保存（PUT /edit adjust）
function adjRow(view, { key, label, min, max, step = 1, fmt = fmtSigned }) {
  const ed = view.ed;
  return slider({
    label,
    min,
    max,
    step,
    value: ed.adj[key],
    fmt,
    dflt: 0,
    onBegin: () => beginChange(view),
    onInput: (v) => {
      ed.adj[key] = v;
      schedulePreview(view);
    },
    onCommit: (v) => {
      ed.adj[key] = v;
      persist(view, { adjust: adjustBody(ed) }).then(() => endChange(view));
      schedulePreview(view, { full: true });
    },
  }).el;
}

export function adjustPanel(view) {
  const ed = view.ed;
  const v2 = ed.v2;
  const rows = [
    h('div', { class: 'pane-head' }, h('h2', {}, '调整')),
    adjRow(view, { key: 'exposure_ev', label: '曝光', min: -5, max: 5, step: 0.1, fmt: fmtEv }),
    adjRow(view, { key: 'contrast', label: '对比度', min: -100, max: 100 }),
    adjRow(view, { key: 'highlight', label: '高光', min: -100, max: 100 }),
    adjRow(view, { key: 'shadow', label: '阴影', min: -100, max: 100 }),
  ];
  if (v2) rows.push(adjRow(view, { key: 'black_point', label: '黑点', min: -100, max: 100 }));
  rows.push(adjRow(view, { key: 'warm', label: '色温', min: -100, max: 100 }));
  rows.push(adjRow(view, { key: 'tint', label: '色调', min: -100, max: 100 }));
  if (v2) {
    rows.push(adjRow(view, { key: 'saturation', label: '饱和度', min: -100, max: 100 }));
    rows.push(adjRow(view, { key: 'vibrance', label: '自然饱和度', min: -100, max: 100 }));
    rows.push(adjRow(view, { key: 'vignette', label: '暗角', min: -100, max: 100 }));
  } else {
    rows.push(notReady('黑点、饱和度、自然饱和度、暗角'));
  }
  rows.push(h('div', { class: 'sec' }, '曲线'));
  rows.push(curveEditor(view));
  rows.push(h('div', { class: 'sticky-foot' }, resetButton(view)));
  return h('div', { class: 'pane edit-panel' }, ...rows);
}

function isIdentity(points) {
  return points.every(([x, y]) => x === y);
}

function svgEl(tag, attrs = {}) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  return n;
}

// 曲线编辑：SVG 坐标 0–255，y 向上。ed.curve 为 null 表示恒等直线。
// 拖动时工作副本（working）同时写进 ed.curve，所以 schedulePreview 发出去的请求带的就是拖动中的曲线。
function curveEditor(view) {
  const ed = view.ed;
  const svg = svgEl('svg', { viewBox: '0 0 255 255', class: 'curve', role: 'group', 'aria-label': '曲线' });
  const count = h('span', { class: 'small dim' });
  const wrap = h('div', { class: 'curve-wrap' }, svg, count);
  let working = null;
  let drag = null;

  const pts = () => working || (ed.curve ? ed.curve : DIAG).map((p) => [...p]);

  const draw = () => {
    const list = pts();
    svg.replaceChildren(
      svgEl('rect', { x: 0, y: 0, width: 255, height: 255, class: 'curve-bg' }),
      svgEl('line', { x1: 0, y1: 255, x2: 255, y2: 0, class: 'curve-diag' }),
      svgEl('polyline', { points: list.map(([x, y]) => `${x},${255 - y}`).join(' '), class: 'curve-line' }),
      ...list.map(([x, y], i) => svgEl('circle', { cx: x, cy: 255 - y, r: 6, class: 'curve-point', 'data-i': i })),
    );
    count.textContent = `${list.length} / ${CURVE_MAX}`;
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

  // 把工作副本写进 ed.curve（拖动中的请求要用到）
  const setLive = () => {
    ed.curve = isIdentity(working) ? null : working.map((p) => [...p]);
  };

  svg.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    const p = toPoint(e);
    let i = nearest(p);
    let added = false;
    beginChange(view);
    working = pts();
    if (i < 0) {
      if (working.length >= CURVE_MAX) {
        endChange(view);
        working = null;
        return;
      }
      i = working.findIndex(([x]) => x > p[0]);
      if (i < 0) i = working.length;
      working.splice(i, 0, p);
      added = true;
      setLive();
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
    setLive();
    draw();
    schedulePreview(view);
  });

  const end = () => {
    if (!drag) return;
    const changed = drag.moved || drag.added;
    drag = null;
    const list = working;
    working = null;
    if (!changed) {
      endChange(view);
      return;
    }
    ed.curve = isIdentity(list) ? null : list.map((p) => [...p]);
    persist(view, { curve: curveBody(ed) }).then(() => endChange(view));
    schedulePreview(view, { full: true });
    draw();
  };
  svg.addEventListener('pointerup', end);
  svg.addEventListener('pointercancel', end);

  svg.addEventListener('dblclick', (e) => {
    const i = nearest(toPoint(e));
    const list = (ed.curve ? ed.curve : DIAG).map((p) => [...p]);
    if (i <= 0 || i >= list.length - 1 || list.length <= 2) return;
    beginChange(view);
    list.splice(i, 1);
    ed.curve = isIdentity(list) ? null : list;
    persist(view, { curve: curveBody(ed) }).then(() => endChange(view));
    schedulePreview(view, { full: true });
    draw();
  });

  draw();
  return wrap;
}

// 重置：只清调整和曲线，不动配方（COPY 3.2）
function resetButton(view) {
  const btn = h('button', { class: 'btn', type: 'button' }, '重置');
  btn.addEventListener('click', () => {
    const ed = view.ed;
    beginChange(view);
    Object.assign(ed.adj, { exposure_ev: 0, warm: 0, tint: 0, contrast: 0, highlight: 0, shadow: 0, black_point: 0, saturation: 0, vibrance: 0, vignette: 0 });
    ed.curve = null;
    persist(view, { adjust: adjustBody(ed), curve: null }).then(() => endChange(view));
    schedulePreview(view, { full: true });
    view.redrawSide?.();
  });
  return btn;
}
