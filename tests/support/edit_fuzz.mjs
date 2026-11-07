// 裁剪 / 几何的随机交错测试（种子固定，可重现）。
// 真正的 viewer.js、preview.js、correct.js、crop.js、saves.js、crop_save.js、undo_entries.js 在 node 里跑，
// 接口换成一个照真服务器规则写的替身：
//   - 写几何：存下几何；需要收框（拉直、透视、畸变）时把已存的框按预设比例收进新的有效画面，画布尺寸跟着变（旋转交换宽高）；
//     只旋转、翻转时服务器不动框（和真服务器一样，框要界面自己转）。
//   - 写框：先校验（范围、最小边、预设比例 1%、拉直后的有效区），不合格 422（ratio_mismatch / crop_outside_image / bad_rect / too_small）；
//     rect 为 null 时服务器自己算居中的最大框；写框还会把 angle_deg 写成几何的拉直角度（真服务器的 write_crop 就是这样）。
//   - 随机延迟；随机挂住（等放行）、500、rev_conflict（409，不写入）。
// 每条序列 6–10 步，从这些里抽：旋转、翻转、垂直 / 水平 / 畸变滑块、拉直、自动、点比例、挪框、撤销、重做、重试、等写完（settle）。
// 同一串步骤再在「没有故障、每步等写完」的理想情形下跑一遍，两边写完以后必须一致。
// 用法：node edit_fuzz.mjs <模块目录> <起始种子> <条数>。输出一行 JSON，断言在 Python 那边。
import { pathToFileURL } from 'node:url';
import { installDom, tick } from './mini_dom.mjs';

const folder = process.argv[2];
const seedStart = Number(process.argv[3] || 1);
const count = Number(process.argv[4] || 200);
const file = (name) => pathToFileURL(`${folder}/${name}`).href;

installDom();

// ---- 随机数 -----------------------------------------------------------------
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const pick = (r, list) => list[Math.floor(r() * list.length)];

// ---- 服务器规则（照 crop.py / finish.geometry 的做法，数值是简化的但规则一样）-------------------
const BASE = { w: 3000, h: 2000 };
const DEFAULT_GEOMETRY = { straighten: 0, vertical: 0, horizontal: 0, distortion: 0, rotate90: 0, flip: false };
const PRESETS = { original: 'frame', free: null, '1:1': 1, '4:5': 1.25, '3:2': 1.5, '4:3': 4 / 3, '16:9': 16 / 9 };
const expands = (g) => !!(g.vertical || g.horizontal || g.distortion);
const needsFit = (g) => Math.abs(g.straighten) > 1e-9 || expands(g);

function frameOf(g) {
  let w = BASE.w;
  let h = BASE.h;
  if (expands(g)) {
    w = Math.round(BASE.w * (1 + (Math.abs(g.vertical) + Math.abs(g.horizontal)) / 400));
    h = Math.round(BASE.h * (1 + Math.abs(g.distortion) / 400 + Math.abs(g.vertical) / 800));
  }
  return g.rotate90 % 2 ? { width: h, height: w } : { width: w, height: h };
}
// 有效画面：拉直用 crop.py 的 box_inside（四个角转回去都在原画面里）；透视、畸变再给一块居中的、边长是画布 k 倍的区域
const kPersp = (g) => 1 / (1 + (Math.abs(g.vertical) + Math.abs(g.horizontal) + Math.abs(g.distortion)) / 300);
const r6 = (v) => Math.round(v * 1e6) / 1e6;

function boxInside(x0, y0, w, h, width, height, angle, tol) {
  const th = (angle * Math.PI) / 180;
  const c = Math.cos(th);
  const s = Math.sin(th);
  const cx = width / 2;
  const cy = height / 2;
  for (const [px, py] of [[x0, y0], [x0 + w, y0], [x0, y0 + h], [x0 + w, y0 + h]]) {
    const dx = px - cx;
    const dy = py - cy;
    const sx = dx * c + dy * s;
    const sy = -dx * s + dy * c;
    if (Math.abs(sx) > width / 2 + tol || Math.abs(sy) > height / 2 + tol) return false;
  }
  return true;
}

function ratioOf(preset, frame, orientation) {
  const base = PRESETS[preset];
  if (base === null) return null;
  if (base === 'frame') return frame.width / frame.height;
  return orientation === 'portrait' ? 1 / base : base;
}

// 把框收进有效画面（照 geometry.fit_preset_in_valid / clamp_norm_rect 的规则）：
//   - 定比例的预设：取目标框里最大的、比例对的框（横竖跟着目标框），再缩进有效画面，中心尽量不动；
//   - 自由、原始：已经在有效画面里就原样返回，不然保持宽高比缩进去。
function fit(rect, preset, frame, g) {
  const W = frame.width;
  const H = frame.height;
  const kP = kPersp(g);
  const angle = g.flip ? -g.straighten : g.straighten;
  const [x, y, w, h] = rect;
  let pw = w * W;
  let ph = h * H;
  const cx = (x + w / 2) * W;
  const cy = (y + h / 2) * H;
  const okAt = (bw, bh) => {
    const bx = Math.min(Math.max(cx - bw / 2, ((1 - kP) / 2) * W), ((1 + kP) / 2) * W - bw);
    const by = Math.min(Math.max(cy - bh / 2, ((1 - kP) / 2) * H), ((1 + kP) / 2) * H - bh);
    return bw <= kP * W + 1e-6 && bh <= kP * H + 1e-6 && boxInside(bx, by, bw, bh, W, H, angle, 1e-6);
  };
  const base = PRESETS[preset];
  const fixed = typeof base === 'number';
  if (fixed) {
    const landscape = pw >= ph;
    const r = landscape === base >= 1 ? base : 1 / base;
    const bw = Math.min(pw, ph * r);
    pw = bw;
    ph = bw / r;
  } else if (okAt(pw, ph) && x * W >= ((1 - kP) / 2) * W - 1e-6 && y * H >= ((1 - kP) / 2) * H - 1e-6 && (x + w) * W <= ((1 + kP) / 2) * W + 1e-6 && (y + h) * H <= ((1 + kP) / 2) * H + 1e-6 && boxInside(x * W, y * H, pw, ph, W, H, angle, 1e-6)) {
    return rect.map(r6);
  }
  let lo = 0.02;
  let hi = 1;
  if (okAt(pw, ph)) lo = 1;
  else for (let i = 0; i < 24; i += 1) {
    const mid = (lo + hi) / 2;
    if (okAt(pw * mid, ph * mid)) lo = mid;
    else hi = mid;
  }
  pw *= lo;
  ph *= lo;
  const px = Math.min(Math.max(cx - pw / 2, ((1 - kP) / 2) * W), ((1 + kP) / 2) * W - pw);
  const py = Math.min(Math.max(cy - ph / 2, ((1 - kP) / 2) * H), ((1 + kP) / 2) * H - ph);
  return [r6(px / W), r6(py / H), r6(pw / W), r6(ph / H)];
}

function defaultBox(preset, orientation, frame, g) {
  const r = ratioOf(preset, frame, orientation) ?? frame.width / frame.height;
  const angle = g.flip ? -g.straighten : g.straighten;
  let pw = frame.width;
  let ph = pw / r;
  if (ph > frame.height) {
    ph = frame.height;
    pw = ph * r;
  }
  let lo = 0.02;
  let hi = 1;
  const okAt = (bw, bh) => boxInside((frame.width - bw) / 2, (frame.height - bh) / 2, bw, bh, frame.width, frame.height, angle, 1e-6);
  if (okAt(pw, ph)) lo = 1;
  else for (let i = 0; i < 24; i += 1) {
    const mid = (lo + hi) / 2;
    if (okAt(pw * mid, ph * mid)) lo = mid;
    else hi = mid;
  }
  pw = Math.floor(pw * lo);
  ph = Math.floor(pw / r + 0.5);
  return [(frame.width - pw) / 2 / frame.width, (frame.height - ph) / 2 / frame.height, pw / frame.width, ph / frame.height];
}

class ServerError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
    this.data = { message };
  }
}

function validate(preset, rect, frame, g) {
  if (!(preset in PRESETS)) throw new ServerError(422, 'unknown_preset', '无法裁剪。请重新选择比例。');
  const [x, y, w, h] = rect;
  const eps = 1e-9;
  if (!rect.every(Number.isFinite) || w <= 0 || h <= 0 || x < -eps || y < -eps || x + w > 1 + eps || y + h > 1 + eps) throw new ServerError(422, 'bad_rect', '裁剪框超出画面范围');
  const wp = Math.round(w * frame.width);
  const hp = Math.round(h * frame.height);
  if (wp < 16 || hp < 16) throw new ServerError(422, 'too_small', '裁剪框超出画面范围');
  const r = PRESETS[preset] === null ? null : ratioOf(preset, frame, 'landscape');
  if (r !== null) {
    const got = wp / hp;
    if (Math.min(Math.abs(got - r) / r, Math.abs(got - 1 / r) / (1 / r)) > 0.01) throw new ServerError(422, 'ratio_mismatch', '裁剪框超出画面范围');
  }
  const angle = g.flip ? -g.straighten : g.straighten;
  if (!boxInside(Math.round(x * frame.width), Math.round(y * frame.height), wp, hp, frame.width, frame.height, angle, 1)) throw new ServerError(422, 'crop_outside_image', '裁剪框超出画面范围');
}

// ---- 照片（服务器上的状态）-------------------------------------------------------
const photos = new Map(); // id → { rev, geometry, crop:{preset, rect, orientation}, puts:[] }
const PHOTO_META = (id) => ({ id, name: `${id}.DNG`, date: '2026-10-01', captured_at: '2026-10-01T10:00:00', has_raw: true, has_jpg: true, width: BASE.w, height: BASE.h, lens: '50mm' });

function newPhoto(id) {
  photos.set(id, { rev: 1, geometry: { ...DEFAULT_GEOMETRY }, crop: { preset: 'original', rect: [0, 0, 1, 1], orientation: 'landscape' }, writes: 0, edits: 0, nullWrites: 0 });
}

function cropPayload(p) {
  const frame = frameOf(p.geometry);
  const bw = Math.round(p.crop.rect[2] * frame.width);
  const bh = Math.round(p.crop.rect[3] * frame.height);
  return { preset: p.crop.preset, rect: [...p.crop.rect], angle_deg: p.geometry.straighten, active: true, frame: { ...frame }, orientation: bw >= bh ? 'landscape' : 'portrait', rev: p.rev };
}
const editPayload = (p) => ({ rev: p.rev, recipe: null, lut: null, adjust: {}, curve: null, geometry: { ...p.geometry }, frame: null, stamp: null, memory: {}, render: { available: true } });

function applyEdit(p, body) {
  if (body.geometry) {
    const g = { ...DEFAULT_GEOMETRY, ...body.geometry };
    g.straighten = Math.max(-45, Math.min(45, Number(g.straighten) || 0));
    p.geometry = g;
    if (needsFit(g)) p.crop.rect = fit(p.crop.rect, p.crop.preset, frameOf(g), g);
  }
}

function applyCrop(p, body) {
  const g = { ...p.geometry, straighten: body.angle_deg ?? p.geometry.straighten };
  const frame = frameOf(g);
  const orientation = body.orientation || 'landscape';
  let rect;
  if (body.rect === null || body.rect === undefined) {
    p.nullWrites += 1;
    rect = defaultBox(body.preset, orientation, frame, g);
  }
  else if (body.fit) {
    // 目标框：不合格也不拒绝，整理成有限、正尺寸、在画面里，下面收成离它最近的合格框
    if (!(body.preset in PRESETS)) throw new ServerError(422, 'unknown_preset', '无法裁剪。请重新选择比例。');
    let [x, y, w, h] = body.rect.map(Number);
    if (![x, y, w, h].every(Number.isFinite) || w <= 0 || h <= 0) throw new ServerError(422, 'bad_rect', '裁剪框超出画面范围');
    w = Math.min(w, 1);
    h = Math.min(h, 1);
    x = Math.min(Math.max(x, 0), 1 - w);
    y = Math.min(Math.max(y, 0), 1 - h);
    rect = [x, y, w, h];
  } else {
    rect = body.rect.map(Number);
    validate(body.preset, rect, frame, g);
  }
  if (needsFit(g) || (body.fit && body.rect)) rect = fit(rect, body.preset, frame, g);
  p.geometry = g;
  p.crop = { preset: body.preset, rect: rect.map(r6), orientation };
}

// ---- 故障与延迟 ---------------------------------------------------------------
let faultRng = rng(1);
let faultsOn = false;
let holdList = []; // 挂住的 PUT：{ resolve }
let nth = 0;

async function maybeFault(method) {
  await tick(faultsOn ? Math.floor(faultRng() * 3) : 0);
  if (!faultsOn || method !== 'PUT') return;
  const roll = faultRng();
  if (roll < 0.1) {
    faultCount += 1; // 挂住也算：界面在回应回来之前接着操作，用的是还没跟上服务器收框的框
    T('  挂住 PUT');
    await new Promise((resolve) => holdList.push({ resolve }));
  } else if (roll < 0.18) {
    faultCount += 1;
    T('  500 PUT');
    throw new ServerError(500, 'disk_full', '磁盘已满，无法写入编辑记录。');
  } else if (roll < 0.24) {
    faultCount += 1;
    T('  409 PUT');
    throw new ServerError(409, 'rev_conflict', '这张照片已在别处修改。请刷新后重试。');
  }
}
const releaseHolds = () => {
  const list = holdList;
  holdList = [];
  for (const h of list) h.resolve();
};

let trace = [];
let faultCount = 0;
const T = (...a) => trace.push(a.map((x) => (typeof x === 'string' ? x : JSON.stringify(x))).join(' '));
let autoAnswer = { straighten: 0, vertical: 0, confident: false, message: '未检测到明显的水平线' };

globalThis.apiStub = async (url, opts = {}) => {
  const method = opts.method || 'GET';
  let m;
  if (method === 'GET' && (m = /^\/api\/photos\/(\w+)\/(edit|crop)$/.exec(url))) {
    const p = photos.get(m[1]);
    await maybeFault('GET');
    T('  GET', m[2], m[2] === 'crop' ? cropPayload(p).rect : p.geometry);
    return m[2] === 'edit' ? editPayload(p) : cropPayload(p);
  }
  if (method === 'GET' && (m = /^\/api\/photos\/(\w+)$/.exec(url))) return PHOTO_META(m[1]);
  if (method === 'GET' && url.startsWith('/api/photos?')) return [...photos.keys()].map(PHOTO_META);
  if (method === 'GET' && /\/capabilities$/.test(url)) return { raw: true, recipe: 'full', reason: '' };
  if (method === 'GET' && /\/meta$/.test(url)) return {};
  if (method === 'GET' && url === '/api/recipes') return { units: [] };
  if (method === 'GET' && url === '/api/frames') return { styles: [] };
  if (method === 'GET' && url === '/api/defaults/finish') return { frame: { style: 'none' }, stamp: { enabled: false } };
  if (method === 'GET' && url === '/api/camdata/status') return { ready: false, camera_data: { state: 'missing' }, calibration: { state: 'missing' } };
  if (method === 'POST' && /\/geometry\/auto$/.test(url)) {
    await maybeFault('GET');
    return { ...autoAnswer };
  }
  if (method === 'PUT' && (m = /^\/api\/photos\/(\w+)\/(edit|crop)$/.exec(url))) {
    const p = photos.get(m[1]);
    nth += 1;
    await maybeFault('PUT');
    const body = opts.body;
    if (body.expected_rev !== p.rev) throw new ServerError(409, 'rev_conflict', '这张照片已在别处修改。请刷新后重试。');
    const before = JSON.stringify([p.geometry, p.crop]);
    if (m[2] === 'edit') applyEdit(p, body);
    else applyCrop(p, body);
    p.rev += 1;
    p.writes += 1;
    T('  PUT', m[2], body.geometry || { preset: body.preset, rect: body.rect, angle: body.angle_deg, o: body.orientation }, '→', { g: p.geometry, rect: p.crop.rect });
    p.log = [...(p.log || []), `${m[2]}${JSON.stringify(body.geometry || body.rect)}${before === JSON.stringify([p.geometry, p.crop]) ? '=' : ''}`];
    return m[2] === 'edit' ? editPayload(p) : cropPayload(p);
  }
  throw new Error(`没有替身：${method} ${url}`);
};
globalThis.apiBlobStub = () => Promise.resolve({ blob: {}, headers: { get: () => null } });

const { state } = await import(file('state.js'));
const viewer = await import(file('viewer.js'));
const preview = await import(file('preview.js'));
const saves = await import(file('saves.js'));
const cropMod = await import(file('crop.js'));
const correct = await import(file('correct.js'));
const geom = await import(file('geom.js'));
const { createRouter } = await import(file('router.js'));
state.settings = { gpu_preview: false, export_dir: '/tmp/out' };
state.prefs = { export_hdr: false };
state.camdata = { ready: true, state: 'ready', dataVersion: 'v1' };

const root = document.getElementById('view');
const titlebar = Object.assign(document.createElement('header'), { id: 'titlebar' });
titlebar.append(Object.assign(document.createElement('div'), { id: 'tb-edit' }));
document.body.append(titlebar);
const router = createRouter({
  pages: {
    photos: { enter: async () => { root.replaceChildren(document.createElement('div')); return null; } },
    viewer: {
      enter: async (r, isCurrent) => { await viewer.renderViewer(root, r.id, isCurrent); return isCurrent() ? viewer.viewerKeys() : null; },
      leave: () => viewer.leaveViewer(),
    },
    recipes: { enter: async () => null },
    camera: { enter: async () => null },
  },
});
async function go(hash) {
  location.hash = hash;
  return router.route();
}

const toastText = () => document.getElementById('toast-root').children.map((t) => t.textContent);
const near = (a, b, eps) => a.every((v, i) => Math.abs(v - b[i]) <= eps);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ---- 一条序列 --------------------------------------------------------------
const ITEMS = [
  { id: 'original', label: '原始' },
  { id: 'free', label: '自由' },
  { id: '1:1', label: '1:1' },
  { id: '3:2', label: '3:2' },
  { id: '16:9', label: '16:9' },
  { id: '4:3', label: '小红书 3:4', orient: 'portrait' },
  { id: '4:5', label: 'Instagram 4:5', orient: 'portrait' },
];

function makeSteps(seed) {
  const r = rng(seed * 7919 + 13);
  const n = 6 + Math.floor(r() * 5);
  const steps = [];
  // 开头先摆一个有位置的框，后面的步骤才有「用户摆好的框」可以丢
  steps.push(['ratio', pick(r, ITEMS.filter((i) => i.id !== 'original'))]);
  steps.push(['nudge', (r() - 0.5) * 0.1, (r() - 0.5) * 0.1]);
  const kinds = ['rotate', 'rotate', 'flip', 'slider', 'slider', 'straighten', 'auto', 'ratio', 'nudge', 'undo', 'undo', 'redo', 'retry', 'settle'];
  while (steps.length < n) {
    const kind = pick(r, kinds);
    if (kind === 'slider') steps.push(['slider', pick(r, ['vertical', 'horizontal', 'distortion']), pick(r, [-60, -30, 0, 20, 40, 80])]);
    else if (kind === 'straighten') steps.push(['straighten', pick(r, [-2, -0.5, 0.5, 2, 4])]);
    else if (kind === 'auto') steps.push(['auto', pick(r, [0, 1.5, 4]), pick(r, [0, 8, 40])]);
    else if (kind === 'ratio') steps.push(['ratio', pick(r, ITEMS)]);
    else if (kind === 'nudge') steps.push(['nudge', (r() - 0.5) * 0.2, (r() - 0.5) * 0.2]);
    else steps.push([kind]);
  }
  return steps;
}

const screenState = (v) => ({
  geometry: { ...v.ed.geometry },
  rect: [v.crop.rect.x, v.crop.rect.y, v.crop.rect.w, v.crop.rect.h],
  preset: v.crop.preset,
  frame: [v.crop.frame.width, v.crop.frame.height],
});
const serverState = (id) => {
  const p = photos.get(id);
  const f = frameOf(p.geometry);
  return { geometry: { ...p.geometry }, rect: [...p.crop.rect], preset: p.crop.preset, frame: [f.width, f.height] };
};
const stateText = (s) => `${JSON.stringify(s.geometry)} ${s.preset} [${s.rect.map((x) => +x.toFixed(4))}] ${s.frame}`;

async function runSequence(seed, steps, { ideal }) {
  const id = `${ideal ? 'b' : 'a'}${seed.toString(16)}`; // 路由只认十六进制的 id
  newPhoto(id);
  faultsOn = false;
  faultRng = rng(seed * 104729 + 7);
  holdList = [];
  await go(`#/photo/${id}`);
  await tick(6);
  const v = viewer.currentView();
  const section = correct.correctSection(v);
  document.body.append(section);
  const input = (label) => {
    let hit = null;
    const walk = (n) => { if (!hit && n.tagName === 'INPUT' && n.getAttribute && n.getAttribute('aria-label') === label) hit = n; (n.children || []).forEach(walk); };
    walk(section);
    return hit;
  };
  const labels = { vertical: '垂直', horizontal: '水平', distortion: '畸变' };
  const pending = [];
  const seen = [];
  const problems = [];
  const updateCanvas = () => {
    const f = frameOf(v.ed.geometry);
    v.canvas = { w: f.width, h: f.height };
  };
  const rec = saves.saveRecord(id);
  const settle = async () => {
    for (let i = 0; i < 30; i += 1) {
      releaseHolds();
      await tick(4);
      if (rec.pending === 0 && !holdList.length) break;
    }
    await Promise.allSettled(pending.splice(0));
    // 重试到记录清空：前两次还带着故障，之后没有
    for (let i = 0; i < 6 && rec.failed; i += 1) {
      faultsOn = !ideal && i < 2;
      pending.push(preview.retrySaves(id));
      for (let j = 0; j < 12; j += 1) {
        releaseHolds();
        await tick(3);
      }
      await Promise.allSettled(pending.splice(0));
    }
    faultsOn = false;
    for (let i = 0; i < 6; i += 1) {
      releaseHolds();
      await tick(3);
    }
    await Promise.allSettled(pending.splice(0));
    updateCanvas();
  };
  // 用户看到的每一个状态：界面每重画一次（onChange / onRectDrag / redrawSide）就记一份
  for (const hook of ['onChange', 'onRectDrag', 'redrawSide']) {
    const orig = v[hook];
    v[hook] = (...a) => {
      if (v.ed && v.crop && v.crop.frame) seen.push(screenState(v));
      return orig?.apply(v, a);
    };
  }
  const note = () => {
    updateCanvas();
    seen.push(screenState(v));
  };
  const check = (label) => {
    const s = screenState(v);
    const srv = serverState(id);
    const status = preview.saveStatus(id);
    if (rec.failed) problems.push(`${label}: 重试之后记录还没清空`);
    if (status === 'saving') problems.push(`${label}: 写完之后状态还是 saving`);
    if (!same(s.geometry, srv.geometry)) problems.push(`${label}: 几何不一致 屏幕 ${JSON.stringify(s.geometry)} 服务器 ${JSON.stringify(srv.geometry)}`);
    if (!near(s.rect, srv.rect, 2e-3) || s.preset !== srv.preset) problems.push(`${label}: 框不一致 屏幕 ${s.preset} [${s.rect.map((x) => +x.toFixed(4))}] 服务器 ${srv.preset} [${srv.rect.map((x) => +x.toFixed(4))}]`);
    if (!same(s.frame, srv.frame)) problems.push(`${label}: 画布不一致 屏幕 ${s.frame} 服务器 ${srv.frame}`);
    return s;
  };
  const act = async (step) => {
    updateCanvas();
    const [kind, a, b] = step;
    T('STEP', step, '屏幕', stateText(screenState(v)));
    if (kind === 'rotate') pending.push(correct.rotate90(v));
    else if (kind === 'flip') pending.push(correct.flipHorizontal(v));
    else if (kind === 'slider') {
      const el = input(labels[a]);
      el.dispatchEvent(new Event('pointerdown'));
      el.value = String(b);
      el.dispatchEvent(new Event('input'));
      el.dispatchEvent(new Event('pointerup'));
    } else if (kind === 'straighten') correct.nudgeStraighten(v, a);
    else if (kind === 'auto') {
      autoAnswer = { straighten: a, vertical: b, confident: true, message: '' };
      pending.push(correct.autoCorrect(v));
    } else if (kind === 'ratio') pending.push(cropMod.applyPreset(v, a));
    else if (kind === 'nudge') {
      const r0 = v.crop.rect;
      preview.beginChange(v);
      v.crop.rect = cropMod.fitCrop(v, geom.moveRect(r0, a, b)).rect;
      v.onRectDrag?.();
      pending.push(preview.saveCrop(v, cropMod.cropBody(v, { preset: v.crop.preset, rect: cropMod.rectArray(v.crop.rect) }, cropMod.orientationNow(v))));
    } else if (kind === 'undo') preview.undoChange(v);
    else if (kind === 'redo') preview.redoChange(v);
    else if (kind === 'retry') pending.push(preview.retrySaves(id));
  };

  note();
  trace = [];
  faultCount = 0;
  faultsOn = !ideal;
  for (const step of steps) {
    if (step[0] === 'settle') {
      await settle();
      if (!ideal) check(`settle@${steps.indexOf(step)}`);
      note();
      faultsOn = !ideal;
      continue;
    }
    await act(step);
    if (ideal) await settle();
    else await tick(Math.floor(faultRng() * 4));
    note();
  }
  await settle();
  const last = ideal ? screenState(v) : check('end');
  note();
  const final = serverState(id);
  const history = [];
  // 写完之后撤销、重做：只能走到用户见过的状态
  if (!ideal) {
    // 见过的状态；或者见过的框因为放不进现在的几何、被服务器收小了（中心不动、只变小）——收的结果界面上也看得到，算数
    const centre = (r) => [r[0] + r[2] / 2, r[1] + r[3] / 2];
    const seenAt = (s) => seen.some((x) => same(x.geometry, s.geometry) && x.preset === s.preset
      && (near(x.rect, s.rect, 0.02) || (near(centre(x.rect), centre(s.rect), 0.03) && s.rect[2] <= x.rect[2] + 0.02 && s.rect[3] <= x.rect[3] + 0.02)));
    for (const op of ['undo', 'redo', 'undo']) {
      const before = JSON.stringify(screenState(v));
      await act([op]);
      await settle();
      const s = check(`${op}-after-settle`);
      history.push(`${op}→${stateText(s)}`);
      if (JSON.stringify(s) !== before && !seenAt(s)) problems.push(`${op}: 走到了用户没见过的状态 ${stateText(s)}`);
      seen.push(s);
    }
  }
  section.remove();
  const nulls = photos.get(id).nullWrites;
  const clicks = steps.filter((st) => st[0] === 'ratio').length;
  if (nulls > clicks) problems.push(`服务器自己算的居中框写了 ${nulls} 次，点比例只有 ${clicks} 次：别的地方不该让服务器编框`);
  return { problems, final, last, log: photos.get(id).log || [], history, toasts: toastText().length, faults: faultCount, trace: trace.slice() };
}

const failures = [];
let done = 0;
for (let seed = seedStart; seed < seedStart + count; seed += 1) {
  const steps = makeSteps(seed);
  let real;
  let ideal;
  try {
    ideal = await runSequence(seed, steps, { ideal: true });
    document.getElementById('toast-root').replaceChildren();
    real = await runSequence(seed, steps, { ideal: false });
  } catch (e) {
    failures.push({ seed, steps: steps.map((s) => JSON.stringify(s)), problems: [`脚本出错：${e && e.stack ? e.stack.split('\n').slice(0, 4).join(' | ') : e}`] });
    continue;
  }
  const problems = [...real.problems];
  // 和理想情形比：没有故障时，延迟、挂住再放行都不能改变结果；有故障时只允许「丢掉失败的那一笔并告诉用户」
  const x = ideal.final;
  const y = real.final;
  // 有写入被挂住或失败过：丢掉的中间一笔会少收一次框，界面接着操作用的也是还没跟上服务器收框的框，最后的框大小会不同，不比。
  // 只有延迟的：几何、比例必须一样，框的中心必须在同一处（用户在服务器收框的结果回来之前就接着操作，框的大小因收的先后可以差一点，位置不能被换掉）
  // 自动的结果要等分析回来才落到几何上，期间用户做的别的操作先写进去：顺序和理想情形不一样，框的收法不可交换，所以含自动的不比
  if (!real.faults && !steps.some((st) => st[0] === 'auto') && (!same(x.geometry, y.geometry) || x.preset !== y.preset || !near([x.rect[0] + x.rect[2] / 2, x.rect[1] + x.rect[3] / 2], [y.rect[0] + y.rect[2] / 2, y.rect[1] + y.rect[3] / 2], 0.03))) {
    problems.push(`DIVERGE 和理想情形不同：理想 ${stateText(x)} 实际 ${stateText(y)}`);
  }
  if (problems.length) failures.push({ seed, steps: steps.map((s) => JSON.stringify(s)), problems, history: real.history, trace: process.env.FUZZ_TRACE ? real.trace : undefined });
  done += 1;
}
console.log(JSON.stringify({ sequences: count, failures: failures.slice(0, Number(process.env.FUZZ_KEEP || 12)), failed: failures.length, done }));
process.exit(0);
