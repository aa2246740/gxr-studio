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
const failIf = []; // (method, url, body) → 要抛的错误或 null；抛过一次就移走（按请求内容挑哪一笔失败）
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
    // rect 为 null：像真后端一样自己算框（cropAnswer 由场景按比例给出）
    return m[2] === 'edit' ? { ...editOf(m[1]), rev: revs[k] } : { ...cropOf(m[1]), ...body, rect: body.rect || globalThis.cropAnswer?.[body.preset] || [0, 0, 1, 1], rev: revs[k] };
  }
  if (method === 'POST' && (m = /^\/api\/photos\/(\w+)\/export$/.exec(url))) return { path: '/tmp/x_gxr.jpg', width: 10, height: 10, bytes: 100, export_id: 'e1' };
  if (method === 'PUT' && url === '/api/prefs') return body;
  if (method === 'POST' && url === '/api/export') {
    const ids = (body && body.ids) || [];
    return { id: 'j1', state: 'done', progress: { done: ids.length, total: ids.length, results: ids.map((id) => ({ id, path: `/tmp/${id}_gxr.jpg` })) } };
  }
  if (method === 'GET' && url === '/api/camdata/status') return camStatus;
  if (method === 'GET' && url === '/api/settings') return { ...state.settings, library_dir: '/tmp/lib', photo_count: 2, free_bytes: 1e9, undated_policy: 'mtime' };
  if (method === 'POST' && url === '/api/library/rebuild') return { ok: true };
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
  for (let i = 0; i < failIf.length; i += 1) {
    const e = failIf[i](method, url, opts.body);
    if (e) {
      failIf.splice(i, 1);
      throw e;
    }
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
const blobCalls = [];
globalThis.apiBlobStub = (url, opts = {}) => {
  blobCalls.push(url);
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
} else if (scenario === 'scope-change-during-barrier') {
  // N01：B 的修改没写进去。在 A 上导出「这一张」，等 A 的保存时把范围换成「这一天的全部」：
  // 这一次导出的还是点导出时选的那一张；不能在只等了 A 的情况下发出整天的导出
  await go(`#/photo/${PHOTOS.B.id}`);
  await tick();
  failures.set(`PUT /api/photos/${PHOTOS.B.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  await preview.persist(viewer.currentView(), { adjust: { exposure_ev: 1 } });
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick(5);
  holds.add(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  const saving = preview.persist(viewer.currentView(), { adjust: { exposure_ev: 0.5 } });
  const form = await openExport();
  buttonByText('导出', form).click();
  await tick();
  find(form, (n) => n.tagName === 'BUTTON' && n.textContent.endsWith('的全部')).click();
  await tick();
  g.release(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  await saving;
  await tick(10);
  out.failedB = preview.saveStatus(PHOTOS.B.id);
  out.posts = requests.filter((r) => r.method === 'POST' && r.url.endsWith('/export')).map((r) => ({ url: r.url, date: (r.body && r.body.date) || null }));
} else if (scenario === 'retry-no-reread') {
  // N02：重开 A 后点「重试」，写进去以后面板直接用写入的回应更新，不再读一次（再读一次可能失败，
  // 失败时面板还是旧值、rev 却是新的，下一次调整会把补写进去的值盖掉）
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  await preview.persist(viewer.currentView(), { adjust: { exposure_ev: 1 } });
  await go(`#/photo/${PHOTOS.B.id}`);
  await tick();
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick(5);
  out.reopenedExposure = viewer.currentView().ed.adj.exposure_ev;
  holds.add(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  const retry = preview.retrySaves(PHOTOS.A.id);
  await tick();
  // 补写已经在路上；之后再读编辑记录都会失败
  const readsBefore = requests.filter((r) => r.method === 'GET' && r.url === `/api/photos/${PHOTOS.A.id}/edit`).length;
  failures.set(`GET /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '读不到编辑记录。'));
  g.release(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  out.retry = await retry;
  await tick(10);
  out.readsAfterRetry = requests.filter((r) => r.method === 'GET' && r.url === `/api/photos/${PHOTOS.A.id}/edit`).length - readsBefore;
  failures.delete(`GET /api/photos/${PHOTOS.A.id}/edit`);
  const v = viewer.currentView();
  out.viewExposure = v.ed.adj.exposure_ev;
  out.status = preview.saveStatus(PHOTOS.A.id);
  v.ed.adj.contrast = 5;
  out.next = await preview.persist(v, { adjust: preview.adjustBody(v.ed) });
  out.serverExposure = stored[PHOTOS.A.id].adjust.exposure_ev;
  out.serverContrast = stored[PHOTOS.A.id].adjust.contrast;
} else if (scenario === 'retry-keeps-newer-draft') {
  // N02：重试还在路上时又把曝光改成 2，这一笔写失败：重试完成后面板保留 2（不拿服务器上的 1 盖掉），
  // 底栏标成没保存；之后再调对比度，写进去的曝光是 2
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  const v = viewer.currentView();
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  v.ed.adj.exposure_ev = 1;
  await preview.persist(v, { adjust: preview.adjustBody(v.ed) });
  holds.add(`GET /api/photos/${PHOTOS.A.id}/edit`);
  const retry = preview.retrySaves(PHOTOS.A.id);
  await tick();
  v.ed.adj.exposure_ev = 2;
  failIf.push((method, url, body) => (method === 'PUT' && body && body.adjust && body.adjust.exposure_ev === 2 ? new StubError(500, '磁盘已满，无法写入编辑记录。') : null));
  const newer = preview.persist(v, { adjust: preview.adjustBody(v.ed) });
  g.release(`GET /api/photos/${PHOTOS.A.id}/edit`);
  out.retry = await retry;
  out.newer = await newer;
  await tick(10);
  out.viewExposure = v.ed.adj.exposure_ev;
  out.status = preview.saveStatus(PHOTOS.A.id);
  v.ed.adj.contrast = 5;
  out.next = await preview.persist(v, { adjust: preview.adjustBody(v.ed) });
  out.serverExposure = stored[PHOTOS.A.id].adjust.exposure_ev;
  out.statusAfter = preview.saveStatus(PHOTOS.A.id);
} else if (scenario === 'before-cache-return') {
  // N03：对比图 0° 已经缓存；改到 5°（请求挂着）又改回 0°：直接用缓存，晚到的 5° 不能把它换掉
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick(5);
  const v = viewer.currentView();
  blobManual = true;
  v.ed.geometry = { ...v.ed.geometry, straighten: 0 };
  preview.beforeUrl(v, () => {});
  blobQueue[0].resolve();
  await tick();
  const cached = v.ed.before.url;
  v.ed.geometry = { ...v.ed.geometry, straighten: 5 };
  preview.beforeUrl(v, () => {});
  v.ed.geometry = { ...v.ed.geometry, straighten: 0 };
  out.returnedCached = preview.beforeUrl(v, () => {}) === cached;
  blobQueue[1].resolve();
  await tick();
  out.requests = blobQueue.length;
  out.keptCached = v.ed.before.url === cached;
  out.keyIs0 = JSON.parse(v.ed.before.key)[0].straighten === 0;
} else if (scenario === 'single-close-fast-result') {
  // N04：单张导出途中关掉对话框，结果在 160ms 退场动画期间就回来了：照样用提示告诉结果（成功、失败各一次）
  const { closeTopModal } = await import(file('dom.js'));
  const toasts = () => document.getElementById('toast-root').textContent;
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  holds.add(`POST /api/photos/${PHOTOS.A.id}/export`);
  let form = await openExport();
  buttonByText('导出', form).click();
  await tick();
  closeTopModal();
  g.release(`POST /api/photos/${PHOTOS.A.id}/export`);
  await tick(5);
  await new Promise((r) => setTimeout(r, 200));
  out.okToast = toasts();
  document.getElementById('toast-root').replaceChildren();
  holds.add(`POST /api/photos/${PHOTOS.A.id}/export`);
  failures.set(`POST /api/photos/${PHOTOS.A.id}/export`, new StubError(500, '导出目录不能写。'));
  form = await openExport();
  buttonByText('导出', form).click();
  await tick();
  closeTopModal();
  g.release(`POST /api/photos/${PHOTOS.A.id}/export`);
  await tick(5);
  await new Promise((r) => setTimeout(r, 200));
  out.failToast = toasts();
} else if (scenario === 'settings-rebuild-closed-failure') {
  // N04：设置里点「重建索引」后关掉设置，重建失败：错误用带「重试」的提示告诉用户，不写进看不见的对话框；
  // 从提示里重试会再发一次重建
  const { showSettings } = await import(file('onboarding.js'));
  const { closeTopModal } = await import(file('dom.js'));
  state.settings = { ...state.settings, library_dir: '/tmp/lib', photo_count: 2, free_bytes: 1e9, undated_policy: 'mtime' };
  await showSettings();
  await tick(5);
  holds.add('POST /api/library/rebuild');
  failures.set('POST /api/library/rebuild', new StubError(500, '照片库读不出来。'));
  const dialog = find(document.body, (n) => n.getAttribute && n.getAttribute('role') === 'dialog');
  find(dialog, (n) => n.tagName === 'BUTTON' && n.textContent.trim() === '重建索引').click();
  await tick();
  closeTopModal();
  g.release('POST /api/library/rebuild');
  await tick(5);
  const root = document.getElementById('toast-root');
  out.toast = root.textContent;
  const again = buttonByText('重试', root);
  out.hasRetry = !!again;
  // 连点两下提示里的「重试」，第一下的重建还没回来（V03）：只重试一次
  holds.add('POST /api/library/rebuild');
  again.click();
  again.click();
  await tick(5);
  out.postsBeforeRelease = requests.filter((r) => r.method === 'POST' && r.url === '/api/library/rebuild').length;
  g.release('POST /api/library/rebuild');
  await tick(5);
  out.rebuildPosts = requests.filter((r) => r.method === 'POST' && r.url === '/api/library/rebuild').length;
  out.toastAfter = root.textContent;
  out.doneToasts = root.textContent.split('已重建索引').length - 1;
} else if (scenario === 'retry-other-field') {
  // V01：重开 A 后重试（曝光 1），重试排着时又只改了曲线：曲线以界面为准，补写进去的曝光照样显示；
  // 之后再调对比度，服务器上的曝光仍是 1
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  let v = viewer.currentView();
  v.ed.adj.exposure_ev = 1;
  await preview.persist(v, { adjust: preview.adjustBody(v.ed) });
  await go(`#/photo/${PHOTOS.B.id}`);
  await tick();
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick(5);
  v = viewer.currentView();
  out.reopenedExposure = v.ed.adj.exposure_ev;
  holds.add(`GET /api/photos/${PHOTOS.A.id}/edit`);
  const retry = preview.retrySaves(PHOTOS.A.id);
  await tick();
  const curve = [[0, 0], [128, 110], [255, 255]];
  v.ed.curve = curve.map((q) => [...q]);
  const newer = preview.persist(v, { curve: { master: curve } });
  g.release(`GET /api/photos/${PHOTOS.A.id}/edit`);
  out.retry = await retry;
  out.newer = await newer;
  await tick(10);
  out.viewExposure = v.ed.adj.exposure_ev;
  out.viewCurve = v.ed.curve;
  out.status = preview.saveStatus(PHOTOS.A.id);
  v.ed.adj.contrast = 5;
  out.next = await preview.persist(v, { adjust: preview.adjustBody(v.ed) });
  out.serverExposure = stored[PHOTOS.A.id].adjust.exposure_ev;
  out.serverCurve = stored[PHOTOS.A.id].curve;
} else if (scenario === 'retry-partial-two-kinds') {
  // V02：编辑和裁剪都没写进去；重开 A 后第一次重试编辑成功、裁剪又失败：写进去的编辑马上显示；
  // 第二次重试补上裁剪；之后再调对比度，服务器上的曝光仍是 1
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  let v = viewer.currentView();
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  v.ed.adj.exposure_ev = 1;
  await preview.persist(v, { adjust: preview.adjustBody(v.ed) });
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/crop`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  await preview.saveCrop(v, { preset: 'free', rect: [0.1, 0.1, 0.8, 0.8] });
  await go(`#/photo/${PHOTOS.B.id}`);
  await tick();
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick(5);
  v = viewer.currentView();
  out.reopenedExposure = v.ed.adj.exposure_ev;
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/crop`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  out.firstRetry = await preview.retrySaves(PHOTOS.A.id);
  await tick(5);
  out.afterPartial = { view: v.ed.adj.exposure_ev, server: stored[PHOTOS.A.id].adjust.exposure_ev, status: preview.saveStatus(PHOTOS.A.id) };
  out.secondRetry = await preview.retrySaves(PHOTOS.A.id);
  await tick(5);
  out.after = { view: v.ed.adj.exposure_ev, status: preview.saveStatus(PHOTOS.A.id), rect: v.crop.rect };
  v.ed.adj.contrast = 5;
  out.next = await preview.persist(v, { adjust: preview.adjustBody(v.ed) });
  out.serverAfterNext = stored[PHOTOS.A.id].adjust.exposure_ev;
} else if (scenario === 'batch-close-fast') {
  // V04：批量导出途中关掉对话框，结果在 160ms 退场期间回来：照样用提示告诉结果
  const { openExportDialog } = await import(file('export.js'));
  const { closeTopModal } = await import(file('dom.js'));
  await go('#/photos');
  await tick();
  holds.add('POST /api/export');
  openExportDialog({ ids: [PHOTOS.A.id, PHOTOS.B.id], label: '选中的 2 张', photos: state.photos });
  await tick();
  const submits = [];
  const walk = (n) => { if (n.tagName === 'BUTTON' && n.textContent.trim() === '导出' && n.parentNode.classList.contains('modal-foot')) submits.push(n); n.children.forEach(walk); };
  walk(document.body);
  submits[submits.length - 1].click();
  await tick();
  closeTopModal();
  g.release('POST /api/export');
  await tick(5);
  await new Promise((r) => setTimeout(r, 200));
  out.toast = document.getElementById('toast-root').textContent;
} else if (scenario === 'closed-save-retry') {
  // V05：B 的修改没写进去（当时的提示已经消失）；导出 A、B 时 A 还在保存，关掉对话框后关卡才查到 B：
  // 提示里要有「重试保存」，点了会补写 B；不发出导出请求
  const { openExportDialog } = await import(file('export.js'));
  const { closeTopModal } = await import(file('dom.js'));
  await go(`#/photo/${PHOTOS.B.id}`);
  await tick();
  failures.set(`PUT /api/photos/${PHOTOS.B.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  await preview.persist(viewer.currentView(), { adjust: { exposure_ev: 1 } });
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick(5);
  holds.add(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  const saving = preview.persist(viewer.currentView(), { adjust: { exposure_ev: 0.5 } });
  await go('#/photos');
  await tick();
  const root = document.getElementById('toast-root');
  root.replaceChildren(); // 之前的提示已经消失
  openExportDialog({ ids: [PHOTOS.A.id, PHOTOS.B.id], label: '选中的 2 张', photos: state.photos });
  await tick();
  const submits = [];
  const walk = (n) => { if (n.tagName === 'BUTTON' && n.textContent.trim() === '导出' && n.parentNode.classList.contains('modal-foot')) submits.push(n); n.children.forEach(walk); };
  walk(document.body);
  submits[submits.length - 1].click();
  await tick();
  closeTopModal();
  g.release(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  await saving;
  await tick(5);
  await new Promise((r) => setTimeout(r, 200));
  out.toast = root.textContent;
  const retry = buttonByText('重试保存', root);
  out.hasRetrySave = !!retry;
  if (retry) retry.click();
  await tick(10);
  out.statusB = preview.saveStatus(PHOTOS.B.id);
  out.toastAfter = root.textContent;
  out.posts = requests.filter((r) => r.method === 'POST' && r.url.endsWith('/export')).length;
} else if (scenario === 'retry-during-gesture') {
  // W01：重开 A 后重试；补写编辑写进去时你正在拖曲线（beginChange 之后）：面板不能被重建（拖着的控件和松手时的保存还要用）；
  // 松手、曲线写进去、这一步结束后面板再刷新，面板和服务器上的曝光都是 1
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  let v = viewer.currentView();
  v.ed.adj.exposure_ev = 1;
  await preview.persist(v, { adjust: preview.adjustBody(v.ed) });
  await go(`#/photo/${PHOTOS.B.id}`);
  await tick();
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick(5);
  v = viewer.currentView();
  viewer.openViewerTab('adjust');
  await tick();
  const pane = () => v.panes[v.tab].el.firstChild;
  out.tab = v.tab;
  const before = pane();
  out.hadPane = !!before;
  holds.add(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  const retry = preview.retrySaves(PHOTOS.A.id);
  await tick();
  preview.beginChange(v); // 按下曲线
  const curve = [[0, 0], [133, 148], [255, 255]];
  v.ed.curve = curve.map((q) => [...q]);
  g.release(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  out.retry = await retry;
  await tick(5);
  out.sameDuringGesture = pane() === before;
  out.exposureDuringGesture = v.ed.adj.exposure_ev;
  await preview.persist(v, { curve: preview.curveBody(v.ed) }).then(() => preview.endChange(v)); // 松手
  await tick(5);
  out.rebuiltAfter = pane() !== before;
  out.serverCurve = stored[PHOTOS.A.id].curve;
  out.serverExposure = stored[PHOTOS.A.id].adjust.exposure_ev;
  out.status = preview.saveStatus(PHOTOS.A.id);
} else if (scenario === 'retry-undo-cross-field') {
  // W02：重开 A（曝光显示成旧的 0）后重试，重试排着时改了曲线；重试写进曝光 1 以后撤销曲线：
  // 只撤掉曲线，曝光仍是 1；重做把曲线放回来
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  let v = viewer.currentView();
  v.ed.adj.exposure_ev = 1;
  await preview.persist(v, { adjust: preview.adjustBody(v.ed) });
  await go(`#/photo/${PHOTOS.B.id}`);
  await tick();
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick(5);
  v = viewer.currentView();
  holds.add(`GET /api/photos/${PHOTOS.A.id}/edit`);
  const retry = preview.retrySaves(PHOTOS.A.id);
  await tick();
  preview.beginChange(v);
  const curve = [[0, 0], [128, 110], [255, 255]];
  v.ed.curve = curve.map((q) => [...q]);
  const curveSave = preview.persist(v, { curve: preview.curveBody(v.ed) }).then(() => preview.endChange(v));
  g.release(`GET /api/photos/${PHOTOS.A.id}/edit`);
  out.retry = await retry;
  await curveSave;
  await tick(5);
  out.beforeUndo = { view: v.ed.adj.exposure_ev, server: stored[PHOTOS.A.id].adjust.exposure_ev };
  preview.undoChange(v);
  await preview.flushEdits(v);
  await tick(5);
  out.afterUndo = { view: v.ed.adj.exposure_ev, server: stored[PHOTOS.A.id].adjust.exposure_ev, curve: stored[PHOTOS.A.id].curve, viewCurve: v.ed.curve };
  preview.redoChange(v);
  await preview.flushEdits(v);
  await tick(5);
  out.afterRedo = { view: v.ed.adj.exposure_ev, server: stored[PHOTOS.A.id].adjust.exposure_ev, curve: stored[PHOTOS.A.id].curve };
} else if (scenario === 'retry-undo-own-history') {
  // W02 的另一面：没有切走，曝光 0→1 是你自己改的（写失败后重试成功）：撤销照样回到 0
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  const v = viewer.currentView();
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  preview.beginChange(v);
  v.ed.adj.exposure_ev = 1;
  await preview.persist(v, { adjust: preview.adjustBody(v.ed) }).then(() => preview.endChange(v));
  out.retry = await preview.retrySaves(PHOTOS.A.id);
  await tick(5);
  preview.undoChange(v);
  await preview.flushEdits(v);
  await tick(5);
  out.afterUndo = { view: v.ed.adj.exposure_ev, server: stored[PHOTOS.A.id].adjust.exposure_ev };
} else if (scenario === 'no-op-slider') {
  // X01：重开 A（曝光显示成旧的 0）后点重试；补写还没回来时在真的调整面板上按下又松开曝光滑块、值没变
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  let v = viewer.currentView();
  v.ed.adj.exposure_ev = 1;
  await preview.persist(v, { adjust: preview.adjustBody(v.ed) });
  await go(`#/photo/${PHOTOS.B.id}`);
  await tick();
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick(5);
  v = viewer.currentView();
  viewer.openViewerTab('adjust');
  await tick();
  const exposureInput = () => {
    let found = null;
    const walk = (n) => { if (!found && n.tagName === 'INPUT' && n.getAttribute && n.getAttribute('aria-label') === '曝光') found = n; (n.children || []).forEach(walk); };
    walk(v.panes.adjust.el);
    return found;
  };
  const input = exposureInput();
  out.sliderFound = !!input;
  holds.add(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  const retry = preview.retrySaves(PHOTOS.A.id);
  await tick();
  input.dispatchEvent(new Event('pointerdown'));
  input.dispatchEvent(new Event('pointerup'));
  out.pendingAfterRelease = !!v.pending;
  g.release(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  out.retry = await retry;
  await preview.flushEdits(v);
  await tick(5);
  out.pending = !!v.pending;
  out.panesStale = !!v.panesStale;
  out.sliderValue = String(exposureInput().value);
  out.modelExposure = v.ed.adj.exposure_ev;
  out.undoLength = v.undo.length;
  out.status = preview.saveStatus(PHOTOS.A.id);
} else if (scenario === 'retry-owned-history') {
  // X02：A 的曝光 0→2→0 都写进去，→1 写失败；点重试、补写回来之前又开始拖曝光到 0（没松手）。
  // 曝光是在这里改过的字段：撤销记录 [0, 2, 0] 不能被改；松手写 0 后撤销两次是 1、0
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  const v = viewer.currentView();
  const step = async (ev) => {
    preview.beginChange(v);
    v.ed.adj.exposure_ev = ev;
    await preview.persist(v, { adjust: preview.adjustBody(v.ed) }).then(() => preview.endChange(v));
  };
  const undoExposures = () => v.undo.map((s) => JSON.parse(s).adj.exposure_ev);
  await step(2);
  await step(0);
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  await step(1);
  out.undoBefore = undoExposures();
  holds.add(`GET /api/photos/${PHOTOS.A.id}/edit`);
  const retry = preview.retrySaves(PHOTOS.A.id);
  await tick();
  preview.beginChange(v);
  v.ed.adj.exposure_ev = 0;
  g.release(`GET /api/photos/${PHOTOS.A.id}/edit`);
  out.retry = await retry;
  await tick(5);
  out.undoAfterRetry = undoExposures();
  v.ed.adj.exposure_ev = 0; // 松手时滑块的值
  await preview.persist(v, { adjust: preview.adjustBody(v.ed) }).then(() => preview.endChange(v));
  out.undos = [];
  for (let i = 0; i < 2; i += 1) {
    preview.undoChange(v);
    await preview.flushEdits(v);
    await tick(5);
    out.undos.push({ view: v.ed.adj.exposure_ev, server: stored[PHOTOS.A.id].adjust.exposure_ev });
  }
} else if (scenario === 'refresh-mixed-history' || scenario === 'refresh-owned-history') {
  // Y01：曝光的步骤（mixed：0→2→0 写进去、→1 失败；owned：只有 0→1 失败）之后原地刷新（换相机数据时的 refreshViewerPhoto），
  // 读回服务器上的旧值，失败的写入和撤销记录都还在。mixed 再改一笔曲线；然后重试补写曝光 1。
  // mixed：撤销曲线只撤曲线，曝光仍是 1。owned：撤销曝光回到你原来的 0
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  const v = viewer.currentView();
  const step = async (patch, body) => {
    preview.beginChange(v);
    patch();
    await preview.persist(v, body()).then(() => preview.endChange(v));
  };
  const exposure = (ev) => step(() => { v.ed.adj.exposure_ev = ev; }, () => ({ adjust: preview.adjustBody(v.ed) }));
  const mixed = scenario === 'refresh-mixed-history';
  if (mixed) {
    await exposure(2);
    await exposure(0);
  }
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  await exposure(1);
  failures.delete(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  await viewer.refreshViewerPhoto();
  await tick(5);
  out.afterRefresh = v.ed.adj.exposure_ev;
  const curve = [[0, 0], [133, 148], [255, 255]];
  if (mixed) await step(() => { v.ed.curve = curve.map((q) => [...q]); }, () => ({ curve: preview.curveBody(v.ed) }));
  out.retry = await preview.retrySaves(PHOTOS.A.id);
  await tick(5);
  out.afterRetry = { view: v.ed.adj.exposure_ev, server: stored[PHOTOS.A.id].adjust.exposure_ev };
  preview.undoChange(v);
  await preview.flushEdits(v);
  await tick(5);
  out.afterUndo = { view: v.ed.adj.exposure_ev, server: stored[PHOTOS.A.id].adjust.exposure_ev, curve: stored[PHOTOS.A.id].curve ?? null };
  preview.redoChange(v);
  await preview.flushEdits(v);
  await tick(5);
  out.afterRedo = { view: v.ed.adj.exposure_ev, server: stored[PHOTOS.A.id].adjust.exposure_ev, curve: stored[PHOTOS.A.id].curve ?? null };
} else if (scenario === 'drag-back-to-start') {
  // Y02：重开 A（显示旧的 0）后重试；补写回来之前在真的曝光滑块上拖到 5，补写回来后又拖回 0 松手：
  // 浏览器不发 change，但拖动过，松手时的 0 照样保存（松手时的值算数）
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  failures.set(`PUT /api/photos/${PHOTOS.A.id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  let v = viewer.currentView();
  v.ed.adj.exposure_ev = 1;
  await preview.persist(v, { adjust: preview.adjustBody(v.ed) });
  await go(`#/photo/${PHOTOS.B.id}`);
  await tick();
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick(5);
  v = viewer.currentView();
  viewer.openViewerTab('adjust');
  await tick();
  let input = null;
  const walk = (n) => { if (!input && n.tagName === 'INPUT' && n.getAttribute && n.getAttribute('aria-label') === '曝光') input = n; (n.children || []).forEach(walk); };
  walk(v.panes.adjust.el);
  holds.add(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  const retry = preview.retrySaves(PHOTOS.A.id);
  await tick();
  const start = input.value;
  input.dispatchEvent(new Event('pointerdown'));
  input.value = '5';
  input.dispatchEvent(new Event('input'));
  g.release(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  out.retry = await retry;
  await tick(5);
  out.serverMid = stored[PHOTOS.A.id].adjust.exposure_ev;
  input.value = start; // 拖回起点：值和按下时完全一样，浏览器不发 change
  input.dispatchEvent(new Event('input'));
  input.dispatchEvent(new Event('pointerup'));
  input.dispatchEvent(new Event('blur')); // 之后离开也不会再存一次
  await preview.flushEdits(v);
  await tick(5);
  out.final = { view: v.ed.adj.exposure_ev, server: stored[PHOTOS.A.id].adjust.exposure_ev, pending: !!v.pending, status: preview.saveStatus(PHOTOS.A.id) };
  out.edits = requests.filter((r) => r.method === 'PUT' && r.url === `/api/photos/${PHOTOS.A.id}/edit`).length;
} else if (scenario === 'ruler-blur') {
  // Y02：在拉直刻度上按住右方向键，焦点离开（keyup 发到了别处）：照样保存，这一步结束
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  const v = viewer.currentView();
  const { correctSection } = await import(file('correct.js'));
  const section = correctSection(v);
  document.body.append(section);
  out.v2 = !!v.ed.v2;
  let ruler = null;
  const walk = (n) => { if (!ruler && n.getAttribute && n.getAttribute('aria-label') === '拉直') ruler = n; (n.children || []).forEach(walk); };
  walk(section);
  out.found = !!ruler;
  const key = (type, k) => { const e = new Event(type); e.key = k; e.repeat = false; e.preventDefault = () => {}; ruler.dispatchEvent(e); };
  key('keydown', 'ArrowRight');
  ruler.dispatchEvent(new Event('blur'));
  await preview.flushEdits(v);
  await tick(5);
  ruler.dispatchEvent(new Event('blur')); // 再离开一次不会再存
  await preview.flushEdits(v);
  await tick(5);
  out.pending = !!v.pending;
  out.straighten = (stored[PHOTOS.A.id].geometry || {}).straighten ?? null;
  out.geometryWrites = requests.filter((r) => r.method === 'PUT' && r.url === `/api/photos/${PHOTOS.A.id}/edit` && r.body && r.body.geometry).length;
} else if (scenario === 'decoration-undo-other-group' || scenario === 'decoration-undo-during-retry') {
  // Z01：打开印记的那一笔失败，原地刷新后选白边并保存，然后只撤销边框。
  // 撤销只写 frame（不带 stamp 键），印记那笔失败的修改还在，重试能补写回来。
  // during-retry：重试先发出去、补写被压住时撤销边框，补写回来后不能被撤销冲掉
  const id = PHOTOS.A.id;
  const edits = () => requests.filter((r) => r.method === 'PUT' && r.url === `/api/photos/${id}/edit`);
  await go(`#/photo/${id}`);
  await tick();
  const v = viewer.currentView();
  const step = async (patch, body) => {
    preview.beginChange(v);
    patch();
    await preview.persist(v, body()).then(() => preview.endChange(v));
  };
  failures.set(`PUT /api/photos/${id}/edit`, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  await step(() => { v.ed.stamp = { ...v.ed.stamp, enabled: true }; v.ed.stampFrom = 'photo'; }, () => ({ stamp: v.ed.stamp }));
  await viewer.refreshViewerPhoto();
  await tick(5);
  out.stampAfterRefresh = !!(v.ed.stamp && v.ed.stamp.enabled);
  await step(() => { v.ed.frame = { ...v.ed.frame, style: 'white' }; v.ed.frameFrom = 'photo'; }, () => ({ frame: { style: 'white' } }));
  out.frameSaved = stored[id].frame;
  let retry = null;
  if (scenario === 'decoration-undo-during-retry') {
    holds.add(`PUT /api/photos/${id}/edit`);
    retry = preview.retrySaves(id);
    await tick();
  }
  const before = edits().length;
  preview.undoChange(v);
  if (retry) {
    await tick();
    g.release(`PUT /api/photos/${id}/edit`);
  }
  await preview.flushEdits(v).catch(() => {}); // 印记还没补写时会报「有修改没有保存」，这是对的
  await tick(5);
  out.undoKeys = edits().slice(before).map((r) => Object.keys(r.body).filter((k) => k !== 'expected_rev').sort());
  out.statusAfterUndo = preview.saveStatus(id);
  out.retry = retry ? await retry : await preview.retrySaves(id);
  await preview.flushEdits(v);
  await tick(5);
  out.final = { frame: stored[id].frame ?? null, stamp: !!(stored[id].stamp && stored[id].stamp.enabled), status: preview.saveStatus(id) };
} else if (scenario === 'legacy-angle-undo') {
  // Z02：旧接口（没有 /api/frames）的拉直角度存在裁剪里。只拉直 5° 的一步，撤销回到 0°、重做回到 5°，都要写回去
  failures.set('GET /api/frames', new StubError(404, 'Not Found'));
  const id = PHOTOS.A.id;
  await go(`#/photo/${id}`);
  await tick(5);
  const v = viewer.currentView();
  out.v2 = !!v.ed.v2;
  const crops = () => requests.filter((r) => r.method === 'PUT' && r.url === `/api/photos/${id}/crop`).map((r) => r.body);
  v.canvas = { w: 3000, h: 2000 };
  v.crop.rect = { x: 0.1, y: 0.2, w: 0.5, h: 0.4 };
  v.crop.preset = 'free';
  const { nudgeStraighten } = await import(file('correct.js'));
  out.before = v.crop.angle_deg;
  nudgeStraighten(v, 5);
  await preview.flushEdits(v);
  await tick(5);
  out.afterAction = v.crop.angle_deg;
  out.actionBodies = crops().map((b) => ({ angle_deg: b.angle_deg, rect: b.rect, preset: b.preset }));
  const n = crops().length;
  preview.undoChange(v);
  await preview.flushEdits(v);
  await tick(5);
  out.afterUndo = { angle: v.crop.angle_deg, bodies: crops().slice(n).map((b) => ({ angle_deg: b.angle_deg, rect: b.rect, preset: b.preset })) };
  const m = crops().length;
  preview.redoChange(v);
  await preview.flushEdits(v);
  await tick(5);
  out.afterRedo = { angle: v.crop.angle_deg, bodies: crops().slice(m).map((b) => ({ angle_deg: b.angle_deg, rect: b.rect, preset: b.preset })) };
} else if (scenario === 'arrow-burst') {
  // B1：在 A 上按 →（去 B，B 的编辑记录被压住、还在加载），紧接着按 ←：要回到 A，不能被吃掉
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  const key = (k) => { const keys = router.keys(); return keys ? keys.down({ key: k, preventDefault() {} }) : null; };
  // 真浏览器里 hashchange 是异步的：→ 改了地址栏、页面还是 A 时又来一个键。← 要从 B 算回 A；撤销不能作用在 A 上
  out.preRight = key('ArrowRight');
  out.preHash = location.hash;
  out.preShown = shown();
  out.preUndo = router.keys().undo();
  out.preSpace = key(' ');
  out.preLeft = key('ArrowLeft');
  out.preHashAfterLeft = location.hash;
  await router.route();
  await tick();
  holds.add(`GET /api/photos/${PHOTOS.B.id}/edit`);
  out.right = key('ArrowRight');
  out.hashAfterRight = location.hash;
  const loading = router.route();
  await tick();
  out.shownWhileLoading = shown(); // 图库里有这张的信息：页面马上换成 B，编辑记录还在读
  out.keysWhileLoading = !!router.keys();
  out.left = key('ArrowLeft');
  out.hashAfterLeft = location.hash;
  const back = router.route();
  g.release(`GET /api/photos/${PHOTOS.B.id}/edit`);
  await loading;
  await back;
  await tick(5);
  out.final = { hash: location.hash, shown: shown() };
} else if (scenario === 'toolbar-redo') {
  // B2：顶栏有重做按钮：没东西可重做时置灰；撤销后可用，点了把那一步放回来；新的改动后又置灰
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  const v = viewer.currentView();
  const redoBtn = find(tbEdit, (n) => n.tagName === 'BUTTON' && String(n.getAttribute('title') || '').startsWith('重做'));
  const undoBtn = find(tbEdit, (n) => n.tagName === 'BUTTON' && String(n.getAttribute('title') || '').startsWith('撤销'));
  const off = (b) => b.getAttribute('aria-disabled') === 'true';
  out.found = !!redoBtn && !!undoBtn;
  if (!out.found) {
    console.log(JSON.stringify(out));
    process.exit(0);
  }
  out.initial = { undo: off(undoBtn), redo: off(redoBtn) };
  preview.beginChange(v);
  v.ed.adj.exposure_ev = 1;
  await preview.persist(v, { adjust: preview.adjustBody(v.ed) }).then(() => preview.endChange(v));
  out.afterEdit = { undo: off(undoBtn), redo: off(redoBtn) };
  undoBtn.click();
  await preview.flushEdits(v);
  await tick(5);
  out.afterUndo = { undo: off(undoBtn), redo: off(redoBtn), value: v.ed.adj.exposure_ev, server: stored[PHOTOS.A.id].adjust.exposure_ev };
  redoBtn.click();
  await preview.flushEdits(v);
  await tick(5);
  out.afterRedo = { undo: off(undoBtn), redo: off(redoBtn), value: v.ed.adj.exposure_ev, server: stored[PHOTOS.A.id].adjust.exposure_ev };
  undoBtn.click();
  await preview.flushEdits(v);
  preview.beginChange(v);
  v.ed.adj.exposure_ev = 2;
  await preview.persist(v, { adjust: preview.adjustBody(v.ed) }).then(() => preview.endChange(v));
  out.afterNewEdit = { redo: off(redoBtn) };
} else if (scenario === 'export-close-label') {
  // 导出开始以后，对话框左边的按钮叫「关闭」（关掉不会停下导出），开始之前叫「取消」
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  const form = await openExport();
  const foot = find(form, (n) => n.classList && n.classList.contains('modal-foot'));
  const left = () => foot.children[0].textContent.trim();
  out.before = left();
  buttonByText('导出', form).click();
  await tick(10);
  out.after = left();
  out.exported = requests.filter((r) => r.method === 'POST' && r.url.endsWith('/export')).length;
} else if (scenario === 'frame-thumb-cache') {
  // AA01：同一张照片、同一版本再搭一次边框面板，缩略图从缓存里出：卡片要有图、没有骨架，也不再发请求
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  const v = viewer.currentView();
  const { framePanel } = await import(file('frame_panel.js'));
  v.ed.v2 = true;
  v.ed.frames = { styles: [{ id: 'none', options: [] }, { id: 'gallery', options: ['color'] }] };
  const renders = () => blobCalls.filter((u) => u.includes('/render') && u.includes('size=card')).length;
  const thumbs = (pane) => {
    const list = [];
    const walk = (n) => { if (n.classList && n.classList.contains('thumb')) list.push(n); n.children.forEach(walk); };
    walk(pane);
    return list.map((t) => ({ skel: t.classList.contains('skel'), busy: t.getAttribute('aria-busy'), img: !!t.children.find((c) => c.tagName === 'IMG') }));
  };
  const host = document.createElement('div');
  document.body.append(host);
  const first = framePanel(v);
  host.replaceChildren(first);
  await tick(10);
  out.first = thumbs(first);
  const before = renders();
  const second = framePanel(v);
  host.replaceChildren(second);
  await tick(10);
  out.second = thumbs(second);
  out.extraRenders = renders() - before;
} else if (scenario === 'crop-click-local-first') {
  // 点比例：框先在本地算好、马上变；后端慢（保存挂着）也一样。回应回来换成后端算的准框；连点时过时的回应不盖掉新的框。
  // 保存失败：框留着、标成未保存，重试补上。撤销只还原裁切这一组。
  const id = PHOTOS.A.id;
  await go(`#/photo/${id}`);
  await tick(5);
  const v = viewer.currentView();
  const cropMod = await import(file('crop.js'));
  v.canvas = { w: 3000, h: 2000 };
  let drawn = 0;
  let side = 0;
  v.onRectDrag = () => { drawn += 1; };
  v.redrawSide = () => { side += 1; };
  globalThis.cropAnswer = {
    '1:1': [500 / 3000, 0, 2000 / 3000, 1],
    '16:9': [0, 156 / 2000, 1, 1688 / 2000],
    '4:5': [700 / 3000, 0, 1600 / 3000, 1],
    'xpan-65-24': [0, 446 / 2000, 1, 1108 / 2000],
  };
  const key = `PUT /api/photos/${id}/crop`;
  const snap = () => ({ rect: { ...v.crop.rect }, preset: v.crop.preset, orientation: v.crop.orientation });
  const cropPuts = () => requests.filter((r) => r.method === 'PUT' && r.url === `/api/photos/${id}/crop`).map((r) => r.body);

  // 1) 保存挂着：点完马上就是新框
  holds.add(key);
  const first = cropMod.applyPreset(v, { id: '1:1', label: '1:1' });
  out.right = snap();
  out.redraws = { box: drawn, side };
  await tick(3);
  out.heldBody = cropPuts().map((b) => ({ preset: b.preset, rect: b.rect, orientation: b.orientation }));
  out.stillLocal = snap();
  g.release(key);
  await first;
  await tick(3);
  out.afterServer = snap();

  // 2) 连点两下：第一笔的回应晚到，不能把第二笔的框换回去
  holds.add(key);
  const second = cropMod.applyPreset(v, { id: '16:9', label: '16:9' });
  const third = cropMod.applyPreset(v, { id: '4:5', label: '4:5', orient: 'portrait' });
  out.double = snap();
  await tick(3);
  holds.add(key); // 保存是一笔一笔排队的：第二笔在第一笔写完以后才发出，也让它挂着
  g.release(key);
  await tick(5);
  out.afterStale = snap();
  g.release(key);
  await Promise.all([second, third]);
  await tick(3);
  out.afterBoth = snap();

  // 3) 撤销：只写回裁切，不碰编辑；还原到这一步之前的框
  const before = requests.length;
  preview.undoChange(v);
  await preview.flushEdits(v);
  await tick(5);
  const after = requests.slice(before).filter((r) => r.method === 'PUT');
  out.undo = { urls: after.map((r) => r.url.split('/').pop()), body: after.map((r) => ({ preset: r.body.preset, rect: r.body.rect }))[0], now: snap() };

  // 4) 保存失败：框留着，标成未保存；重试补上，框换成后端算的
  failures.set(key, new StubError(500, '磁盘已满，无法写入编辑记录。'));
  await cropMod.applyPreset(v, { id: 'xpan-65-24', label: 'XPan 65:24' });
  await tick(3);
  out.failed = { status: preview.saveStatus(id), ...snap() };
  out.retry = await preview.retrySaves(id);
  await tick(5);
  out.retried = { status: preview.saveStatus(id), ...snap() };
} else if (scenario === 'frame-cards-yield') {
  // 边框卡片是次要的：请求用 size=card；面板重搭时同一张不重复请求；这张照片有保存在路上时先让路
  await go(`#/photo/${PHOTOS.A.id}`);
  await tick();
  const v = viewer.currentView();
  const { framePanel } = await import(file('frame_panel.js'));
  v.ed.v2 = true;
  v.ed.frames = { styles: [{ id: 'none', options: [] }, { id: 'gallery', options: ['color'] }, { id: 'film', options: [] }] };
  const renders = () => blobCalls.filter((u) => u.includes('/render') && /size=(card|thumb)/.test(u));
  const host = document.createElement('div');
  document.body.append(host);
  // 保存还没写完：卡片先不请求
  holds.add(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  const saving = preview.persist(v, { adjust: preview.adjustBody(v.ed) });
  await tick(2);
  out.statusWhileSaving = preview.saveStatus(PHOTOS.A.id);
  blobManual = true;
  // 面板搭了两次（任何一次重画都会这样），都在保存还没写完的时候：同一张图只请求一次
  const first = framePanel(v);
  host.replaceChildren(first);
  const second = framePanel(v);
  host.replaceChildren(second);
  await tick(10);
  out.requestsWhileSaving = renders().length;
  g.release(`PUT /api/photos/${PHOTOS.A.id}/edit`);
  await saving;
  await new Promise((r) => setTimeout(r, 250));
  out.afterSave = { requests: renders().length, queued: blobQueue.length };
  while (blobQueue.length) blobQueue.shift().resolve();
  await tick(10);
  while (blobQueue.length) blobQueue.shift().resolve();
  await tick(10);
  const imgs = (pane) => {
    let n = 0;
    const walk = (x) => { if (x.tagName === 'IMG') n += 1; x.children.forEach(walk); };
    walk(pane);
    return n;
  };
  out.final = { requests: renders().length, firstImgs: imgs(first), secondImgs: imgs(second), urls: [...new Set(renders().map((u) => u.split('?')[1]))] };
} else {
  throw new Error(`没有这个场景：${scenario}`);
}

console.log(JSON.stringify(out));
process.exit(0);
