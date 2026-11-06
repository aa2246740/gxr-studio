// 裁切与纠正里的「纠正」：拉直（刻度盘）、垂直、水平、镜头畸变、旋转 90°、左右翻转、自动、还原。
// 新版后端（v2）才有的项，旧版显示「还没准备好」。拉直两版都能用：旧版写 /crop 的 angle_deg（只认 ±10°，超出时后端的原因会显示出来）。
// 拖动时只改画面（不保存），松手再保存（DIAG 原因 4 同样的做法：拖动中的值就是请求里发出去的值）。

import { h, button, toast, toastError } from './dom.js';
import { beginChange, endChange, saveCrop, persist, schedulePreview, validRect, resetCropRemote, geometryOf } from './preview.js';
import { cropBody, orientationNow, rectArray } from './crop.js';
import { slider, notReady } from './controls.js';
import { fitInto, rotateRect90, flipRect, fmtDeg, clamp } from './geom.js';
import { DEFAULT_GEOMETRY } from './finish.js';
import { api } from './api.js';

const ANGLE_MIN = -45;
const ANGLE_MAX = 45;

export function straightOf(view) {
  return geometryOf(view).straighten || 0;
}

// 拉直的值：v2 存在几何里，旧版存在 crop.angle_deg 里
function setStraightValue(view, v) {
  if (view.ed.v2) view.ed.geometry = { ...view.ed.geometry, straighten: v };
  else view.crop.angle_deg = v;
}

// 拖动中：只改画面和框（不保存）。框收进新的有效画面。
function setStraightLive(view, v) {
  const value = clamp(Math.round(v * 10) / 10, ANGLE_MIN, ANGLE_MAX);
  setStraightValue(view, value);
  if (view.canvas && view.crop) view.crop.rect = fitInto(view.crop.rect, validRect(view, view.canvas)).rect;
  view.refreshAngle?.();
  view.onRectDrag?.();
  if (view.ed.v2) schedulePreview(view, { stage: 'uncropped' });
  else view.onChange?.();
}

// 松手：保存。v2：先写几何，再看框要不要收进新的有效画面；旧版：一次 /crop 写完（拉直超范围时后端报错，会显示出来）。
export async function commitStraight(view) {
  if (view.ed.v2) {
    await persist(view, { geometry: view.ed.geometry });
    await afterGeometry(view);
  } else if (view.crop) {
    const fitted = view.canvas ? fitInto(view.crop.rect, validRect(view, view.canvas)).rect : view.crop.rect;
    await saveCrop(view, cropBody(view, { preset: view.crop.preset, rect: rectArray(fitted) }, orientationNow(view)));
  }
  endChange(view);
}

// 几何变了（拉直、透视、畸变、旋转、翻转）：框如果超出新的有效画面，收进来并保存
async function afterGeometry(view) {
  // 等保存时照片已经关掉：不再根据旧界面的框补写（写入已经绑定在这张照片上，前面那笔照常完成）
  if (view.disposed || !view.canvas || !view.crop) return;
  const fitted = fitInto(view.crop.rect, validRect(view, view.canvas));
  if (fitted.changed) view.crop.rect = fitted.rect;
  // 框和服务端一样就不用写；拖动中已经收进去的框（没写回）也要写
  if (JSON.stringify(view.crop.rect) === view.crop.savedKey) return;
  await saveCrop(view, cropBody(view, { preset: view.crop.preset, rect: rectArray(view.crop.rect) }, orientationNow(view)));
}

// 键盘 ↑/↓：每次 0.5°（裁切中，不在滑块上时）
export function nudgeStraighten(view, delta) {
  beginChange(view);
  setStraightLive(view, straightOf(view) + delta);
  commitStraight(view);
}

// 旋转 90°（逆时针）：只有新版能做。框跟着画面转。
export async function rotate90(view) {
  if (!view.ed || !view.ed.v2 || !view.crop) return;
  beginChange(view);
  const g = view.ed.geometry;
  view.ed.geometry = { ...g, rotate90: (g.rotate90 + 1) % 4 };
  view.crop.rect = rotateRect90(view.crop.rect);
  view.canvas = null;
  // 松手后的纠正预览用 preview 尺寸（PRD 第 11 节；full 太慢，见 V2-15）
  schedulePreview(view, { stage: 'uncropped' });
  view.onChange?.();
  await persist(view, { geometry: view.ed.geometry });
  await afterGeometry(view);
  endChange(view);
}

export async function flipHorizontal(view) {
  if (!view.ed || !view.ed.v2 || !view.crop) return;
  beginChange(view);
  const g = view.ed.geometry;
  view.ed.geometry = { ...g, flip: !g.flip };
  view.crop.rect = flipRect(view.crop.rect);
  schedulePreview(view, { stage: 'uncropped' });
  view.onChange?.();
  await persist(view, { geometry: view.ed.geometry });
  await afterGeometry(view);
  endChange(view);
}

// 垂直、水平、畸变这三个滑块（新版）
function geomSlider(view, key, label, ends) {
  return slider({
    label,
    min: -100,
    max: 100,
    step: 1,
    value: view.ed.geometry[key] || 0,
    fmt: (v) => (v > 0 ? `+${v}` : `${v}`),
    ends,
    dflt: 0,
    onBegin: () => beginChange(view),
    onCancel: () => endChange(view),
    onInput: (v) => {
      view.ed.geometry = { ...view.ed.geometry, [key]: v };
      schedulePreview(view, { stage: 'uncropped' });
    },
    onCommit: (v) => {
      view.ed.geometry = { ...view.ed.geometry, [key]: v };
      persist(view, { geometry: view.ed.geometry }).then(() => afterGeometry(view)).then(() => endChange(view));
    },
  }).el;
}

// 自动：电脑找地平线和竖直线，给出建议值（没把握就不改）
export async function autoCorrect(view) {
  try {
    const r = await api(`/api/photos/${view.photo.id}/geometry/auto`, { method: 'POST' });
    if (!r.confident) {
      toast(r.message || '未检测到明显的水平线', 'info');
      return;
    }
    beginChange(view);
    view.ed.geometry = { ...view.ed.geometry, straighten: r.straighten, vertical: r.vertical };
    await persist(view, { geometry: view.ed.geometry });
    await afterGeometry(view);
    endChange(view);
  } catch (e) {
    toastError(e, '无法自动纠正这张照片。请重试。');
  }
}

// 还原裁切和纠正：比例、拉直、透视、畸变、旋转、翻转、框都回到原图。不动配方、调整、边框、时间戳。
export async function resetCorrections(view) {
  beginChange(view);
  if (view.ed.v2) {
    view.ed.geometry = { ...DEFAULT_GEOMETRY };
    await persist(view, { geometry: view.ed.geometry });
  }
  await resetCropRemote(view);
  endChange(view);
}

// ---- 刻度盘 -------------------------------------------------------------------

const PX_PER_DEG = 12;

function paintRuler(box, v) {
  box.querySelectorAll('.tick, .tnum').forEach((n) => n.remove());
  const W = box.clientWidth || 340;
  const c = W / 2;
  for (let d = Math.floor(v - c / PX_PER_DEG) - 1; d <= Math.ceil(v + c / PX_PER_DEG) + 1; d++) {
    if (d < ANGLE_MIN || d > ANGLE_MAX) continue;
    const x = c + (d - v) * PX_PER_DEG;
    if (x < -2 || x > W + 2) continue;
    const cls = d % 10 === 0 ? 'tick n' : d % 5 === 0 ? 'tick l' : 'tick';
    box.append(h('div', { class: cls, style: { left: `${x}px` } }));
    if (d % 10 === 0) box.append(h('div', { class: 'tnum', style: { left: `${x}px` } }, String(d)));
  }
  box.classList.toggle('out', Math.abs(v) >= ANGLE_MAX);
}

function ruler(view) {
  const v0 = straightOf(view);
  const box = h('div', {
    class: 'ruler',
    tabindex: '0',
    role: 'slider',
    'aria-label': '拉直',
    'aria-valuemin': ANGLE_MIN,
    'aria-valuemax': ANGLE_MAX,
    'aria-valuenow': v0,
    'aria-valuetext': fmtDeg(v0),
  }, h('div', { class: 'pointer' }));
  view.rulerEl = box;
  // 下一帧再画：这时候可能已经离开了这张照片（view 的编辑记录已经清掉），所以先看看还在不在
  requestAnimationFrame(() => {
    if (box.isConnected && view.ed) paintRuler(box, straightOf(view));
  });

  let drag = null;
  let keyHeld = false;
  box.addEventListener('pointerdown', (e) => {
    box.setPointerCapture(e.pointerId);
    beginChange(view);
    drag = { x: e.clientX, v: straightOf(view) };
  });
  box.addEventListener('pointermove', (e) => {
    if (!drag) return;
    setStraightLive(view, drag.v + (e.clientX - drag.x) / PX_PER_DEG);
    paintRuler(box, straightOf(view));
  });
  const up = () => {
    if (!drag) return;
    drag = null;
    commitStraight(view);
  };
  box.addEventListener('pointerup', up);
  box.addEventListener('pointercancel', up);
  box.addEventListener('dblclick', () => {
    beginChange(view);
    setStraightLive(view, 0);
    paintRuler(box, 0);
    commitStraight(view);
  });
  box.addEventListener('keydown', (e) => {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) return;
    e.preventDefault();
    const step = e.key === 'ArrowLeft' || e.key === 'ArrowRight' ? 0.1 : 0.5;
    const sign = e.key === 'ArrowRight' || e.key === 'ArrowUp' ? 1 : -1;
    if (!e.repeat) beginChange(view);
    keyHeld = true;
    setStraightLive(view, straightOf(view) + sign * step);
    paintRuler(box, straightOf(view));
  });
  const keyDone = () => {
    if (!keyHeld) return;
    keyHeld = false;
    commitStraight(view);
  };
  box.addEventListener('keyup', keyDone);
  // 按着方向键时焦点离开（keyup 发到了别处）：照样保存，这一步也就结束了（Y02）
  box.addEventListener('blur', keyDone);
  return box;
}

// 刻度盘上方的数字（随拖动更新）
function angleReadout(view) {
  const out = h('output', {}, fmtDeg(straightOf(view)));
  view.refreshAngle = () => {
    out.textContent = fmtDeg(straightOf(view));
    if (view.rulerEl) {
      view.rulerEl.setAttribute('aria-valuenow', String(straightOf(view)));
      view.rulerEl.setAttribute('aria-valuetext', fmtDeg(straightOf(view)));
      paintRuler(view.rulerEl, straightOf(view));
    }
  };
  return out;
}

// ---- 面板 ----------------------------------------------------------------------

export function correctSection(view) {
  const v2 = view.ed.v2;
  const out = angleReadout(view);
  // 拉直：左边标签，右边数值（和垂直、水平同一排法）
  const rows = [h('div', { class: 'row' }, h('div', { class: 'lab' }, h('span', {}, '拉直'), out), ruler(view))];

  if (v2) {
    rows.push(geomSlider(view, 'vertical', '垂直', null));
    rows.push(geomSlider(view, 'horizontal', '水平', null));
    rows.push(geomSlider(view, 'distortion', '畸变', null));
  } else {
    rows.push(notReady('垂直、水平、畸变、向左旋转、水平翻转、自动'));
  }
  return h('div', { class: 'correct-section' }, ...rows);
}

// 面板底部固定的一行按钮（COPY 3.1：[自动] [向左旋转] [水平翻转] [重置]）。旧版只有重置。
export function correctActions(view) {
  if (!view.ed.v2) return [button('重置', { ico: 'rotate-ccw', onclick: () => resetCorrections(view) })];
  return [
    button('自动', { ico: 'wand-sparkles', title: '自动', onclick: () => autoCorrect(view) }),
    button('向左旋转', { ico: 'rotate-ccw-square', title: '向左旋转（[）', onclick: () => rotate90(view) }),
    button('水平翻转', { ico: 'flip-horizontal-2', title: '水平翻转', pressed: !!view.ed.geometry.flip, onclick: () => flipHorizontal(view) }),
    button('重置', { ico: 'rotate-ccw', onclick: () => resetCorrections(view) }),
  ];
}
