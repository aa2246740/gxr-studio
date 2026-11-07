// 裁切：比例（常用 + 社交媒体）、横竖、框的大小、构图辅助线。裁切只写进 sidecar，原图不动。
// 框的拖动在 stage.js；拉直、透视、旋转翻转在 correct.js。
// 坐标约定（PRD 第 10 节第 10 条）：rect 归一化到「stage=uncropped 返回的那张图」。

import { h, icon } from './dom.js';
import { beginChange, endChange, saveCrop, geometryOf, validRect } from './preview.js';
import { slider } from './controls.js';
import { maxRectFor, zoomRect, pixelSize, centeredMaxRect, landscapePair, fitInto, fitStraightened, cropTilt } from './geom.js';

// 比例分组。id 是后端 crop.py 的 PRESETS 的键；同一个 id 可以出现在两组里（例如 4:3 既是常用，也是小红书的竖图 3:4）。
// orient：默认方向（'landscape' / 'portrait'）；没写就跟着照片的方向。
// 比例的两组按 COPY.md 3.1 写死。（COPY 里没有的比例，如微博、公众号、电脑壁纸，这一版不放，见 COPY_GAPS。）
export const PRESET_GROUPS = [
  {
    title: '常用',
    items: [
      { id: 'original', label: '原始' },
      { id: 'free', label: '自由' },
      { id: '1:1', label: '1:1' },
      { id: '4:5', label: '4:5', orient: 'portrait' },
      { id: '3:2', label: '3:2' },
      { id: '16:9', label: '16:9' },
      { id: 'xpan-65-24', label: 'XPan 65:24' },
    ],
  },
  {
    title: '社交媒体',
    items: [
      { id: '4:5', label: 'Instagram 4:5', orient: 'portrait' },
      { id: '1.91:1', label: 'Instagram 1.91:1', orient: 'landscape' },
      { id: '4:3', label: '小红书 3:4', orient: 'portrait' },
      { id: '16:9', label: '9:16', orient: 'portrait' },
    ],
  },
];

// 能横竖切换的比例（原始、自由、1:1 不分横竖）
const ORIENTABLE = new Set(['3:2', '4:3', '16:9', '4:5', '1.91:1', 'xpan-65-24']);

// 参考线（G 切换，会循环一圈）。COPY.md 3.1 / 术语表：三等分、网格、黄金比例、黄金螺旋、对角线。
export const GUIDES = [
  ['off', '关'],
  ['thirds', '三等分'],
  ['grid', '网格'],
  ['golden', '黄金比例'],
  ['spiral', '黄金螺旋'],
  ['diagonal', '对角线'],
];

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

// 参考线换了：显卡预览的画面不会因为 onChange 重建裁切框，所以要明说重画框里的线（onRectDrag）
export function cycleGuide(view) {
  const ids = GUIDES.map(([id]) => id);
  const i = ids.indexOf(guidePrefs().guide);
  setPrefs({ guide: ids[(i + 1) % ids.length] });
  view.onChange?.();
  view.onRectDrag?.();
}

export function cycleSpiral(view) {
  setPrefs({ spiral: (guidePrefs().spiral + 1) % 4 });
  view.onChange?.();
  view.onRectDrag?.();
}

// ---- 比例 ----------------------------------------------------------------------

// 横向的比例数值（宽 / 高 ≥ 1）。原比例、自由返回 null。
function landscapeRatio(id) {
  if (id === 'xpan-65-24') return 65 / 24;
  const m = /^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$/.exec(id || '');
  if (!m) return null;
  const a = Number(m[1]);
  const b = Number(m[2]);
  return Math.max(a / b, b / a);
}

// 当前选中的是哪一项（同一个 id 可能在两组里，view.presetItem 记住点的是哪一个）
export function presetFor(view) {
  if (view.presetItem && view.presetItem.id === view.crop.preset) return view.presetItem;
  const first = PRESET_GROUPS.flatMap((g) => g.items).find((x) => x.id === view.crop.preset);
  return first || { id: view.crop.preset, label: view.crop.preset };
}

export function orientable(item) {
  return !!item && ORIENTABLE.has(item.id);
}

// 照文件的方向：原照片横的还是竖的（旧版横竖切换用它算 flip）
export function frameLandscape(view) {
  const f = view.crop.frame || { width: 1, height: 1 };
  return f.width >= f.height;
}

// 框现在是横的还是竖的（有画布就看框的形状，没有就看照片的方向）
export function isPortrait(view, canvas) {
  const r = view.crop.rect;
  if (canvas && canvas.w) return r.w * canvas.w < r.h * canvas.h;
  return !frameLandscape(view);
}

// 框的比例锁（宽 / 高，像素）。自由时返回 null；原比例用画布本身的比例。
export function lockRatio(view, canvas) {
  if (view.crop.preset === 'free') return null;
  const item = presetFor(view);
  const L = landscapeRatio(item.id);
  if (L == null) return canvas.w / canvas.h;
  if (!orientable(item)) return L;
  return isPortrait(view, canvas) ? 1 / L : L;
}

// 框的当前比例（宽 / 高，像素），给框的大小用
export function ratioForMax(view, canvas) {
  return lockRatio(view, canvas);
}

// 写 /crop 的请求体。v2 用 orientation；旧版用 flip（和照片本身的方向不同时为 true），并带上拉直角度（旧版拉直只在 /crop 里）。
export function cropBody(view, base, orientation) {
  // 一定要带上当前的拉直角度：后端 PUT /crop 的 angle_deg 默认是 0，不带的话会把刚拉的角度冲回 0
  const angle_deg = geometryOf(view).straighten || 0;
  if (view.ed && view.ed.v2) return { ...base, orientation, angle_deg };
  const wantLandscape = orientation !== 'portrait';
  const flip = wantLandscape !== frameLandscape(view);
  return { ...base, flip, angle_deg };
}

// 全尺寸的未裁切画布（原图像素）。接口给的 frame 已经是几何变换（拉直、透视、畸变、旋转 90°）之后的画布，
// 不能再按 rotate90 转一次（转过 90° 再点比例，框的比例和「W × H」读数会错）。本地转 90° 时 rotate90() 自己把 frame 的宽高对调。
// 不知道原图尺寸时退回预览画布。
export function fullCanvas(view) {
  const f = view.crop && view.crop.frame;
  if (f && f.width && f.height) return { w: f.width, h: f.height };
  return view.canvas || null;
}

// 框收进这张照片能裁的范围，和后端校验、点比例时写下的框用同一个范围（PR #39 复审）：
// 没有透视、畸变时是拉直以后的原画面（geom.fitStraightened，即后端的 crop.box_inside；翻转过的照片倾斜方向反过来，见 geom.cropTilt）；
// 有透视、畸变时只知道有效矩形，用它（保守一些，后端会再按有效区收）。返回 { rect, changed }。
export function fitCrop(view, rect) {
  const g = geometryOf(view) || {};
  const full = fullCanvas(view);
  if (g.vertical || g.horizontal || g.distortion || !full) return fitInto(rect, validRect(view, view.canvas));
  return fitStraightened(rect, full, cropTilt(g));
}

// 先在界面上回到原图（撤销要记得住框），再用 preview.js 的 resetCropRemote 排队保存
export function resetCropLocal(view) {
  if (view.crop) Object.assign(view.crop, { preset: 'original', rect: { x: 0, y: 0, w: 1, h: 1 }, angle_deg: 0, orientation: 'landscape' });
}

// 点比例 / 横竖：框先在本地算好、马上画出来，再后台保存；后端回来的框由 saveCrop 的 onOk 换上。
// 不等后端：机器忙的时候，点一下也要立刻看到变化。横竖指几何变换之后的画布（转过 90° 的横图是竖的），和后端一致。
// 没有透视、畸变时和后端写下的是同一个几何（geom.centeredMaxRect），只差取整；
// 有透视、畸变时有效区只有后端知道，先画有效画面里的最大框，后端回来再换成准框（方向相同，大小可能小一点）。
export function presetRect(view, item, portrait) {
  const g = geometryOf(view) || {};
  const full = fullCanvas(view);
  if (!full || !full.w || !full.h) return null;
  const L = landscapeRatio(item.id);
  const ratio = L == null ? null : orientable(item) && portrait ? 1 / L : L;
  if (g.vertical || g.horizontal || g.distortion) {
    const canvas = view.canvas || full;
    return maxRectFor(validRect(view, canvas), canvas, ratio);
  }
  return centeredMaxRect(full, ratio, g.straighten || 0, landscapePair(item.id));
}

function showNow(view, item, orientation) {
  const rect = presetRect(view, item, orientation === 'portrait');
  // 算不出框（还不知道画布大小）也要把比例记下来，这一步才有可撤销的差别；框等后端回来再换
  view.crop.preset = item.id;
  if (view.ed && view.ed.v2) view.crop.orientation = orientation;
  if (!rect) return;
  view.crop.rect = rect;
  view.onRectDrag?.(); // 画面上的框和尺寸读数
  view.redrawSide?.(); // 面板里的比例名、横竖、缩放
}

// 选一个比例（保存时 rect 为 null，让后端算出居中的最大框）
export async function applyPreset(view, item) {
  beginChange(view);
  view.presetItem = item;
  view.zoom = 100;
  const orientation = orientable(item) && item.orient === 'portrait' ? 'portrait' : 'landscape';
  showNow(view, item, orientation);
  await saveCrop(view, cropBody(view, { preset: item.id, rect: null }, orientation));
  endChange(view);
}

// 横竖切换：同一个比例，换成另一个方向
export async function toggleOrientation(view) {
  const item = presetFor(view);
  if (!orientable(item)) return;
  const orientation = isPortrait(view, view.canvas) ? 'landscape' : 'portrait';
  beginChange(view);
  view.presetItem = item;
  view.zoom = 100;
  showNow(view, item, orientation);
  await saveCrop(view, cropBody(view, { preset: item.id, rect: null }, orientation));
  endChange(view);
}

// 框的大小：以「最大框」为 100%，以框的中心为准缩放，再收进能裁的范围。
// 最大框就是点这个比例时画的那一框（presetRect），收框也和拖动、后端一样（fitCrop）：拉直以后拖到 100% 不会变小（PR #39 复审）。
// 有透视、畸变时仍按有效矩形（那时只知道它）。
export function previewZoom(view, pct) {
  const canvas = view.canvas;
  if (!canvas) return null;
  const g = geometryOf(view) || {};
  if (g.vertical || g.horizontal || g.distortion) {
    const valid = validRect(view, canvas);
    const max = maxRectFor(valid, canvas, ratioForMax(view, canvas));
    return zoomRect(view.crop.rect, max, pct, valid);
  }
  const max = presetRect(view, presetFor(view), isPortrait(view, canvas));
  if (!max) return null;
  const r = view.crop.rect;
  const w = (max.w * pct) / 100;
  const h = (max.h * pct) / 100;
  const cx = r.x + r.w / 2;
  const cy = r.y + r.h / 2;
  return fitCrop(view, { x: cx - w / 2, y: cy - h / 2, w, h }).rect;
}

export async function applyZoom(view, pct) {
  const next = previewZoom(view, pct);
  if (!next) return;
  beginChange(view);
  view.crop.rect = next;
  await saveCrop(view, cropBody(view, { preset: view.crop.preset, rect: rectArray(next) }, orientationNow(view)));
  endChange(view);
}

export function orientationNow(view) {
  return isPortrait(view, view.canvas) ? 'portrait' : 'landscape';
}

export function rectArray(r) {
  return [r.x, r.y, r.w, r.h];
}

// 「裁出 4288 × 2848 像素」这一行：按原图的像素算，不按预览图（预览图比原图小，直接用会少一半）
export function pixelsText(view) {
  const full = fullCanvas(view) || { w: 1, h: 1 };
  const px = pixelSize(view.crop.rect, full);
  return `${px.w} × ${px.h}`;
}

// ---- 辅助线的 SVG（框里面）-------------------------------------------------------

const NS = (() => { const t = document.createElement('template'); t.innerHTML = '<svg></svg>'; return t.content.firstElementChild.namespaceURI; })();
const PHI = (1 + Math.sqrt(5)) / 2;
const GOLD_SMALL = 1 - 1 / PHI;
const GOLD_BIG = 1 / PHI;

export function guidesSvg(rect, canvas) {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('class', 'guides');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('aria-hidden', 'true');
  const g = guidePrefs();
  if (g.guide === 'off') {
    svg.style.display = 'none';
    return svg;
  }
  const ar = Math.max(0.01, (rect.w * canvas.w) / (rect.h * canvas.h));
  svg.setAttribute('viewBox', `0 0 ${ar} 1`);
  const { lines, path } = guideShapes(g.guide, g.spiral, ar);
  for (const [x1, y1, x2, y2] of lines) svg.append(svgNode('line', { x1, y1, x2, y2 }));
  if (path) svg.append(svgNode('path', { d: path }));
  return svg;
}

export function guideFade() {
  return guidePrefs().fade;
}

function svgNode(tag, attrs) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

function guideShapes(guide, spiral, ar) {
  const lines = [];
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

// 黄金螺旋：中心在四个黄金分割交点之一，每转 90° 半径乘 φ
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

// ---- 面板「裁切」这一段 -----------------------------------------------------------

export function cropSection(view) {
  const item = presetFor(view);
  const canvas = view.canvas || fullCanvas(view); // 画面还没出来（刚转过 90° 等）时按接口给的画布，不留骨架
  const portrait = canvas ? isPortrait(view, canvas) : !frameLandscape(view);
  const canOrient = orientable(item);
  const ratioBtn = h('button', { type: 'button', class: 'select', 'aria-haspopup': 'listbox', 'aria-expanded': 'false', 'aria-label': '比例', onclick: (e) => openPresetMenu(view, e.currentTarget) }, h('span', {}, item.label || item.id), icon('chevron-down', 'sm'));
  const orientBtn = h(
    'button',
    {
      type: 'button',
      class: 'btn sm',
      'aria-pressed': String(portrait),
      'aria-disabled': canOrient ? null : 'true',
      title: canOrient ? '横向 / 纵向' : null,
      onclick: () => {
        if (canOrient) toggleOrientation(view);
      },
    },
    portrait ? '纵向' : '横向',
  );

  const zoomRow = slider({
    label: '缩放',
    min: 30,
    max: 100,
    step: 1,
    value: view.zoom || 100,
    fmt: (v) => `${v}%`,
    dflt: 100,
    onBegin: () => beginChange(view),
    onCancel: () => endChange(view),
    onInput: (v) => {
      view.zoom = v;
      const next = previewZoom(view, v);
      if (next) {
        view.crop.rect = next;
        view.onChange?.();
      }
    },
    onCommit: (v) => {
      view.zoom = v;
      applyZoom(view, v);
    },
  });

  // 参考线：和比例一样是下拉框「三等分 ▾」（COPY 3.1）。拖动时参考线完整显示，松手后变淡（stage.js 的 data-fade）
  const g = guidePrefs();
  const guideLabel = (GUIDES.find(([id]) => id === g.guide) || GUIDES[1])[1];
  const guideBtn = h('button', { type: 'button', class: 'select', 'aria-haspopup': 'listbox', 'aria-expanded': 'false', 'aria-label': '参考线', onclick: (e) => openGuideMenu(view, e.currentTarget) }, h('span', {}, guideLabel), icon('chevron-down', 'sm'));

  // 尺寸不在面板里写：画面右下角的 W × H 由 stage.js 画（COPY 3.1）
  return h(
    'div',
    { class: 'crop-section' },
    h('div', { class: 'field-line' }, h('span', { class: 'seg-label' }, '比例'), ratioBtn, orientBtn),
    zoomRow.el,
    h('div', { class: 'field-line' }, h('span', { class: 'seg-label' }, '参考线'), guideBtn),
  );
}

function openGuideMenu(view, anchor) {
  const cur = guidePrefs().guide;
  const groups = [{ title: null, items: GUIDES.map(([id, text]) => ({ id, label: text, on: cur === id })) }];
  openMenu(anchor, groups, (id) => {
    setPrefs({ guide: id });
    view.onChange?.();
    view.onRectDrag?.();
    view.redrawSide?.();
  });
}

function openPresetMenu(view, anchor) {
  const cur = presetFor(view);
  const groups = PRESET_GROUPS.map((grp) => ({
    title: grp.title,
    items: grp.items.map((x) => ({ id: `${x.id}|${x.label}`, label: x.label, on: cur.label === x.label })),
  }));
  openMenu(anchor, groups, (key) => {
    const [id, label] = key.split('|');
    const item = PRESET_GROUPS.flatMap((g) => g.items).find((x) => x.id === id && x.label === label);
    if (item) applyPreset(view, item);
  });
}

let menuEl = null;
let menuAnchor = null;
export function closeMenus() {
  if (menuEl) menuEl.remove();
  if (menuAnchor) {
    menuAnchor.setAttribute('aria-expanded', 'false');
    if (menuAnchor.isConnected && menuEl && menuEl.contains(document.activeElement)) menuAnchor.focus();
  }
  menuEl = null;
  menuAnchor = null;
}

// 弹出菜单：点选项或点别处就关；↑↓ 移动、回车选、Esc 关并回到按钮
export function openMenu(anchor, groups, onPick) {
  closeMenus();
  const r = anchor.getBoundingClientRect();
  const m = h('div', { class: 'menu', role: 'listbox', style: { left: `${Math.min(r.left, window.innerWidth - 250)}px`, top: `${r.bottom + 6}px` } });
  for (const g of groups) {
    if (g.title) m.append(h('div', { class: 'mt', role: 'presentation' }, g.title));
    for (const it of g.items) {
      m.append(h('button', {
        type: 'button', class: 'mi', role: 'option', 'aria-selected': String(!!it.on),
        onclick: () => {
          closeMenus();
          onPick(it.id);
        },
      }, h('span', { class: 'chk' }, it.on ? icon('check', 'sm') : null), h('span', {}, it.label)));
    }
  }
  m.addEventListener('keydown', (e) => {
    const items = [...m.querySelectorAll('.mi')];
    const i = items.indexOf(document.activeElement);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const n = items.length;
      items[(i + (e.key === 'ArrowDown' ? 1 : -1) + n) % n]?.focus();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      closeMenus();
    } else if (e.key === 'Tab') {
      closeMenus();
    }
  });
  document.body.append(m);
  menuEl = m;
  menuAnchor = anchor;
  anchor.setAttribute('aria-expanded', 'true');
  const sel = m.querySelector('.mi[aria-selected="true"]') || m.querySelector('.mi');
  if (sel && typeof sel.focus === 'function') sel.focus();
}

document.addEventListener('pointerdown', (e) => {
  if (menuEl && !menuEl.contains(e.target) && !(menuAnchor && menuAnchor.contains(e.target))) closeMenus();
});
