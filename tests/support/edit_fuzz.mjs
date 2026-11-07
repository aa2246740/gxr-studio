// 裁剪 / 几何的随机交错测试（种子固定，可重现）。
// 真正的 viewer.js、preview.js、correct.js、crop.js、saves.js、undo_entries.js 在 node 里跑；
// 服务器是真的：tests/test_edit_fuzz.py 起一个真服务器（真的路由、sidecar、收框、校验），这里的「替身」只做一件事——
// 在请求前加随机延迟、挂住（等放行）、500、rev_conflict（409，不转发，服务器上什么也没变），其余原样转发给真服务器。
// 写编辑、写框走测试专用的包装路由（tests/support/fit_checks.py）：先调用真正的处理函数，再用真代码检查写下去的框
// （能不能通过 validate_crop、有没有落在有效区里、fit 有没有保留中心、有没有收得比必要的更小）。
// 每条序列 6–10 步，从这些里抽：旋转、翻转、垂直 / 水平 / 畸变滑块、拉直、自动、点比例、挪框、撤销、重做、重试、等写完（settle）。
// 同一串步骤再在「没有故障、每步等写完」的理想情形下跑一遍。
// 用法：node edit_fuzz.mjs <模块目录> <起始种子> <条数> <端口> <令牌> <照片 id>。输出一行 JSON，断言在 Python 那边。
import { pathToFileURL } from 'node:url';
import { installDom, tick } from './mini_dom.mjs';

const [folder, seedArg, countArg, portArg, token, realId] = process.argv.slice(2);
const seedStart = Number(seedArg || 1);
const count = Number(countArg || 100);
const base = `http://127.0.0.1:${portArg}`;
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
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---- 真服务器 ---------------------------------------------------------------
class ApiErr extends Error {
  constructor(status, code, message, data) {
    super(message || '无法完成操作。请重试。');
    this.status = status;
    this.code = code;
    this.data = data;
  }
}

async function direct(method, path, body) {
  const res = await fetch(base + path, {
    method,
    headers: { 'X-GXR-Token': token, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) throw new ApiErr(res.status, data?.error || `http_${res.status}`, data?.message, data);
  return data;
}

// ---- 故障与延迟（只在转发之前）--------------------------------------------------
let faultRng = rng(1);
let faultsOn = false;
let latencyOnly = false; // 只加延迟、不出故障（串行的序列）
let holdList = []; // 挂住的 PUT：{ resolve }
let inflight = 0; // 正在路上的请求（含挂住的）
let trace = [];
let faultCount = 0;
let serverProblems = []; // 包装路由用真代码查出来的问题
const T = (...a) => trace.push(a.map((x) => (typeof x === 'string' ? x : JSON.stringify(x))).join(' '));

async function maybeFault(method) {
  await tick(faultsOn || latencyOnly ? Math.floor(faultRng() * 3) : 0);
  if (!faultsOn || method !== 'PUT') return;
  const roll = faultRng();
  if (roll < 0.1) {
    faultCount += 1; // 挂住也算：界面在回应回来之前接着操作，用的是还没跟上服务器收框的框
    T('  挂住 PUT');
    await new Promise((resolve) => holdList.push({ resolve }));
  } else if (roll < 0.18) {
    faultCount += 1;
    T('  500 PUT');
    throw new ApiErr(500, 'disk_full', '磁盘已满，无法写入编辑记录。', null);
  } else if (roll < 0.24) {
    faultCount += 1;
    T('  409 PUT');
    throw new ApiErr(409, 'rev_conflict', '这张照片已在别处修改。请刷新后重试。', null);
  }
}
const releaseHolds = () => {
  const list = holdList;
  holdList = [];
  for (const h of list) h.resolve();
};

let autoAnswer = { straighten: 0, vertical: 0, confident: false, message: '未检测到明显的水平线' };
let photoMeta = null;

globalThis.apiStub = async (url, opts = {}) => {
  const method = opts.method || 'GET';
  let m;
  inflight += 1;
  try {
    if (method === 'GET' && (m = /^\/api\/photos\/(\w+)\/(edit|crop)$/.exec(url))) {
      await maybeFault('GET');
      const r = await direct('GET', `/api/photos/${realId}/${m[2]}`);
      T('  GET', m[2], m[2] === 'crop' ? r.rect : r.geometry);
      return r;
    }
    if (method === 'PUT' && (m = /^\/api\/photos\/(\w+)\/(edit|crop)$/.exec(url))) {
      await maybeFault('PUT');
      const out = await direct('PUT', `/__test/photos/${realId}/${m[2]}`, opts.body);
      for (const p of out.problems) serverProblems.push(`${m[2]} ${JSON.stringify(m[2] === 'crop' ? { preset: opts.body.preset, rect: opts.body.rect, fit: opts.body.fit } : opts.body.geometry)}: ${p}`);
      T('  PUT', m[2], m[2] === 'edit' ? opts.body.geometry : { preset: opts.body.preset, rect: opts.body.rect, fit: !!opts.body.fit }, '→', m[2] === 'edit' ? out.resp.geometry : out.resp.rect);
      return out.resp;
    }
    if (method === 'GET' && (m = /^\/api\/photos\/(\w+)$/.exec(url))) return { ...photoMeta, id: m[1] };
    if (method === 'GET' && url.startsWith('/api/photos?')) return [photoMeta];
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
    throw new Error(`没有替身：${method} ${url}`);
  } finally {
    inflight -= 1;
  }
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
const centre = (r) => [r[0] + r[2] / 2, r[1] + r[3] / 2];
// 同一个框的两种收法：重叠占小的那个的 60% 以上。收的先后不同（一个框用的是服务器收过的、一个用的是界面还没跟上的），
// 框的大小和被有效区推开的距离会不同，中心可以差开；但不能是两块没什么关系的地方
function boxesAgree(a, b) {
  const w = Math.min(a[0] + a[2], b[0] + b[2]) - Math.max(a[0], b[0]);
  const h = Math.min(a[1] + a[3], b[1] + b[3]) - Math.max(a[1], b[1]);
  if (w <= 0 || h <= 0) return false;
  return (w * h) / Math.min(a[2] * a[3], b[2] * b[3]) >= 0.6;
}

// ---- 一条序列 --------------------------------------------------------------
const ITEMS = [
  { id: 'original', label: '原始' },
  { id: 'free', label: '自由' },
  { id: '1:1', label: '1:1' },
  { id: '3:2', label: '3:2' },
  { id: '16:9', label: '16:9' },
  { id: 'xpan-65-24', label: 'XPan 65:24' },
  { id: '1.91:1', label: 'Instagram 1.91:1', orient: 'landscape' },
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
    else if (kind === 'straighten') steps.push(['straighten', pick(r, [-2, -0.5, 0.5, 2, 4, 6])]);
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
async function serverState() {
  const [edit, crop] = await Promise.all([direct('GET', `/api/photos/${realId}/edit`), direct('GET', `/api/photos/${realId}/crop`)]);
  return { geometry: { straighten: 0, vertical: 0, horizontal: 0, distortion: 0, rotate90: 0, flip: false, ...(edit.geometry || {}) }, rect: crop.rect, preset: crop.preset, frame: [crop.frame.width, crop.frame.height] };
}
const stateText = (s) => `${JSON.stringify(s.geometry)} ${s.preset} [${s.rect.map((x) => +x.toFixed(4))}] ${s.frame}`;

async function runSequence(seed, steps, { ideal }) {
  // 每三条里有一条是「串行」的：只加延迟、不出故障，每一步等上一步全部写完才开始（和理想情形的操作顺序一样）
  const serialMode = !ideal && seed % 3 === 0;
  latencyOnly = serialMode;
  const id = `${ideal ? 'b' : 'a'}${seed.toString(16)}`; // 路由只认十六进制的 id
  // 照片回到没有任何编辑的样子（真服务器上的同一张照片，一次只跑一条序列）
  await direct('DELETE', `/api/photos/${realId}/edit`);
  await direct('DELETE', `/api/photos/${realId}/crop`);
  serverProblems = [];
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
  // 预览画布：界面显示的图是按界面当前（还没保存的）几何出的，宽高就是这套几何的画布；用真服务器的 output_size 算
  const frames = new Map();
  const updateCanvas = async () => {
    const key = JSON.stringify(v.ed.geometry);
    if (!frames.has(key)) frames.set(key, await direct('POST', '/__test/frame', { shot_id: realId, geometry: v.ed.geometry }));
    const f = frames.get(key);
    v.canvas = { w: f.width, h: f.height };
  };
  const rec = saves.saveRecord(id);
  // 请求是真的 HTTP，要等真实的时间：按条件等（没有请求在路上、没有排着的写入、没有挂住的），不能数个空转的 tick
  const quiet = async () => {
    for (let i = 0; i < 4000; i += 1) {
      releaseHolds();
      if (inflight === 0 && rec.pending === 0 && !holdList.length) {
        await tick(3);
        if (inflight === 0 && rec.pending === 0 && !holdList.length) return;
      }
      await sleep(2);
    }
    problems.push('等了 12 秒还有请求没完成');
  };
  const settle = async () => {
    await quiet();
    await Promise.allSettled(pending.splice(0));
    // 重试到记录清空：前两次还带着故障，之后没有
    for (let i = 0; i < 6 && rec.failed; i += 1) {
      faultsOn = !ideal && !serialMode && i < 2;
      pending.push(preview.retrySaves(id));
      await quiet();
      await Promise.allSettled(pending.splice(0));
    }
    faultsOn = false;
    await quiet();
    await Promise.allSettled(pending.splice(0));
    await updateCanvas();
  };
  // 用户看到的每一个状态：界面每重画一次（onChange / onRectDrag / redrawSide）就记一份
  for (const hook of ['onChange', 'onRectDrag', 'redrawSide']) {
    const orig = v[hook];
    v[hook] = (...a) => {
      if (v.ed && v.crop && v.crop.frame) seen.push(screenState(v));
      return orig?.apply(v, a);
    };
  }
  const note = async () => {
    await updateCanvas();
    seen.push(screenState(v));
  };
  // 见过的状态；或者见过的框因为放不进现在的几何、被服务器收小了（中心不动、只变小）——收的结果界面上也看得到，算数
  const seenAt = (s) => seen.some((x) => same(x.geometry, s.geometry) && x.preset === s.preset
    && (near(x.rect, s.rect, 0.02) || (near(centre(x.rect), centre(s.rect), 0.03) && s.rect[2] <= x.rect[2] + 0.02 && s.rect[3] <= x.rect[3] + 0.02)));
  const check = async (label) => {
    const s = screenState(v);
    const srv = await serverState();
    const status = preview.saveStatus(id);
    if (rec.failed) problems.push(`${label}: 重试之后记录还没清空`);
    if (status === 'saving') problems.push(`${label}: 写完之后状态还是 saving`);
    if (!same(s.geometry, srv.geometry)) problems.push(`${label}: 几何不一致 屏幕 ${JSON.stringify(s.geometry)} 服务器 ${JSON.stringify(srv.geometry)}`);
    if (!near(s.rect, srv.rect, 2e-3) || s.preset !== srv.preset) problems.push(`${label}: 框不一致 屏幕 ${s.preset} [${s.rect.map((x) => +x.toFixed(4))}] 服务器 ${srv.preset} [${srv.rect.map((x) => +x.toFixed(4))}]`);
    if (!same(s.frame, srv.frame)) problems.push(`${label}: 画布不一致 屏幕 ${s.frame} 服务器 ${srv.frame}`);
    // 写完以后服务器上存的框必须通得过严格校验（validate_crop）：服务器收出来的框（写几何、fit）自己也不能不合格
    for (const p of (await direct('GET', `/__test/photos/${realId}/stored-check`)).problems) problems.push(`${label}: ${p}`);
    return s;
  };
  const act = async (step) => {
    await updateCanvas();
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

  await note();
  trace = [];
  faultCount = 0;
  faultsOn = !ideal && !serialMode;
  // 串行：每一步开始时上一步已经全部写完、没有请求在路上，也没有出过故障。
  // 这样的序列和理想情形的操作顺序完全一样，结果（包括框的大小）必须一样
  const idle = async () => {
    for (let i = 0; i < 4000 && (inflight > 0 || rec.pending > 0); i += 1) await sleep(2);
    await tick(2);
  };
  for (const step of steps) {
    if (step[0] === 'settle') {
      await settle();
      if (!ideal) await check(`settle@${steps.indexOf(step)}`);
      await note();
      faultsOn = !ideal && !serialMode;
      continue;
    }
    await act(step);
    if (ideal) await settle();
    else if (serialMode) await idle();
    else await tick(Math.floor(faultRng() * 4));
    await note();
  }
  await settle();
  const last = ideal ? screenState(v) : await check('end');
  await note();
  const final = await serverState();
  const history = [];
  if (!ideal) {
    // 最后停下来的状态是用户见过的，或者见过的框被服务器收小的结果
    if (!seenAt(last)) problems.push(`写完之后停在了用户没见过的状态 ${stateText(last)}`);
    // 写完之后撤销、重做：只能走到用户见过的状态
    for (const op of ['undo', 'redo', 'undo']) {
      const before = JSON.stringify(screenState(v));
      await act([op]);
      await settle();
      const s = await check(`${op}-after-settle`);
      history.push(`${op}→${stateText(s)}`);
      if (JSON.stringify(s) !== before && !seenAt(s)) problems.push(`${op}: 走到了用户没见过的状态 ${stateText(s)}`);
      seen.push(s);
    }
  }
  section.remove();
  // 服务器自己算的居中框只出现在点比例的时候（用写进去的次数数）
  const writes = (await direct('GET', `/__test/photos/${realId}/stats`)).null_crop_writes;
  const clicks = steps.filter((st) => st[0] === 'ratio').length;
  if (writes > clicks) problems.push(`服务器自己算的居中框写了 ${writes} 次，点比例只有 ${clicks} 次：别的地方不该让服务器编框`);
  for (const p of serverProblems) problems.push(`服务器上的框：${p}`);
  return { problems, final, last, history, toasts: toastText().length, faults: faultCount, serial: serialMode && faultCount === 0, trace: trace.slice() };
}

photoMeta = await direct('GET', `/api/photos/${realId}`);
const failures = [];
let done = 0;
let serialCompared = 0;
for (let seed = seedStart; seed < seedStart + count; seed += 1) {
  const steps = makeSteps(seed);
  let real;
  let ideal;
  try {
    // 含自动的、非串行的序列不和理想情形比（见下），也就不用跑理想情形
    const needIdeal = seed % 3 === 0 || !steps.some((st) => st[0] === 'auto');
    if (needIdeal) {
      await direct('POST', `/__test/photos/${realId}/stats/reset`);
      ideal = await runSequence(seed, steps, { ideal: true });
      document.getElementById('toast-root').replaceChildren();
    }
    await direct('POST', `/__test/photos/${realId}/stats/reset`);
    real = await runSequence(seed, steps, { ideal: false });
  } catch (e) {
    failures.push({ seed, steps: steps.map((s) => JSON.stringify(s)), problems: [`脚本出错：${e && e.stack ? e.stack.split('\n').slice(0, 4).join(' | ') : e}`] });
    continue;
  }
  const problems = [...real.problems, ...(ideal ? ideal.problems.map((p) => `理想情形：${p}`) : [])];
  const x = ideal?.final;
  const y = real.final;
  const hasAuto = steps.some((st) => st[0] === 'auto');
  const hasUndo = steps.some((st) => st[0] === 'undo' || st[0] === 'redo');
  if (real.serial && real.faults === 0) {
    // 串行、没有故障：和理想情形的操作顺序完全一样，几何、比例、框（连大小）都必须一样（1 像素以内）
    serialCompared += 1;
    const px = 1.5 / photoMeta.width;
    if (!same(x.geometry, y.geometry) || x.preset !== y.preset || !near(x.rect, y.rect, Math.max(px, 1.5 / photoMeta.height))) {
      problems.push(`DIVERGE 串行的序列和理想情形不同：理想 ${stateText(x)} 实际 ${stateText(y)}`);
    }
  } else if (!hasAuto && !hasUndo && ideal) {
    // 有重叠或有故障（不含自动）：本地的几何一步一步累加，和服务器写成什么样无关，最后的几何必须和理想情形一样；
    // 框的中心必须在同一处（用户在服务器收框的结果回来之前接着操作，或者丢了中间一笔，框的大小可以因收的先后差一点，位置不能被换掉）
    if (!same(x.geometry, y.geometry) || x.preset !== y.preset || !boxesAgree(x.rect, y.rect)) {
      problems.push(`DIVERGE 有重叠或故障的序列和理想情形不同：理想 ${stateText(x)} 实际 ${stateText(y)}`);
    }
  }
  // 含撤销 / 重做的、有重叠或故障的也不比：同一个动作作用在界面当时的框上，框还没跟上服务器收的结果时「挪框」可能什么也没挪动、
  // 就不记一步撤销（理想情形里框已经跟上，挪动了，记了一步），两边的撤销记录不一样长，撤销撤掉的步骤不同，几何本来就可以不同。
  // 含自动的、有重叠或故障的不和理想情形比：自动的结果要等分析回来才落到几何上，期间用户做的别的操作先写进去，
  // 最后写进去的才算数（例如自动之后又拖垂直，理想情形是拖的算数，真实情形里自动的结果晚到、反而盖掉它），几何本来就可以不同
  if (problems.length) failures.push({ seed, steps: steps.map((s) => JSON.stringify(s)), problems, history: real.history, trace: process.env.FUZZ_TRACE ? real.trace : undefined });
  done += 1;
}
console.log(JSON.stringify({ sequences: count, failures: failures.slice(0, Number(process.env.FUZZ_KEEP || 12)), failed: failures.length, done, serialCompared }));
process.exit(0);
