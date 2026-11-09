// 大图「调整」标签（仅电脑）：曝光、对比度、高光、阴影、黑点、饱和度、自然饱和度、色温、色调、暗角，以及曲线。
// 这些只在电脑上生效，不会写进相机配方。黑点、饱和度、自然饱和度、暗角要新版后端，旧版显示「暂不可用」。
// 曲线拖动时，工作副本直接写进 ed.curve，预览请求用的就是拖动中的这条曲线（DIAG 原因 4）；松手才保存。

import { h } from './dom.js';
import { adjustBody, persist, schedulePreview, beginChange, endChange } from './preview.js';
import { slider, notReady } from './controls.js';
import { curveEditor } from './curve_editor.js';

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
    onCancel: () => endChange(view),
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
  rows.push(h('div', { class: 'foot' }, resetButton(view)));
  return h('div', { class: 'pane edit-panel' }, ...rows);
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
