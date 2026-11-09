import { h } from './dom.js';
import { persist, schedulePreview, beginChange, endChange } from './preview.js';
import { CURVE_MAX, CURVE_CHANNELS, IDENTITY_CURVE, curveBody, normalizeCurves } from './curves.js';

const HIT = 14;
const NS = (() => { const t = document.createElement('template'); t.innerHTML = '<svg></svg>'; return t.content.firstElementChild.namespaceURI; })();

function svgEl(tag, attrs = {}) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  return n;
}

// 拖动中的点写回 ed.curve 对应通道；预览、直方图和保存都拿同一份草稿。
export function curveEditor(view) {
  const ed = view.ed;
  let channel = ed.curveChannel || 'master';
  const svg = svgEl('svg', { viewBox: '0 0 255 255', class: 'curve', role: 'group', 'aria-label': '曲线' });
  const count = h('span', { class: 'small dim' });
  const switches = CURVE_CHANNELS.map(([key, label]) => h('button', {
    type: 'button', class: 'btn sm', 'data-channel': key, 'aria-pressed': 'false',
    onclick: () => {
      channel = ed.curveChannel = key;
      draw();
    },
  }, label));
  const resetOne = h('button', { type: 'button', class: 'btn ghost sm', onclick: () => reset(false) }, '重置此通道');
  const resetAll = h('button', { type: 'button', class: 'btn ghost sm', onclick: () => reset(true) }, '重置全部曲线');
  const wrap = h('div', { class: 'curve-wrap' },
    h('div', { class: 'curve-channels', role: 'group', 'aria-label': '曲线通道' }, ...switches),
    svg, h('div', { class: 'curve-actions' }, resetOne, resetAll), count);
  let working = null;
  let drag = null;

  const pts = () => working || (ed.curve?.[channel] || IDENTITY_CURVE).map((p) => [...p]);
  const line = (points, key, ghost = false) => svgEl('polyline', {
    points: points.map(([x, y]) => `${x},${255 - y}`).join(' '),
    class: `curve-line${ghost ? ' curve-ghost' : ''}`, 'data-channel': key,
  });

  const draw = () => {
    const list = pts();
    const label = CURVE_CHANNELS.find(([key]) => key === channel)[1];
    svg.setAttribute('data-channel', channel);
    svg.setAttribute('aria-label', channel === 'master' ? label : `${label}曲线`);
    for (const btn of switches) {
      const changed = Boolean(ed.curve?.[btn.dataset.channel]);
      btn.setAttribute('aria-pressed', String(btn.dataset.channel === channel));
      btn.setAttribute('data-changed', String(changed));
      btn.title = changed ? '已调整' : '未调整';
    }
    resetOne.disabled = !ed.curve?.[channel];
    resetAll.disabled = !ed.curve;
    svg.replaceChildren(
      svgEl('rect', { x: 0, y: 0, width: 255, height: 255, class: 'curve-bg' }),
      svgEl('line', { x1: 0, y1: 255, x2: 255, y2: 0, class: 'curve-diag' }),
      ...CURVE_CHANNELS.filter(([key]) => key !== channel && ed.curve?.[key]).map(([key]) => line(ed.curve[key], key, true)),
      line(list, channel),
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

  const setPoints = (points) => {
    ed.curve = normalizeCurves({ ...ed.curve, [channel]: points });
  };

  const save = () => {
    persist(view, { curve: curveBody(ed) }).then(() => endChange(view));
    schedulePreview(view, { full: true });
    draw();
  };

  const reset = (all) => {
    beginChange(view);
    ed.curve = all ? null : normalizeCurves({ ...ed.curve, [channel]: null });
    save();
  };

  svg.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    const p = toPoint(e);
    let i = nearest(p);
    let added = false;
    beginChange(view);
    working = pts();
    if (i < 0) {
      if (working.length >= CURVE_MAX || working.some(([x]) => x === p[0])) {
        endChange(view);
        working = null;
        return;
      }
      i = working.findIndex(([x]) => x > p[0]);
      if (i < 0) i = working.length;
      working.splice(i, 0, p);
      added = true;
      setPoints(working);
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
    setPoints(working);
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
    setPoints(list);
    save();
  };
  svg.addEventListener('pointerup', end);
  svg.addEventListener('pointercancel', end);

  svg.addEventListener('dblclick', (e) => {
    const i = nearest(toPoint(e));
    const list = pts();
    if (i <= 0 || i >= list.length - 1 || list.length <= 2) return;
    beginChange(view);
    list.splice(i, 1);
    setPoints(list);
    save();
  });

  draw();
  return wrap;
}
