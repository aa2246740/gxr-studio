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
// 写进去的编辑字段留在这里，再读编辑记录时原样带回来（重开照片要看到写进去的内容）
const stored = {};
const editOf = (id) => ({ rev: revs[key(id)], recipe: null, lut: null, adjust: {}, curve: null, geometry: {}, frame: null, stamp: null, memory: {}, ...(stored[id] || {}) });
let camStatus = { ready: false, camera_data: { state: 'missing' }, calibration: { state: 'missing' } };
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
    if (m[2] === 'edit') {
      const { expected_rev: _rev, ...fields } = body;
      stored[m[1]] = { ...(stored[m[1]] || {}), ...fields };
    }
    return m[2] === 'edit' ? { ...editOf(m[1]), rev: revs[k] } : { ...cropOf(m[1]), ...body, rect: body.rect || [0, 0, 1, 1], rev: revs[k] };
  }
  if (method === 'POST' && (m = /^\/api\/photos\/(\w+)\/export$/.exec(url))) return { path: '/tmp/x_gxr.jpg', width: 10, height: 10, bytes: 100, export_id: 'e1' };
  if (method === 'PUT' && url === '/api/prefs') return body;
  if (method === 'POST' && url === '/api/export') return { id: 'j1', state: 'done', progress: { done: 1, total: 1, results: [] } };
  if (method === 'GET' && url === '/api/camdata/status') return camStatus;
  if (method === 'POST' && url === '/api/camdata/import-card') {
    camStatus = { ready: true, camera_data: { state: 'ready' }, calibration: { state: 'ready' } };
    return { message: '已导入相机配方文件' };
  }
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
// 预览图：永远不回来也没关系（AbortController 会取消）；这里直接给一张空图。
// blobManual 打开时，/render 请求排进 blobQueue，由脚本决定先后放行（对比图的乱序场景）
let blobManual = false;
const blobQueue = [];
globalThis.apiBlobStub = (url, opts = {}) => {
  if (blobManual && url.includes('/render')) {
    return new Promise((resolve) => blobQueue.push({ body: opts.body, resolve: () => resolve({ blob: { body: opts.body }, headers: { get: () => null } }) }));
  }
  return Promise.resolve({ blob: {}, headers: { get: () => null } });
};
let urlCount = 0;
const createUrl = URL.createObjectURL;
URL.createObjectURL = (b) => { urlCount += 1; return createUrl(b); };

const { state } = await import(file('state.js'));
const viewer = await import(file('viewer.js'));
const preview = await import(file('preview.js'));
const { createRouter } = await import(file('router.js'));
state.settings = { gpu_preview: false, export_dir: '/tmp/out' };
state.prefs = { export_hdr: false };
state.photos = Object.values(PHOTOS);
state.camdata = { ready: true, state: 'ready', dataVersion: 'v1' };

const root = document.getElementById('view');
// 顶栏：大图打开时照片名写在这里（#tb-edit），和真页面一样放在 #view 外面
const titlebar = Object.assign(document.createElement('header'), { id: 'titlebar' });
const tbEdit = Object.assign(document.createElement('div'), { id: 'tb-edit' });
titlebar.append(tbEdit);
document.body.append(titlebar);
const pageText = () => tbEdit.textContent + root.textContent;
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
  out.pageHasB = pageText().includes(PHOTOS.B.name);
  out.pageHasA = pageText().includes(PHOTOS.A.name);
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
  out.page = pageText();
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
  // R11：打开 A（照片请求慢）后马上打开 B；B 先回来，A 后回来。
  // 照片列表先清空：图库里没有这张照片的信息时才会去读 /api/photos/<id>（直接打开链接、刷新页面）
  state.photos = [];
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
  out.pageHasB = pageText().includes(PHOTOS.B.name);
  out.pageHasA = pageText().includes(PHOTOS.A.name);
  out.keysFor = router.keys() ? shown() : null;
  out.aEditReads = requests.filter((r) => r.url === `/api/photos/${PHOTOS.A.id}/edit`).length;
} else if (scenario === 'stale-route-back-to-gallery') {
  // R11：打开 A 后马上回图库，A 的响应才到
  state.photos = [];
  holds.add(`GET /api/photos/${PHOTOS.A.id}`);
  const a = go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  await go('#/photos');
  g.release(`GET /api/photos/${PHOTOS.A.id}`);
  out.aStillCurrent = await a;
  await tick(10);
  out.page = pageText();
  out.shown = shown();
  out.keys = router.keys();
  out.aEditReads = requests.filter((r) => r.url === `/api/photos/${PHOTOS.A.id}/edit`).length;
} else if (scenario === 'cached-open-stale-refresh') {
  // 从图库打开（照片信息已经在列表里）：页面立刻搭好，后台再读一次照片信息。
  // A 的这次后台读取比切到 B 还慢：回来以后不能把顶栏和页面换回 A
  holds.add(`GET /api/photos/${PHOTOS.A.id}`);
  const a = go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  out.shownFirst = shown();
  out.pageHadA = pageText().includes(PHOTOS.A.name);
  await a;
  await go(`#/photo/${PHOTOS.B.id}`);
  await tick();
  g.release(`GET /api/photos/${PHOTOS.A.id}`, { ...PHOTOS.A, name: 'RENAMED.DNG' });
  await tick(10);
  out.shownAfter = shown();
  out.pageHasB = pageText().includes(PHOTOS.B.name);
  out.pageHasStale = pageText().includes('RENAMED.DNG') || pageText().includes(PHOTOS.A.name);
  out.keysFor = router.keys() ? shown() : null;
} else if (scenario === 'retry-overlap') {
  // F01：重试是保存队列里的一笔。重试的 GET 还没回来时，排进一笔更新的修改、再点导出（flush）：
  // flush 要等重试和新修改都写完；新修改最后写，服务器上留下的是新值
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  const v = viewer.currentView();
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  out.saved = await preview.persist(v, { adjust: { exposure_ev: 1 } });
  holds.add(`GET /api/photos/${PHOTOS.A.id}/edit`);
  const retry = preview.retrySaves(PHOTOS.A.id);
  const newer = preview.persist(v, { adjust: { exposure_ev: 2 } });
  const trace = [];
  const flush = preview.flushEdits(v).then(() => trace.push(`flush@${puts(PHOTOS.A.id).length}`), (e) => trace.push(`flush:${e.code}`));
  await tick();
  out.statusWhileRetrying = preview.saveStatus(PHOTOS.A.id);
  out.flushedEarly = trace.length > 0;
  g.release(`GET /api/photos/${PHOTOS.A.id}/edit`);
  out.retry = await retry;
  out.newer = await newer;
  await flush;
  await tick(5);
  out.putValues = puts(PHOTOS.A.id).map((r) => r.body.adjust.exposure_ev);
  out.trace = trace;
  out.status = preview.saveStatus(PHOTOS.A.id);
  out.server = stored[PHOTOS.A.id].adjust.exposure_ev;
} else if (scenario === 'newer-edit-supersedes-failed') {
  // F01：失败之后、点重试之前又改了同一项并写成功：重试不能拿旧值盖回去
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  const v = viewer.currentView();
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  await preview.persist(v, { adjust: { exposure_ev: 1 } });
  out.newer = await preview.persist(v, { adjust: { exposure_ev: 2 } });
  out.statusAfterNewer = preview.saveStatus(PHOTOS.A.id);
  out.retry = await preview.retrySaves(PHOTOS.A.id);
  out.putValues = puts(PHOTOS.A.id).map((r) => r.body.adjust.exposure_ev);
  out.server = stored[PHOTOS.A.id].adjust.exposure_ev;
} else if (scenario === 'retry-after-reopen') {
  // F03：A 上的修改没写进去；切到 B 再回 A（面板读的是服务器上的旧值），点底栏的「重试」：
  // 写进去以后，面板要显示写进去的值，rev 和服务器一致，下一笔不会 409
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  await preview.persist(viewer.currentView(), { adjust: { exposure_ev: 1 } });
  await go(`#/photo/${PHOTOS.B.id}`);
  await tick();
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick(5);
  const v = viewer.currentView();
  out.reopenedExposure = v.ed.adj.exposure_ev;
  out.statusReopened = preview.saveStatus(PHOTOS.A.id);
  const line = find(document.body, (n) => n.classList && n.classList.contains('save-state'));
  buttonByText('重试', line).click();
  await tick(15);
  out.status = preview.saveStatus(PHOTOS.A.id);
  out.viewExposure = viewer.currentView().ed.adj.exposure_ev;
  out.viewRev = viewer.currentView().ed.rev;
  out.serverRev = revs.A;
  out.nextOk = await preview.persist(viewer.currentView(), { adjust: { exposure_ev: 1, contrast: 5 } });
} else if (scenario === 'reload-race') {
  // F04：重读编辑记录的请求还没全部回来时写进去一笔新修改；重读不能把 rev 和内容退回旧的
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  const v = viewer.currentView();
  holds.add('GET /api/recipes');
  const reload = preview.reloadEdit(v);
  await tick();
  out.saved = await preview.persist(v, { adjust: { exposure_ev: 2 } });
  g.release('GET /api/recipes', { units: [] });
  await reload;
  await tick(5);
  out.viewRev = v.ed.rev;
  out.serverRev = revs.A;
  out.viewExposure = v.ed.adj.exposure_ev;
  out.nextOk = await preview.persist(v, { adjust: { exposure_ev: 3 } });
  out.conflicts = requests.filter((r) => r.method === 'PUT').length - 2;
} else if (scenario === 'date-export-barrier') {
  // F02：「这一天的全部」导出前要等这一天所有改过的照片。B 的修改没写进去：
  // 从大图（A）把范围换成这一天的全部、或者在图库里导出这一天，都不能发出导出请求；只导出 A 这一张可以
  const { openExportDialog } = await import(file('export.js'));
  await go(`#/photo/${PHOTOS.B.id}`);
  await tick();
  failures.set(`PUT /api/photos/${PHOTOS.B.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  await preview.persist(viewer.currentView(), { adjust: { exposure_ev: 1 } });
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick(5);
  let form = await openExport();
  find(form, (n) => n.tagName === 'BUTTON' && n.textContent.endsWith('的全部')).click();
  buttonByText('导出', form).click();
  await tick(10);
  out.dayPostsFromViewer = requests.filter((r) => r.method === 'POST' && r.url === '/api/export').length;
  out.dayFormText = form.textContent;
  find(form, (n) => n.tagName === 'BUTTON' && n.textContent === '这一张').click();
  buttonByText('导出', form).click();
  await tick(10);
  out.singlePosts = requests.filter((r) => r.method === 'POST' && r.url === `/api/photos/${PHOTOS.A.id}/export`).length;
  const { closeTopModal } = await import(file('dom.js'));
  closeTopModal();
  await new Promise((r) => setTimeout(r, 200)); // 关掉的对话框淡出 160ms 后才移走
  await go('#/photos');
  await tick();
  openExportDialog({ ids: [], date: PHOTOS.A.date, dayLabel: '这一天的全部', photos: state.photos });
  await tick();
  const submits = [];
  const walk = (n) => { if (n.tagName === 'BUTTON' && n.textContent.trim() === '导出' && n.parentNode.classList.contains('modal-foot')) submits.push(n); n.children.forEach(walk); };
  walk(document.body);
  form = submits[submits.length - 1].parentNode.parentNode;
  submits[submits.length - 1].click();
  await tick(10);
  out.dayPostsFromGallery = requests.filter((r) => r.method === 'POST' && r.url === '/api/export').length;
  out.galleryFormText = form.textContent;
} else if (scenario === 'reset-double-click') {
  // F09：「复原」点了马上锁住；两笔写入（编辑、裁剪）还在路上时再点没有反应
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  holds.add(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  const reset = buttonByText('复原');
  reset.click();
  await tick();
  out.lockedAtOnce = reset.getAttribute('aria-disabled') === 'true';
  reset.click();
  reset.click();
  await tick();
  g.release(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  await tick(15);
  out.puts = puts(PHOTOS.A.id).map((r) => r.url.split('/').pop());
  out.unlocked = reset.getAttribute('aria-disabled') === null;
} else if (scenario === 'reset-partial-failure') {
  // F09：复原时编辑写失败、裁剪写成功：界面保留复原后的样子，底栏标成没保存，重试只补写编辑
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  buttonByText('复原').click();
  await tick(15);
  out.status = preview.saveStatus(PHOTOS.A.id);
  out.saveLine = (find(document.body, (n) => n.classList && n.classList.contains('save-state')) || { textContent: '' }).textContent;
  const before = puts(PHOTOS.A.id).length;
  out.retry = await preview.retrySaves(PHOTOS.A.id);
  out.retryPuts = puts(PHOTOS.A.id).slice(before).map((r) => r.url.split('/').pop());
  out.statusAfter = preview.saveStatus(PHOTOS.A.id);
} else if (scenario === 'before-out-of-order') {
  // F08：对比图连着请求两次（拉直 0°、5°），5° 的先回来、0° 的后回来：缓存留 5° 的，旧结果不装、不多建地址
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick(5);
  const v = viewer.currentView();
  blobManual = true;
  const start = urlCount;
  let repaints = 0;
  v.ed.geometry = { ...v.ed.geometry, straighten: 0 };
  preview.beforeUrl(v, () => repaints++);
  v.ed.geometry = { ...v.ed.geometry, straighten: 5 };
  preview.beforeUrl(v, () => repaints++);
  out.requests = blobQueue.length;
  blobQueue[1].resolve();
  await tick();
  blobQueue[0].resolve();
  await tick();
  out.key = v.ed.before.key;
  out.urlsCreated = urlCount - start;
  out.repaints = repaints;
} else if (scenario === 'partial-list') {
  // F07：图库只取了第一页就打开照片（照片多于一页）：大图要自己把列表取完，胶片条和序号用完整的列表
  state.photos = [PHOTOS.A];
  state.photosComplete = false;
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick(10);
  out.listLen = state.photos.length;
  out.complete = state.photosComplete;
  const counter = find(root, (n) => n.classList && n.classList.contains('info') && n.classList.contains('num'));
  out.title = counter ? counter.textContent : '';
  out.prevDisabled = viewer.currentView().vb.prev.getAttribute('aria-disabled');
  out.nextEnabled = viewer.currentView().vb.next.getAttribute('aria-disabled') === null;
} else if (scenario === 'info-names') {
  // F11：信息面板里 Looks 和相机里的自定义配方写它们自己的名字，不写原厂底子的名字
  const { recipeName } = await import(file('info_panel.js'));
  state.catalog = { units: [{ id: 'jlaunch', recipes: [{ slot: 4, name: '原厂标准' }], looks: [{ id: 'night', name: 'NIGHT', slot: 4 }], users: [{ id: 'u1', name: 'MY BW', slot: 4 }] }] };
  const fake = { photo: PHOTOS.A, ed: { recipe: null } };
  out.look = recipeName(fake, { kind: 'look', id: 'night', unit: 'jlaunch', slot: 4 });
  out.user = recipeName(fake, { kind: 'camera_user', id: 'u1', unit: 'jlaunch', slot: 4 });
  out.missingLook = recipeName(fake, { kind: 'look', id: 'gone', unit: 'jlaunch', slot: 4 });
  out.builtin = recipeName(fake, { unit: 'jlaunch', slot: 4 });
} else if (scenario === 'camera-import-after-leave') {
  // F05：在相机页点「从存储卡导入配方」，导入还没完成就回到图库；完成后不能把相机页画回来
  const { renderCamera } = await import(file('camera_page.js'));
  let current = true;
  holds.add('POST /api/camdata/import-card');
  renderCamera(root, () => current);
  await tick(5);
  buttonByText('从存储卡导入配方', root).click();
  await tick();
  current = false;
  root.replaceChildren(Object.assign(document.createElement('div'), { id: 'gallery-stub', text: 'GALLERY' }));
  g.release('POST /api/camdata/import-card');
  await tick(15);
  out.page = root.textContent;
  out.camdataReady = state.camdata && state.camdata.ready;
  // 还没离开的时候，导入完成照常重画相机页
  current = true;
  camStatus = { ready: false, camera_data: { state: 'missing' }, calibration: { state: 'missing' } };
  state.camdata = { ready: false, state: 'missing' };
  renderCamera(root, () => current);
  await tick(5);
  buttonByText('从存储卡导入配方', root).click();
  await tick(15);
  out.stayedPage = root.textContent.includes('配方位置一览');
  out.importedShown = root.textContent.includes('已导入');
} else if (scenario === 'modal-focus-guard') {
  // F06：导出途中按钮锁住时焦点不能跑出对话框；焦点被丢到 body 上时，Tab 把它带回对话框；背后的页面 inert
  const app = Object.assign(document.createElement('div'), { id: 'app' });
  document.body.append(app);
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  holds.add(`POST /api/photos/${PHOTOS.A.id}/export`);
  const form = await openExport();
  out.appInertOpen = app.hasAttribute('inert');
  const submit = buttonByText('导出', form);
  submit.focus();
  submit.click();
  await tick();
  out.submitDisabled = submit.hasAttribute('disabled');
  out.submitAriaDisabled = submit.getAttribute('aria-disabled');
  out.focusOnSubmit = document.activeElement === submit;
  submit.click(); // 锁住时再点：不重发
  await tick();
  out.exportPosts = requests.filter((r) => r.method === 'POST' && r.url.endsWith('/export')).length;
  document.activeElement = document.body; // 浏览器把焦点丢到 body 上
  const ev = new Event('keydown', { cancelable: true });
  ev.key = 'Tab';
  ev.shiftKey = false;
  document.dispatchEvent(ev);
  const dialog = find(document.body, (n) => n.getAttribute && n.getAttribute('role') === 'dialog');
  out.focusBackInDialog = dialog.contains(document.activeElement);
  out.tabPrevented = ev.defaultPrevented;
  g.release(`POST /api/photos/${PHOTOS.A.id}/export`);
  await tick(5);
  out.unlocked = submit.getAttribute('aria-disabled') === null;
  const { closeTopModal } = await import(file('dom.js'));
  closeTopModal();
  out.appInertClosed = app.hasAttribute('inert');
} else {
  throw new Error(`没有这个场景：${scenario}`);
}

console.log(JSON.stringify(out));
process.exit(0);
