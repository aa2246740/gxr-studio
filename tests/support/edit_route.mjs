// 编辑保存与路由的正确性（R03、R04、R05、R11）：真正的 viewer.js、preview.js、router.js、export.js，
// 接口用替身，哪个请求什么时候返回由脚本决定（不靠时间间隔）。
// 用法：node edit_route.mjs <模块目录> <场景>。输出一行 JSON，断言在 Python 那边。
import { pathToFileURL } from 'node:url';
import { installDom, gate, tick } from './mini_dom.mjs';

const folder = process.argv[2];
const scenario = process.argv[3];
const file = (name) => pathToFileURL(`${folder}/${name}`).href;

installDom();

// ---- 接口替身 ------------------------------------------------------------------
const requests = []; // { method, url, body, at }
const g = gate();
const holds = new Set(); // 'METHOD url' → 这一类请求先挂起，等脚本放行
const failures = new Map(); // 'METHOD url' → 要抛的错误（只抛一次）
const revs = { A: 1, B: 7 };
const PHOTOS = {
  A: { id: 'a0', name: 'R0010001.DNG', date: '2026-10-01', captured_at: '2026-10-01T10:00:00', has_raw: true, has_jpg: true, width: 3000, height: 2000, lens: '50mm' },
  B: { id: 'b0', name: 'R0010002.DNG', date: '2026-10-01', captured_at: '2026-10-01T10:01:00', has_raw: true, has_jpg: true, width: 3000, height: 2000, lens: '50mm' },
};
const byId = (id) => Object.values(PHOTOS).find((p) => p.id === id);
const key = (id) => Object.keys(PHOTOS).find((k) => PHOTOS[k].id === id);
const editOf = (id) => ({ rev: revs[key(id)], recipe: null, lut: null, adjust: {}, curve: null, geometry: {}, frame: null, stamp: null, memory: {} });
const cropOf = (id) => ({ preset: 'original', rect: [0, 0, 1, 1], angle_deg: 0, orientation: 'landscape', frame: { width: 3000, height: 2000 }, rev: revs[key(id)] });

class StubError extends Error {
  constructor(status, message) { super(message); this.status = status; this.code = `http_${status}`; this.data = { message }; }
}
globalThis.StubError = StubError;

async function answer(method, url, body) {
  let m;
  if (method === 'GET' && (m = /^\/api\/photos\/(\w+)$/.exec(url))) return byId(m[1]);
  if (method === 'GET' && url.startsWith('/api/photos?')) return Object.values(PHOTOS);
  if (method === 'GET' && (m = /^\/api\/photos\/(\w+)\/edit$/.exec(url))) return editOf(m[1]);
  if (method === 'GET' && (m = /^\/api\/photos\/(\w+)\/crop$/.exec(url))) return cropOf(m[1]);
  if (method === 'GET' && /\/capabilities$/.test(url)) return { raw: true, recipe: 'full', reason: '' };
  if (method === 'GET' && /\/meta$/.test(url)) return {};
  if (method === 'GET' && url === '/api/recipes') return { units: [] };
  if (method === 'GET' && url === '/api/frames') return { styles: [] };
  if (method === 'GET' && url === '/api/defaults/finish') return { frame: { style: 'none' }, stamp: { enabled: false } };
  if (method === 'PUT' && (m = /^\/api\/photos\/(\w+)\/(edit|crop)$/.exec(url))) {
    const k = key(m[1]);
    if (body.expected_rev !== revs[k]) throw new StubError(409, '这张照片刚被别处改过。');
    revs[k] += 1;
    return m[2] === 'edit' ? { ...editOf(m[1]), rev: revs[k] } : { ...cropOf(m[1]), ...body, rect: body.rect || [0, 0, 1, 1], rev: revs[k] };
  }
  if (method === 'POST' && (m = /^\/api\/photos\/(\w+)\/export$/.exec(url))) return { path: '/tmp/x_gxr.jpg', width: 10, height: 10, bytes: 100, export_id: 'e1' };
  if (method === 'PUT' && url === '/api/prefs') return body;
  throw new Error(`没有替身：${method} ${url}`);
}

globalThis.apiStub = async (url, opts = {}) => {
  const method = opts.method || 'GET';
  const k = `${method} ${url}`;
  requests.push({ method, url, body: opts.body ?? null });
  if (holds.has(k)) {
    holds.delete(k);
    await g.hold(k);
  }
  if (failures.has(k)) {
    const e = failures.get(k);
    failures.delete(k);
    throw e;
  }
  return answer(method, url, opts.body);
};
// 预览图：永远不回来也没关系（AbortController 会取消）；这里直接给一张空图
globalThis.apiBlobStub = async () => ({ blob: {}, headers: { get: () => null } });

const { state } = await import(file('state.js'));
const viewer = await import(file('viewer.js'));
const preview = await import(file('preview.js'));
const { createRouter } = await import(file('router.js'));
state.settings = { gpu_preview: false, export_dir: '/tmp/out' };
state.prefs = { export_hdr: false };
state.photos = Object.values(PHOTOS);
state.camdata = { ready: true, state: 'ready', dataVersion: 'v1' };

const root = document.getElementById('view');
const find = (n, test) => (test(n) ? n : n.children.map((c) => find(c, test)).find(Boolean));
const buttonByText = (text, under = document.body) => find(under, (n) => n.tagName === 'BUTTON' && n.textContent.trim() === text);
const puts = (id) => requests.filter((r) => r.method === 'PUT' && r.url.startsWith(`/api/photos/${id}/`));
const shown = () => {
  const v = viewer.currentView();
  return v ? v.photo.id : null;
};

// 和 app.js 一样的路由表（图库页用一个替身，只写一行字）
const router = createRouter({
  pages: {
    photos: { enter: async () => { root.replaceChildren(Object.assign(document.createElement('div'), { id: 'gallery-stub', text: 'GALLERY' })); return null; } },
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

// 打开这张照片的导出表单（⌘E 的入口），返回表单所在的节点
async function openExport() {
  viewer.openViewerTab('export');
  await tick();
  const submit = find(document.body, (n) => n.tagName === 'BUTTON' && n.textContent.trim() === '导出' && n.parentNode && n.parentNode.classList.contains('modal-foot'));
  return submit.parentNode.parentNode;
}

const out = {};

if (scenario === 'reset-then-switch') {
  // R03：在 A 上点「复原」，第一笔保存还没返回就切到 B；A 的响应在 B 打开之后才回来
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  holds.add(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  const reset = buttonByText('复原');
  out.hadReset = !!reset;
  reset.click();
  await tick();
  await go(`#/photo/${PHOTOS.B.id}`);
  await tick();
  out.shownBeforeRelease = shown();
  g.release(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  await tick(10);
  out.shownAfter = shown();
  out.putsB = puts(PHOTOS.B.id).map((r) => r.url);
  out.putsA = puts(PHOTOS.A.id).map((r) => ({ url: r.url, rev: r.body.expected_rev, angle: r.body.angle_deg ?? null, rect: r.body.rect ?? null }));
  out.pageHasB = root.textContent.includes(PHOTOS.B.name);
  out.pageHasA = root.textContent.includes(PHOTOS.A.name);
} else if (scenario === 'reset-then-leave') {
  // R03 的另一种：复原等待中回到图库（不应访问空的 view，也不应写别的照片）
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  holds.add(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  buttonByText('复原').click();
  await tick();
  await go('#/photos');
  g.release(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  await tick(10);
  out.putsA = puts(PHOTOS.A.id).map((r) => r.url);
  out.putsB = puts(PHOTOS.B.id).map((r) => r.url);
  out.page = root.textContent;
} else if (scenario === 'queued-save-survives-switch') {
  // R04：连改两项（第一笔在路上、第二笔排队）后立刻切到 B；第二笔必须照样写进 A
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  const v = viewer.currentView();
  holds.add(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  const first = preview.persist(v, { adjust: { exposure_ev: 0.5 } });
  const second = preview.persist(v, { curve: { master: [[0, 0], [1, 1]] } });
  await tick();
  await go(`#/photo/${PHOTOS.B.id}`);
  await tick();
  out.statusWhileSaving = preview.saveStatus(PHOTOS.A.id);
  // 又切回 A：读编辑记录要等 A 排着的写入写完
  const back = go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  out.editReadsBeforeRelease = requests.filter((r) => r.method === 'GET' && r.url === `/api/photos/${PHOTOS.A.id}/edit`).length;
  g.release(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  out.results = await Promise.all([first, second]);
  await back;
  await tick(10);
  out.putsA = puts(PHOTOS.A.id).map((r) => ({ url: r.url, keys: Object.keys(r.body).sort(), rev: r.body.expected_rev }));
  out.putsB = puts(PHOTOS.B.id).length;
  out.editReadsAfter = requests.filter((r) => r.method === 'GET' && r.url === `/api/photos/${PHOTOS.A.id}/edit`).length;
  out.statusAfter = preview.saveStatus(PHOTOS.A.id);
  out.reopenedRev = viewer.currentView().ed.rev;
  out.serverRev = revs.A;
} else if (scenario === 'failed-save-blocks-export') {
  // R05：一次保存失败后点导出：不能发出导出请求；给「重试保存」，重试成功后才能导出
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  const v = viewer.currentView();
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  out.saved = await preview.persist(v, { adjust: { exposure_ev: 1 } });
  out.status = preview.saveStatus(PHOTOS.A.id);
  await tick();
  out.saveLine = (find(document.body, (n) => n.classList && n.classList.contains('save-state')) || { textContent: '' }).textContent;
  try {
    await preview.flushEdits(v);
    out.flush = 'resolved';
  } catch (e) {
    out.flush = e.code;
    out.flushMessage = e.message;
  }
  const form = await openExport();
  buttonByText('导出', form).click();
  await tick(10);
  out.exportPostsAfterFailure = requests.filter((r) => r.method === 'POST' && r.url.endsWith('/export')).length;
  out.formText = form.textContent;
  const retry = buttonByText('重试保存', form);
  out.hasRetry = !!retry;
  retry.click();
  await tick(10);
  out.statusAfterRetry = preview.saveStatus(PHOTOS.A.id);
  out.retryPut = puts(PHOTOS.A.id).map((r) => ({ keys: Object.keys(r.body).sort(), rev: r.body.expected_rev }));
  buttonByText('导出', form).click();
  await tick(10);
  out.exportPostsAfterRetry = requests.filter((r) => r.method === 'POST' && r.url.endsWith('/export')).length;
} else if (scenario === 'stale-route-reversed') {
  // R11：打开 A（照片请求慢）后马上打开 B；B 先回来，A 后回来
  holds.add(`GET /api/photos/${PHOTOS.A.id}`);
  const a = go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  await go(`#/photo/${PHOTOS.B.id}`);
  await tick();
  out.shownBefore = shown();
  g.release(`GET /api/photos/${PHOTOS.A.id}`);
  out.aStillCurrent = await a;
  await tick(10);
  out.shownAfter = shown();
  out.pageHasB = root.textContent.includes(PHOTOS.B.name);
  out.pageHasA = root.textContent.includes(PHOTOS.A.name);
  out.keysFor = router.keys() ? shown() : null;
  out.aEditReads = requests.filter((r) => r.url === `/api/photos/${PHOTOS.A.id}/edit`).length;
} else if (scenario === 'stale-route-back-to-gallery') {
  // R11：打开 A 后马上回图库，A 的响应才到
  holds.add(`GET /api/photos/${PHOTOS.A.id}`);
  const a = go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  await go('#/photos');
  g.release(`GET /api/photos/${PHOTOS.A.id}`);
  out.aStillCurrent = await a;
  await tick(10);
  out.page = root.textContent;
  out.shown = shown();
  out.keys = router.keys();
  out.aEditReads = requests.filter((r) => r.url === `/api/photos/${PHOTOS.A.id}/edit`).length;
} else {
  throw new Error(`没有这个场景：${scenario}`);
}

console.log(JSON.stringify(out));
process.exit(0);
