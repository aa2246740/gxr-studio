// 复制 / 粘贴调整在编辑器里的逻辑：真正的 viewer.js、preview.js、saves.js、paste_core.js、paste_edits.js，接口用替身。
// 用法：node paste_edits.mjs <模块目录> <场景>。输出一行 JSON，断言在 tests/test_paste_ui.py。
import { pathToFileURL } from 'node:url';
import { installDom, gate, tick } from './mini_dom.mjs';

const folder = process.argv[2];
const scenario = process.argv[3];
const file = (name) => pathToFileURL(`${folder}/${name}`).href;
installDom();

const requests = [];
const g = gate();
const holds = new Set();
const revs = { A: 1, B: 5 };
const PHOTOS = {
  A: { id: 'a0', name: 'R0010001.DNG', date: '2026-10-01', captured_at: '2026-10-01T10:00:00', has_raw: true, has_jpg: true, width: 3000, height: 2000, lens: '50mm' },
  B: { id: 'b0', name: 'R0010002.DNG', date: '2026-10-01', captured_at: '2026-10-01T10:01:00', has_raw: true, has_jpg: true, width: 3000, height: 2000, lens: '50mm' },
};
const byId = (id) => Object.values(PHOTOS).find((p) => p.id === id);
const key = (id) => Object.keys(PHOTOS).find((k) => PHOTOS[k].id === id);
const RECIPE = { kind: 'builtin', unit: 'jlaunch', slot: 2, levels: {} };
const stored = { a0: { recipe: RECIPE, adjust: { exposure_ev: 0.5, wb: { warm: 4, tint: 0 }, contrast: 7, highlight: 0, shadow: 0 }, curve: { master: [[0, 0], [128, 120], [255, 255]] } }, b0: {} };
const editOf = (id) => ({ rev: revs[key(id)], recipe: null, lut: null, adjust: {}, curve: null, geometry: {}, frame: null, stamp: null, memory: {}, render: { available: true }, ...(stored[id] || {}) });
const cropOf = (id) => ({ preset: 'original', rect: [0, 0, 1, 1], angle_deg: 0, orientation: 'landscape', frame: { width: 3000, height: 2000 }, rev: revs[key(id)], ...((stored[id] || {}).cropBox || {}) });
let pasteReply = null; // 场景给的粘贴回应（没给就按来源套用）

class StubError extends Error {
  constructor(status, message, code = `http_${status}`) { super(message); this.status = status; this.code = code; this.data = { message }; }
}

function defaultPaste(body) {
  const results = body.targets.map((t) => {
    const k = key(t.id);
    if (t.expected_rev !== revs[k]) return { id: t.id, status: 'error', applied: [], skipped: [], code: 'rev_conflict', message: '这张照片已在别处修改，没有粘贴。' };
    for (const f of ['recipe', 'lut', 'adjust', 'curve', 'geometry', 'frame', 'stamp']) if (f in body.source) stored[t.id] = { ...stored[t.id], [f]: body.source[f] };
    if (body.source.recipe) stored[t.id] = { ...stored[t.id], memory: body.source.recipe.memory || {} }; // 真服务器把配方旁边的记忆存在配方里
    revs[k] += 1;
    return { id: t.id, status: 'applied', applied: body.groups, skipped: [], rev: revs[k], ...(body.include_state ? { edit: editOf(t.id), crop: cropOf(t.id) } : {}) };
  });
  return { batch_id: body.batch_id, results, counts: {} };
}

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
    if (body.expected_rev !== revs[k]) throw new StubError(409, '这张照片刚被别处改过。', 'rev_conflict');
    revs[k] += 1;
    if (m[2] === 'edit') {
      const { expected_rev: _rev, ...fields } = body;
      // 和真服务器一样（render/edits.py 的 write）：配方里带了 memory 就换成它（空的就是清掉），不带就留着原来的
      if (fields.recipe && 'memory' in fields.recipe) fields.memory = fields.recipe.memory;
      else if (fields.recipe === null) fields.memory = {};
      stored[m[1]] = { ...stored[m[1]], ...fields };
      return { ...editOf(m[1]), rev: revs[k] };
    }
    return { ...cropOf(m[1]), ...body, rev: revs[k] };
  }
  if (method === 'POST' && url === '/api/edits/paste') return pasteReply ? pasteReply(body) : defaultPaste(body);
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
  return answer(method, url, opts.body);
};
globalThis.apiBlobStub = () => Promise.resolve({ blob: {}, headers: { get: () => null } });

const { state } = await import(file('state.js'));
const viewer = await import(file('viewer.js'));
const preview = await import(file('preview.js'));
const saves = await import(file('saves.js'));
const core = await import(file('paste_core.js'));
const edits = await import(file('paste_edits.js'));
const { createRouter } = await import(file('router.js'));
state.settings = { gpu_preview: false, export_dir: '/tmp/out' };
state.prefs = { export_hdr: false };
state.photos = Object.values(PHOTOS);
state.camdata = { ready: true, state: 'ready', dataVersion: 'v1' };

const root = document.getElementById('view');
const titlebar = Object.assign(document.createElement('header'), { id: 'titlebar' });
const tbEdit = Object.assign(document.createElement('div'), { id: 'tb-edit' });
titlebar.append(tbEdit);
document.body.append(titlebar);
const find = (n, test) => (test(n) ? n : n.children.map((c) => find(c, test)).find(Boolean));
const buttonByText = (text, under = document.body) => find(under, (n) => n.tagName === 'BUTTON' && n.textContent.trim() === text);
const toasts = () => document.getElementById('toast-root').querySelectorAll('.toast-msg').map((t) => t.textContent);
const lastToast = () => toasts().at(-1) || '';
const puts = (id) => requests.filter((r) => r.method === 'PUT' && r.url.startsWith(`/api/photos/${id}/`));
const pastes = () => requests.filter((r) => r.url === '/api/edits/paste');

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

const out = {};

if (scenario === 'copy') {
  // 复制：默认只带配方和调整；toast 说明复制了什么；这张没有设置的组如实说
  await go('#/photo/a0');
  await tick();
  out.clipBefore = core.clipboard();
  const v = viewer.currentView();
  out.copied = edits.copyEdits(v);
  const clip = core.clipboard();
  out.groups = clip.groups;
  out.sourceKeys = Object.keys(clip.source).sort();
  out.exposure = clip.source.adjust.exposure_ev;
  out.recipeSlot = clip.source.recipe.slot;
  out.from = clip.from;
  out.toast = lastToast();
  out.hasChange = !!buttonByText('更改选项');
  out.titlebar = tbEdit.textContent;
  out.pasteButtonTitle = find(tbEdit, (n) => n.dataset && n.dataset.paste).title;
  out.requests = pastes().length;
}

if (scenario === 'copy-options') {
  // 复制选项：勾选记在本机；这张没有设置的组标出来，也不会进剪贴板
  await go('#/photo/a0');
  await tick();
  const v = viewer.currentView();
  edits.openCopyOptions(v);
  const boxes = () => document.body.querySelectorAll('input').filter((n) => n.dataset.group);
  out.initial = boxes().filter((b) => b.checked).map((b) => b.dataset.group);
  out.dialogText = document.getElementById('modal-root').textContent;
  boxes().find((b) => b.dataset.group === 'recipe').checked = false;
  boxes().find((b) => b.dataset.group === 'geometry').checked = true;
  buttonByText('复制').click();
  await tick();
  out.remembered = JSON.parse(localStorage.getItem('gxr-studio.paste-groups'));
  out.chosen = core.chosenGroups();
  out.groups = core.clipboard().groups;
  out.toast = lastToast();
  out.dialogClosed = document.getElementById('modal-root').children.length === 0;
  // 什么都不勾：不复制，剪贴板还是上一次的
  edits.openCopyOptions(v);
  for (const b of boxes()) b.checked = false;
  buttonByText('复制').click();
  await tick();
  out.noneToast = lastToast();
  out.clipStill = core.clipboard().groups;
}

if (scenario === 'paste-is-one-undo-step') {
  // 复制 A，在 B 上粘贴：排进保存队列、带 B 的 rev；界面换成服务器写完的内容；撤销一次还原
  await go('#/photo/a0');
  await tick();
  edits.copyEdits(viewer.currentView());
  await go('#/photo/b0');
  await tick();
  const v = viewer.currentView();
  out.undoBefore = v.undo.length;
  out.pasteBefore = await edits.pasteEdits(v);
  await tick();
  out.pastePosts = pastes().map((r) => ({ targets: r.body.targets, groups: r.body.groups, include: r.body.include_state }));
  out.adjAfter = v.ed.adj.contrast;
  out.recipeAfter = v.ed.recipe && v.ed.recipe.slot;
  out.curveAfter = v.ed.curve;
  out.undoAfter = v.undo.length;
  out.status = saves.saveStatus('b0');
  out.toast = lastToast();
  out.hasUndoAction = !!buttonByText('撤销', document.getElementById('toast-root'));
  out.recRev = saves.saveRecord('b0').rev;
  out.viewRev = v.ed.rev;
  out.putsBeforeUndo = puts('b0').length;
  preview.undoChange(v);
  await tick();
  out.adjUndone = v.ed.adj.contrast;
  out.recipeUndone = v.ed.recipe;
  const undoPuts = puts('b0');
  out.undoPut = undoPuts.length === 1 ? Object.keys(undoPuts[0].body).sort() : undoPuts.length;
  out.undoLeft = v.undo.length;
  out.redoLeft = v.redo.length;
}

if (scenario === 'paste-waits-for-saves') {
  // 当前这张有一笔保存挂着：粘贴排在它后面，带的是那一笔写完之后的 rev，不抢先
  await go('#/photo/a0');
  await tick();
  edits.copyEdits(viewer.currentView());
  await go('#/photo/b0');
  await tick();
  const v = viewer.currentView();
  holds.add('PUT /api/photos/b0/edit');
  v.ed.adj.contrast = 3;
  const first = preview.persist(v, { adjust: preview.adjustBody(v.ed) });
  await tick();
  const pasting = edits.pasteEdits(v);
  await tick();
  out.postsWhileHeld = pastes().length;
  out.statusWhileHeld = saves.saveStatus('b0');
  g.release('PUT /api/photos/b0/edit');
  await first;
  await pasting;
  await tick();
  out.pastes = pastes().map((r) => r.body.targets);
  out.order = requests.filter((r) => r.method === 'PUT' || r.url === '/api/edits/paste').map((r) => r.method + ' ' + r.url);
  out.finalRev = saves.saveRecord('b0').rev;
}

if (scenario === 'paste-refused') {
  // 没复制过 / 就是来源这张 / 这张有修改没保存：都说明白，不发请求
  await go('#/photo/b0');
  await tick();
  const v = viewer.currentView();
  await edits.pasteEdits(v);
  out.noClip = lastToast();
  await go('#/photo/a0');
  await tick();
  edits.copyEdits(viewer.currentView());
  await edits.pasteEdits(viewer.currentView());
  out.sameToast = lastToast();
  await go('#/photo/b0');
  await tick();
  const b = viewer.currentView();
  const rec = saves.saveRecord('b0');
  rec.failed = { edit: { adjust: {} }, crop: null, error: new Error('磁盘已满') };
  await edits.pasteEdits(b);
  out.failedToast = lastToast();
  out.posts = pastes().length;
}

if (scenario === 'paste-skipped-and-conflict') {
  // 服务器说有一部分没套用：界面只换套用了的，toast 说原因；冲突不动界面
  await go('#/photo/a0');
  await tick();
  edits.copyEdits(viewer.currentView());
  await go('#/photo/b0');
  await tick();
  const v = viewer.currentView();
  pasteReply = (body) => {
    stored.b0 = { ...stored.b0, adjust: body.source.adjust, curve: body.source.curve };
    revs.B += 1;
    return {
      results: [{ id: 'b0', status: 'partial', applied: ['adjust'], rev: revs.B, edit: editOf('b0'), crop: cropOf('b0'),
        skipped: [{ group: 'recipe', part: 'recipe', reason: 'needs_raw', message: '这张照片只有 JPG，没有套用配方。' }] }],
    };
  };
  await edits.pasteEdits(v);
  await tick();
  out.partialToast = lastToast();
  out.adj = v.ed.adj.contrast;
  out.recipe = v.ed.recipe;
  pasteReply = () => ({ results: [{ id: 'b0', status: 'error', applied: [], skipped: [], code: 'rev_conflict', message: '这张照片已在别处修改，没有粘贴。' }] });
  const undoLen = v.undo.length;
  await edits.pasteEdits(v);
  await tick();
  out.conflictToast = lastToast();
  out.undoUnchanged = v.undo.length === undoLen;
}

if (scenario.startsWith('undo-keeps-the-recipe-memory')) {
  // B 有自己的配方和旋钮记忆（:none 时没有记忆）。粘贴 A 的配方（带着 A 的记忆），撤销之后 B 的记忆原样，不留 A 的
  stored.a0.memory = { 'jlaunch/2': { amount: 60 } };
  stored.b0 = { recipe: { ...RECIPE, slot: 5 }, ...(scenario.endsWith(':none') ? {} : { memory: { 'jlaunch/5': { contrast: 3 } } }) };
  await go('#/photo/a0');
  await tick();
  edits.copyEdits(viewer.currentView());
  out.copiedMemory = core.clipboard().source.recipe.memory;
  await go('#/photo/b0');
  await tick();
  const v = viewer.currentView();
  out.memoryBefore = v.ed.memory;
  await edits.pasteEdits(v);
  await tick();
  out.memoryPasted = v.ed.memory;
  preview.undoChange(v);
  await tick();
  out.memoryUndone = v.ed.memory;
  const undoPut = puts('b0').at(-1).body;
  out.undoRecipe = undoPut.recipe;
  out.recipeUndone = v.ed.recipe.slot;
  // 再改配方也不会带出 A 的档位：下一次写配方时带的是 B 自己的记忆
  out.bodyNow = preview.recipeBody(v, { memory: true }).memory ?? null;
  // 离开再回来（重新读服务器）：存着的也是 B 自己的记忆
  const reads = () => requests.filter((r) => r.method === 'GET' && r.url === '/api/photos/b0/edit').length;
  const readsBefore = reads();
  await go('#/photo/a0');
  await tick();
  await go('#/photo/b0');
  await tick();
  out.reread = reads() > readsBefore;
  out.memoryReread = viewer.currentView().ed.memory;
}

if (scenario === 'crop-that-fills-the-frame') {
  // 3:2 的文件上选 3:2：框几乎占满，但比例预设要复制；「原图」整张才算没设置
  stored.a0.cropBox = { preset: '3:2', rect: [0, 0, 1, 1] };
  core.rememberGroups(['recipe', 'adjust', 'crop']);
  await go('#/photo/a0');
  await tick();
  const v = viewer.currentView();
  edits.copyEdits(v);
  out.groups = core.clipboard().groups;
  out.crop = core.clipboard().source.crop;
  out.toast = lastToast();
  v.crop = { ...v.crop, preset: 'original', rect: { x: 0, y: 0, w: 1, h: 1 } };
  out.original = core.copyFromView(v, ['crop']);
  v.crop = { ...v.crop, preset: 'original', rect: { x: 0.2, y: 0, w: 0.8, h: 1 } };
  out.shifted = core.copyFromView(v, ['crop']).copied;
}

if (scenario === 'queued-paste-after-a-failed-save') {
  // 当前这张有一笔保存挂着，粘贴排在它后面；那笔保存失败了：粘贴不发出去（不然「重试」会拿旧内容盖掉粘贴）
  await go('#/photo/a0');
  await tick();
  edits.copyEdits(viewer.currentView());
  await go('#/photo/b0');
  await tick();
  const v = viewer.currentView();
  holds.add('PUT /api/photos/b0/edit');
  v.ed.adj.contrast = 3;
  const first = preview.persist(v, { adjust: preview.adjustBody(v.ed) });
  await tick();
  const pasting = edits.pasteEdits(v);
  await tick();
  out.postsWhileHeld = pastes().length;
  g.fail('PUT /api/photos/b0/edit', new StubError(500, '磁盘已满'));
  out.saved = await first;
  await pasting;
  await tick();
  out.posts = pastes().length;
  out.toast = lastToast();
  out.status = saves.saveStatus('b0');
  out.failedKeys = Object.keys(saves.saveRecord('b0').failed.edit);
  out.undoLen = v.undo.length;
}

console.log(JSON.stringify(out));
process.exit(0);
