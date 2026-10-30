// 裁切：比例（常用 + 社交媒体）、横竖、构图辅助线、拉直、位置（拖动）、框的大小（缩放）、还原。裁切只写进 sidecar，原图不动。
// 坐标约定（见 studio/README.md）：rect = [x, y, w, h] 归一化到"按方向转正后的画面"，
// 拉直在这块画布上转；所以框画在不转的画面上，图片转在下面。

import { api } from './api.js';
import { h, toast } from './dom.js';
import { EDIT_TABS, syncRev } from './preview.js';

// 比例分组。id 就是后端 crop.py 的 PRESETS 的键；同一个 id 可以出现在两组里（例如 4:3 既是常用，也是小红书的竖图 3:4）。
// orient：这个按钮默认的方向（'landscape' 横 / 'portrait' 竖）；没写就跟着照片的方向。
// 数值依据见 research/studio_crop_guides/REPORT.md（凭记忆整理，未联网核对）。
export const PRESET_GROUPS = [
  {
    title: '常用',
    items: [
      { id: 'original', label: '原比例' },
      { id: 'free', label: '自由' },
      { id: '1:1', label: '1:1' },
      { id: '3:2', label: '3:2' },
      { id: '4:3', label: '4:3' },
      { id: '16:9', label: '16:9' },
      { id: 'xpan-65-24', label: 'XPan 65:24' },
    ],
  },
  {
    title: '社交媒体与屏幕',
    items: [
      { id: '4:3', label: '小红书 3:4', orient: 'portrait', note: '小红书常用竖图 3:4（横图 4:3 见「常用」）' },
      { id: '4:5', label: 'Instagram 4:5', orient: 'portrait', note: 'Instagram 竖图 4:5' },
      { id: '1:1', label: 'Instagram 1:1', note: 'Instagram 方图 1:1' },
      { id: '1.91:1', label: 'Instagram 横图 1.91:1', orient: 'landscape', note: 'Instagram 横图 / 链接预览 1.91:1' },
      { id: '2.35:1', label: '公众号封面 2.35:1', orient: 'landscape', note: '微信公众号封面 2.35:1（900×383）' },
      { id: '16:9', label: '微博 16:9', orient: 'landscape', note: '微博图片常用横图 16:9' },
      { id: '16:9', label: '抖音 / 视频号 9:16', orient: 'portrait', note: '抖音、视频号、Story 竖屏 9:16（与 16:9 同一比例，转成竖）' },
      { id: '16:10', label: '电脑壁纸 16:10', orient: 'landscape', note: '电脑屏幕常见 16:10' },
    ],
  },
];

// 构图辅助线（G 切换，会循环一圈）
export const GUIDES = [
  ['off', '关'],
  ['thirds', '三分线'],
  ['golden', '黄金比例'],
  ['spiral', '黄金螺旋'],
  ['diagonal', '对角线'],
  ['cross', '中心十字'],
  ['grid', '网格 4×4'],
];

// 能横竖切换的比例（原比例、自由、1:1 不分横竖）
const ORIENTABLE = new Set(['3:2', '4:3', '16:9', '4:5', '1.91:1', '2.35:1', '16:10', 'xpan-65-24']);
const PRESET_LABEL = { original: '原比例', free: '自由', 'xpan-65-24': 'XPan 65:24' };
const MIN_ZOOM = 30;

// 辅助线的设置（只是界面偏好，存在这台电脑的浏览器里，不进照片的编辑记录）
const PREFS_KEY = 'gxr-studio.crop-guides';
const DEFAULT_PREFS = { guide: 'thirds', spiral: 0, fade: true };
let prefs = null;

export function guidePrefs() {
  if (!prefs) {
    let saved = {};
    try {
      saved = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') || {};
    } catch {
      saved = {};
    }
    prefs = {
      guide: GUIDES.some(([id]) => id === saved.guide) ? saved.guide : DEFAULT_PREFS.guide,
      spiral: Number.isInteger(saved.spiral) && saved.spiral >= 0 && saved.spiral < 4 ? saved.spiral : DEFAULT_PREFS.spiral,
      fade: typeof saved.fade === 'boolean' ? saved.fade : DEFAULT_PREFS.fade,
    };
  }
  return prefs;
}

function setPrefs(patch) {
  prefs = { ...guidePrefs(), ...patch };
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch {
    // 存不下就只在这一次打开的页面里生效
  }
}

// 把后端的裁切描述放进 view（view 由 viewer.js 创建）
export function setCrop(view, c) {
  view.crop = c;
  view.rect = { x: c.rect[0], y: c.rect[1], w: c.rect[2], h: c.rect[3] };
  view.angle = c.angle_deg;
  view.preset = c.preset;
  syncRev(view, c.rev); // 和配方/调整共用一个版本号
}

export async function loadCrop(view) {
  setCrop(view, await api(`/api/photos/${view.photo.id}/crop`));
}

// 每次改动都先把旧状态记进历史（最多 100 步），⌘Z 回退
function pushHistory(view) {
  view.history.push({ preset: view.crop.preset, rect: view.crop.rect, angle_deg: view.crop.angle_deg, active: view.crop.active });
  if (view.history.length > 100) view.history.shift();
}

export async function saveCrop(view, body, { remember = true } = {}) {
  if (remember) pushHistory(view);
  try {
    const c = await api(`/api/photos/${view.photo.id}/crop`, {
      method: 'PUT',
      body: { ...body, expected_rev: view.crop.rev },
    });
    setCrop(view, c);
    applyCrop(view);
    refreshPanel(view);
    if (c.warning) toast(c.warning, 'info');
    return c;
  } catch (e) {
    if (remember) view.history.pop();
    toast(e.message, 'error');
    await loadCrop(view).catch(() => {});
    applyCrop(view);
    refreshPanel(view);
    return null;
  }
}

export async function resetCrop(view) {
  pushHistory(view);
  try {
    const c = await api(`/api/photos/${view.photo.id}/crop`, { method: 'DELETE' });
    setCrop(view, c);
    view.zoom = 100;
    applyCrop(view);
    refreshPanel(view);
  } catch (e) {
    view.history.pop();
    toast(e.message, 'error');
  }
}

// 撤销：回到上一次的裁切（没有上一次就还原）
export async function undoCrop(view) {
  const prev = view.history.pop();
  if (!prev) {
    toast('没有可以撤销的了', 'info');
    return;
  }
  if (!prev.active) {
    await resetCrop(view);
    view.history.pop(); // resetCrop 自己又记了一步，撤销不再入栈
    return;
  }
  await saveCrop(view, { preset: prev.preset, rect: prev.rect, angle_deg: prev.angle_deg }, { remember: false });
}

// 横竖：框的宽高关系。frame 是按方向转正后的画面；flip 表示"这次和照片本身的方向反过来"
function frameLandscape(view) {
  const f = view.crop.frame || { width: 1, height: 1 };
  return f.width >= f.height;
}

function shownLandscape(view) {
  const f = view.crop.frame || { width: 1, height: 1 };
  return view.rect.w * f.width >= view.rect.h * f.height;
}

function flipFor(view, wantLandscape) {
  return wantLandscape !== frameLandscape(view);
}

// 让框和图片都对上当前 view.rect / view.angle
export function applyCrop(view) {
  const { img, box, stage } = view;
  if (!img || !box) return;
  const r = view.rect;
  // 配方、调整的预览是整张画面（不带裁切和拉直），这时不画裁切框
  const plain = view.showOriginal || EDIT_TABS.includes(view.tab);
  img.style.transform = plain ? 'none' : `rotate(${view.angle}deg)`;
  box.style.display = plain ? 'none' : '';
  box.style.left = `${r.x * 100}%`;
  box.style.top = `${r.y * 100}%`;
  box.style.width = `${r.w * 100}%`;
  box.style.height = `${r.h * 100}%`;
  if (stage) stage.dataset.active = String(view.crop.active);
  if (view.guideSvg) {
    box.dataset.fade = String(guidePrefs().fade);
    box.dataset.moving = String(!!view.moving);
    drawGuides(view);
  }
}

// 按舞台大小把画面等比放进可用区域
export function fitStage(view) {
  const { stage, stageWrap, crop } = view;
  if (!stage || !stageWrap || !crop || !crop.frame) return;
  const aw = stageWrap.clientWidth - 40;
  const ah = stageWrap.clientHeight - 90;
  const { width: fw, height: fh } = crop.frame;
  const scale = Math.max(0.05, Math.min(aw / fw, ah / fh));
  stage.style.width = `${Math.floor(fw * scale)}px`;
  stage.style.height = `${Math.floor(fh * scale)}px`;
}

function clamp(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v));
}

// 拖动框的位置（只改界面，松手时才保存）
export function attachDrag(view) {
  const { box, stage } = view;
  let drag = null;
  box.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || view.showOriginal) return;
    box.setPointerCapture(e.pointerId);
    const sr = stage.getBoundingClientRect();
    drag = { sx: e.clientX, sy: e.clientY, start: { ...view.rect }, W: sr.width, H: sr.height, moved: false };
    view.moving = true;
    applyCrop(view);
    e.preventDefault();
  });
  box.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const dx = (e.clientX - drag.sx) / drag.W;
    const dy = (e.clientY - drag.sy) / drag.H;
    if (Math.abs(dx) + Math.abs(dy) > 0.001) drag.moved = true;
    const s = drag.start;
    view.rect = {
      x: clamp(s.x + dx, 0, 1 - s.w),
      y: clamp(s.y + dy, 0, 1 - s.h),
      w: s.w,
      h: s.h,
    };
    applyCrop(view);
  });
  const end = () => {
    if (!drag) return;
    const moved = drag.moved;
    drag = null;
    view.moving = false;
    applyCrop(view);
    if (moved) saveRect(view);
  };
  box.addEventListener('pointerup', end);
  box.addEventListener('pointercancel', end);
}

function saveRect(view) {
  const r = view.rect;
  return saveCrop(view, { preset: view.crop.preset, rect: [r.x, r.y, r.w, r.h], angle_deg: view.angle });
}

// 比例、拉直、横竖变了：让后端算出居中的最大框，并记下它当作缩放的基准
async function recomputeDefault(view, preset, angle, flip = flipFor(view, shownLandscape(view))) {
  const c = await saveCrop(view, { preset, rect: null, angle_deg: angle, flip });
  if (c) {
    view.maxRect = { w: c.rect[2], h: c.rect[3] };
    view.zoom = 100;
    refreshPanel(view);
  }
}

// ---- 构图辅助线 ---------------------------------------------------------------

const PHI = (1 + Math.sqrt(5)) / 2;
const GOLD_SMALL = 1 - 1 / PHI; // ≈ 0.382
const GOLD_BIG = 1 / PHI; // ≈ 0.618

// 辅助线画在框里面的 SVG 上。viewBox 的宽 = 框的真实宽高比、高 = 1，所以线条和螺旋都不会变形
export function attachGuides(view) {
  // 用模板生成 SVG 元素，它自带 SVG 命名空间
  const tpl = document.createElement('template');
  tpl.innerHTML = '<svg class="guides" preserveAspectRatio="none" aria-hidden="true"></svg>';
  const svg = tpl.content.firstElementChild;
  view.box.append(svg);
  view.guideSvg = svg;
  view.guideKey = '';
}

function drawGuides(view) {
  const svg = view.guideSvg;
  const { guide, spiral } = guidePrefs();
  svg.style.display = guide === 'off' ? 'none' : '';
  if (guide === 'off') return;
  const f = view.crop.frame || { width: 1, height: 1 };
  const ar = Math.max(0.01, (view.rect.w * f.width) / (view.rect.h * f.height));
  const key = `${guide}|${spiral}|${ar.toFixed(4)}`;
  if (key === view.guideKey) return;
  view.guideKey = key;
  svg.setAttribute('viewBox', `0 0 ${ar} 1`);
  const svgNode = (tag, attrs) => {
    const el = document.createElementNS(svg.namespaceURI, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
    return el;
  };
  const { lines, path } = guideShapes(guide, spiral, ar);
  const nodes = lines.map(([x1, y1, x2, y2]) => svgNode('line', { x1, y1, x2, y2 }));
  if (path) nodes.push(svgNode('path', { d: path }));
  svg.replaceChildren(...nodes);
}

// 每种辅助线的线段（[x1, y1, x2, y2]）或螺旋的路径。x 的单位是 ar，y 的单位是 1
function guideShapes(guide, spiral, ar) {
  const lines = [];
  // 竖线、横线都按"占宽/占高的比例"给
  const at = (fracs) => {
    for (const f of fracs) {
      lines.push([f * ar, 0, f * ar, 1]);
      lines.push([0, f, ar, f]);
    }
  };
  switch (guide) {
    case 'thirds':
      at([1 / 3, 2 / 3]);
      break;
    case 'golden':
      at([GOLD_SMALL, GOLD_BIG]);
      break;
    case 'diagonal':
      lines.push([0, 0, ar, 1], [ar, 0, 0, 1]);
      break;
    case 'cross':
      at([0.5]);
      break;
    case 'grid':
      at([0.25, 0.5, 0.75]);
      break;
    case 'spiral':
      return { lines, path: spiralPath(spiral, ar) };
    default:
      break;
  }
  return { lines, path: '' };
}

// 黄金螺旋：中心在四个黄金分割交点之一，每转 90° 半径乘 φ（≈1.618）。
// spiral 0..3 = 原样 / 左右翻 / 上下翻 / 两个都翻，对应四个中心位置。
function spiralPath(k, ar) {
  const sx = k & 1 ? -1 : 1;
  const sy = k & 2 ? -1 : 1;
  const px = (sx > 0 ? GOLD_BIG : GOLD_SMALL) * ar;
  const py = sy > 0 ? GOLD_SMALL : GOLD_BIG;
  let d = '';
  for (let u = -9; u <= 0.6 + 1e-9; u += 0.02) {
    const r = 0.9 * PHI ** u;
    const th = (u * Math.PI) / 2;
    const x = px + sx * r * Math.cos(th);
    const y = py + sy * r * Math.sin(th);
    d += `${d ? 'L' : 'M'}${x.toFixed(4)} ${y.toFixed(4)}`;
  }
  return d;
}

function updateGuides(view, patch) {
  setPrefs(patch);
  applyCrop(view);
  refreshPanel(view);
}

// G 键：辅助线循环切换（包括"关"）
export function cycleGuide(view) {
  const ids = GUIDES.map(([id]) => id);
  const i = ids.indexOf(guidePrefs().guide);
  updateGuides(view, { guide: ids[(i + 1) % ids.length] });
}

// O 键：螺旋方向循环（4 个方向）
export function cycleSpiral(view) {
  updateGuides(view, { spiral: (guidePrefs().spiral + 1) % 4 });
}

// ---- 面板 ---------------------------------------------------------------------

// 面板：比例（分组）、横竖、构图辅助线、框的大小、拉直、还原、尺寸提示
export function cropPanel(view) {
  const wrap = h('div', { class: 'crop-panel' });
  view.panelEl = wrap;
  refreshPanel(view);
  return wrap;
}

function presetButton(view, item, cur) {
  const want = item.orient ? item.orient : frameLandscape(view) ? 'landscape' : 'portrait';
  const pressed = view.crop.active && view.preset === item.id && (!ORIENTABLE.has(item.id) || want === cur);
  return h(
    'button',
    {
      class: 'btn',
      type: 'button',
      title: item.note || item.label,
      'aria-pressed': String(pressed),
      onclick: () => recomputeDefault(view, item.id, view.angle, flipFor(view, want === 'landscape')),
    },
    item.label,
  );
}

export function refreshPanel(view) {
  const wrap = view.panelEl;
  if (!wrap) return;
  const c = view.crop;
  const frame = c.frame || { width: 1, height: 1 };
  const pixels = c.pixels ? `${c.pixels.width} × ${c.pixels.height} 像素` : '';
  const cur = shownLandscape(view) ? 'landscape' : 'portrait';
  const guides = guidePrefs();
  const canOrient = ORIENTABLE.has(view.preset) && c.active;
  const ratioNote = `${PRESET_LABEL[view.preset] || view.preset}${canOrient ? (cur === 'landscape' ? '（横）' : '（竖）') : ''}`;

  const zoom = view.zoom || 100;
  const zoomInput = h('input', {
    type: 'range',
    min: MIN_ZOOM,
    max: 100,
    step: 1,
    value: zoom,
    'aria-label': '框的大小',
    oninput: (e) => {
      view.zoom = Number(e.target.value);
      zoomOut.textContent = `${view.zoom}%`;
      view.moving = true;
      applyCrop(view);
    },
    onchange: (e) => {
      const s = Number(e.target.value) / 100;
      const m = view.maxRect || { w: view.rect.w, h: view.rect.h };
      const cx = view.rect.x + view.rect.w / 2;
      const cy = view.rect.y + view.rect.h / 2;
      const w = m.w * s;
      const hh = m.h * s;
      view.moving = false;
      view.rect = { x: clamp(cx - w / 2, 0, 1 - w), y: clamp(cy - hh / 2, 0, 1 - hh), w, h: hh };
      applyCrop(view);
      saveRect(view);
    },
  });
  const zoomOut = h('output', {}, `${zoom}%`);

  const angleOut = h('output', {}, fmtAngle(view.angle));
  const angleInput = h('input', {
    type: 'range',
    min: -10,
    max: 10,
    step: 0.1,
    value: view.angle,
    'aria-label': '拉直角度',
    title: '双击归零',
    ondblclick: () => {
      angleInput.value = 0;
      view.angle = 0;
      angleOut.textContent = fmtAngle(0);
      recomputeDefault(view, view.preset, 0);
    },
    oninput: (e) => {
      view.angle = Number(e.target.value);
      angleOut.textContent = fmtAngle(view.angle);
      view.moving = true;
      applyCrop(view);
    },
    onchange: () => {
      view.moving = false;
      recomputeDefault(view, view.preset, view.angle);
    },
  });

  const groups = PRESET_GROUPS.map((g) =>
    h('div', { class: 'field' }, h('span', { class: 'dim small' }, g.title), h('div', { class: 'seg' }, g.items.map((item) => presetButton(view, item, cur)))),
  );

  const orientRow = canOrient
    ? h(
        'div',
        { class: 'field' },
        h('span', { class: 'dim small' }, '方向'),
        h(
          'div',
          { class: 'seg' },
          [
            ['landscape', '横'],
            ['portrait', '竖'],
          ].map(([want, text]) =>
            h(
              'button',
              {
                class: 'btn',
                type: 'button',
                'aria-pressed': String(cur === want),
                onclick: () => recomputeDefault(view, view.preset, view.angle, flipFor(view, want === 'landscape')),
              },
              text,
            ),
          ),
        ),
      )
    : null;

  const guideRow = h(
    'div',
    { class: 'field' },
    h('span', { class: 'dim small' }, '构图辅助线（G 切换）'),
    h(
      'div',
      { class: 'seg' },
      GUIDES.map(([id, text]) =>
        h('button', { class: 'btn', type: 'button', 'aria-pressed': String(guides.guide === id), onclick: () => updateGuides(view, { guide: id }) }, text),
      ),
    ),
  );

  const spiralRow =
    guides.guide === 'spiral'
      ? h(
          'div',
          { class: 'field-row' },
          h('button', { class: 'btn', type: 'button', title: 'O 切换', onclick: () => updateGuides(view, { spiral: (guides.spiral + 1) % 4 }) }, `螺旋方向 ${guides.spiral + 1}/4（O 切换）`),
        )
      : null;

  const fadeRow = h(
    'label',
    { class: 'small dim' },
    h('input', { type: 'checkbox', checked: guides.fade, onchange: (e) => updateGuides(view, { fade: e.target.checked }) }),
    ' 拖动时看清，松手后变淡',
  );

  const pixelsLine = c.active ? `当前：${ratioNote}${pixels ? `，裁出 ${pixels}` : ''}` : '当前：没有裁切（整张原图）';

  // 没有的行（null）不能直接交给 replaceChildren，否则会变成文字 "null"
  const nodes = [
    h('h2', {}, '裁切'),
    h('p', { class: 'dim small' }, '原图没有被改动。裁切只是记下位置，随时可以还原。'),
    ...groups,
    orientRow,
    h('p', { class: 'small' }, pixelsLine),
    h('div', {}, presetNote(view.preset)),
    guideRow,
    spiralRow,
    fadeRow,
    h(
      'div',
      { class: 'field' },
      h('label', { class: 'small dim' }, '框的大小（拖动框可以挪位置）'),
      h('div', { class: 'field-row' }, zoomInput, zoomOut),
    ),
    h(
      'div',
      { class: 'field' },
      h('label', { class: 'small dim', title: '双击滑块回到 0°' }, '拉直（把歪的地平线摆正）'),
      h('div', { class: 'field-row' }, angleInput, angleOut),
    ),
    h(
      'div',
      { class: 'modal-foot', style: { justifyContent: 'flex-start' } },
      h('button', { class: 'btn', type: 'button', onclick: () => resetCrop(view) }, '还原裁切'),
      h('button', { class: 'btn primary', type: 'button', onclick: () => toast(c.active ? `已记下：${pixels}` : '没有裁切，原图不变', 'ok') }, '完成'),
    ),
  ].filter(Boolean);
  wrap.replaceChildren(...nodes);
}

function presetNote(preset) {
  if (preset === 'xpan-65-24') {
    return h('p', { class: 'small dim' }, 'XPan 65:24 是全景条，宽 : 高 ≈ 2.7 : 1，适合横向景物。默认居中，可以拖动框挪位置。');
  }
  return null;
}

function fmtAngle(a) {
  const n = Number(a) || 0;
  return `${n > 0 ? '+' : ''}${n.toFixed(1)}°`;
}
