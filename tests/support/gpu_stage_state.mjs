// gpu_stage.js 的状态和版本：接口、fetch、引擎都是替身，请求什么时候完成由脚本决定（不靠时间）。
// 用法：node gpu_stage_state.mjs <模块目录> <场景>；模块目录里有 gpu_stage.js、state.js 和替身 api.js、gpu/engine.js。
import { pathToFileURL } from 'node:url';

const folder = process.argv[2];
const scenario = process.argv[3];
const file = (name) => pathToFileURL(`${folder}/${name}`).href;

// ---- 替身 ----
const calls = { api: [], fetch: [], engine: [], boxBodies: [] };
const deferred = new Map(); // url 片段 → { resolve, reject }[]
const routes = { info: () => null, edit: () => ({ recipe: null }), table: null, bake: null, box: null };
Object.defineProperty(globalThis, 'navigator', { value: { gpu: {} }, configurable: true, writable: true });
globalThis.createImageBitmap = async () => ({ width: 120, height: 80, close() {} });
globalThis.engineLog = calls.engine;
globalThis.apiStub = async (path, opts) => {
  calls.api.push(path);
  if (path.endsWith('/gpu/box')) {
    calls.boxBodies.push(opts.body);
    return routes.box(opts.body);
  }
  const m = path.match(/^\/api\/photos\/([^/]+)\/(gpu|edit)$/);
  if (m && m[2] === 'gpu') return routes.info(m[1]);
  if (m && m[2] === 'edit') return routes.edit(m[1]);
  throw new Error(`没有替身：${path}`);
};
const okBody = (headers, bytes = 16) => ({ ok: true, status: 200, headers: { get: (k) => headers[k.toLowerCase()] ?? null }, arrayBuffer: async () => new ArrayBuffer(bytes), blob: async () => ({}) });
const fail = (status) => ({ ok: false, status, headers: { get: () => null } });
function hold(fragment) {
  return new Promise((resolve, reject) => {
    if (!deferred.has(fragment)) deferred.set(fragment, []);
    deferred.get(fragment).push({ resolve, reject });
  });
}
globalThis.fetch = async (url) => {
  calls.fetch.push(url);
  if (url.includes('/raw') || url.includes('/camera-jpg')) return okBody({});
  if (url.includes('/table')) return routes.table(url);
  if (url.includes('/bake')) return routes.bake(url);
  throw new Error(`没有替身：${url}`);
};
const lutHeader = (key) => ({ 'x-gxr-lut': encodeURIComponent(JSON.stringify({ n: 256, display: true, table8: true, key })) });
const bakeHeader = (key) => ({ 'x-gxr-bake': encodeURIComponent(JSON.stringify({ key, nx: 2, ny: 2, origin: [0, 0], step: 1, calibrated: true })) });
const INFO = { enabled: true, unit: 'jlaunch', raw: { url: '/api/photos/P/raw?v=1', layer: [100, 80], full: [200, 160] }, raw_reason: null, jpg: { url: '/api/photos/P/camera-jpg?v=1' }, scene: { A: [[1, 0, 0], [0, 1, 0], [0, 0, 1]], b: [0, 0, 0] } };
routes.info = () => INFO;

const { state, emit } = await import(file('state.js'));
const gs = await import(file('gpu_stage.js'));
state.settings = { gpu_preview: true };
state.camdata = { dataVersion: 'v1' };
const flush = () => new Promise((r) => setTimeout(r, 0));

function makeView(lut = null) {
  return { photo: { id: 'P', name: 'P' }, ed: { v2: true, lut, geometry: {}, frame: { style: 'none' }, stamp: { enabled: false } } };
}
const ctxFor = (recipe) => ({ stage: 'final', recipe, adjust: {}, curve: null, geometry: { straighten: 0, rotate90: 0, flip: false }, crop: null, geometryKey: 'k' });
const REC = { kind: 'builtin', unit: 'jlaunch', slot: 1, levels: {} };

// 反复 route，直到不再是 wait（每次等它要求的 rerun，或等事件循环转一圈）
async function settle(view, ctx, limit = 30) {
  let way = 'wait';
  for (let i = 0; i < limit && way === 'wait'; i++) {
    way = await new Promise((resolve) => {
      let done = false;
      const w = gs.route(view, ctx, () => { if (!done) { done = true; resolve('wait'); } });
      if (w !== 'wait') { done = true; resolve(w); }
      else setTimeout(() => { if (!done) { done = true; resolve('wait'); } }, 0);
    });
    await flush();
  }
  return way;
}
const lastLook = () => [...calls.engine].reverse().find((c) => c[0] === 'setLook')?.[1] || null;
const added = (kind) => calls.engine.filter((c) => c[0] === kind).map((c) => c[1]);

const out = {};
if (scenario === 'lut-patched-to-vlog') {
  let table = (url) => okBody(lutHeader('L-v1'), 64);
  routes.table = (url) => table(url);
  const view = makeView('L');
  out.first = await settle(view, ctxFor(null));
  // LUT 第一次要算几秒：这期间先用服务器出图，表到了再切到显卡
  out.afterLoad = await settle(view, ctxFor(null));
  out.lutKey = lastLook()?.lut;
  table = () => fail(422); // 输出空间改成了 vlog：服务器不再给表
  emit('luts-changed');
  out.afterEvent = await settle(view, ctxFor(null));
  out.again = await settle(view, ctxFor(null));
  out.tableRequests = calls.fetch.filter((u) => u.includes('/table')).length;
  out.addedLuts = added('addLut');
} else if (scenario === 'camdata-changed') {
  routes.bake = (url) => okBody(bakeHeader(url.includes('dv=v2') ? 'K2' : 'K1'));
  const view = makeView();
  out.before = await settle(view, ctxFor(REC));
  out.recipeBefore = lastLook()?.recipe;
  state.camdata = { dataVersion: 'v2' };
  emit('camdata-changed');
  out.after = await settle(view, ctxFor(REC));
  out.recipeAfter = lastLook()?.recipe;
  out.bakeUrls = calls.fetch.filter((u) => u.includes('/bake')).map((u) => (u.includes('dv=v2') ? 'v2' : 'v1'));
} else if (scenario === 'late-success' || scenario === 'late-failure') {
  routes.bake = (url) => hold(url.includes('dv=v2') ? 'v2' : 'v1');
  const view = makeView();
  out.first = gs.route(view, ctxFor(REC), () => {});
  await flush();
  out.firstInfo = await settle(view, ctxFor(REC), 3); // 照片信息到了，配方表还挂着
  state.camdata = { dataVersion: 'v2' };
  emit('camdata-changed');
  const stale = deferred.get('v1')?.shift();
  if (scenario === 'late-success') stale.resolve(okBody(bakeHeader('K1')));
  else stale.reject(new Error('旧请求失败'));
  await flush();
  await flush();
  out.addedAfterStale = added('addBake');
  const pendingNew = settle(view, ctxFor(REC));
  for (let i = 0; i < 10 && !deferred.get('v2')?.length; i++) await flush();
  deferred.get('v2')?.shift()?.resolve(okBody(bakeHeader('K2')));
  out.final = await pendingNew;
  if (out.final === 'wait') out.final = await settle(view, ctxFor(REC));
  out.recipe = lastLook()?.recipe;
  out.addedBakes = added('addBake');
} else if (scenario === 'prefetch-dedupe') {
  state.photos = [{ id: 'A', sidecar_rev: 1 }, { id: 'P', sidecar_rev: 1 }, { id: 'C', sidecar_rev: 0 }];
  routes.info = (id) => ({ ...INFO, raw: { ...INFO.raw, url: `/api/photos/${id}/raw?v=1` }, jpg: { url: `/api/photos/${id}/camera-jpg?v=1` } });
  routes.bake = (url) => okBody(bakeHeader('K1'));
  routes.edit = () => ({ recipe: REC });
  const view = makeView();
  out.first = await settle(view, ctxFor(REC));
  for (let i = 0; i < 25; i++) {
    view.ed = { ...view.ed };
    gs.route(view, { ...ctxFor(REC), adjust: { exposure_ev: i / 10 } }, () => {});
    await flush();
  }
  await flush();
  out.neighbourEdits = calls.api.filter((p) => /\/api\/photos\/(A|C)\/edit/.test(p)).length;
  out.neighbourInfos = calls.api.filter((p) => /\/api\/photos\/(A|C)\/gpu/.test(p)).length;
} else if (scenario === 'warp-export-size') {
  // 透视 / 畸变的成片：边框和时间戳按导出时的像素算版面。全尺寸（200×160）的裁切框到了就用它的宽高，不用颜色层（100×80）的估计
  routes.box = (b) => (b.width === 200 ? { box: [3, 4, 111, 77], valid: [0, 0, 1, 1] } : { box: [1, 1, 50, 36], valid: [0, 0, 1, 1] });
  routes.bake = () => okBody(bakeHeader('K1'));
  const view = makeView();
  const ctx = { ...ctxFor(REC), geometry: { straighten: 0, vertical: 30, horizontal: -10, distortion: 20, rotate90: 1, flip: false } };
  out.first = await settle(view, ctx);
  await flush();
  out.again = await settle(view, ctx);
  out.export = view.gpuExport;
  out.boxSizes = calls.boxBodies.map((b) => [b.width, b.height]);
  out.fullBodyGeometry = calls.boxBodies.find((b) => b.width === 200)?.geometry || null;
} else if (scenario === 'flag-off') {
  state.settings = { gpu_preview: false };
  const view = makeView();
  out.way = gs.route(view, ctxFor(REC), () => {});
  await flush();
  out.gpuInfoRequests = calls.api.filter((p) => p.endsWith('/gpu')).length;
  out.engineCreated = !!gs.gpuEngineForTests();
} else {
  throw new Error(`未知场景 ${scenario}`);
}
console.log(JSON.stringify(out));
