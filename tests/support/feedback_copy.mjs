// 反馈和用词的前端检查：导入结果的说法、相机页的导入反馈、「未校准」的原因、导出文件名预览、
// 编辑器配方标签里两种「我的配方」。接口用 fetch 替身；DOM 用 mini_dom。
// 用法：node feedback_copy.mjs <web/assets 目录> <场景>。输出一行 JSON，断言在 tests/test_feedback_copy.py。
import { pathToFileURL } from 'node:url';
import { installDom, tick } from './mini_dom.mjs';

const dir = process.argv[2];
const scenario = process.argv[3];
const file = (name) => pathToFileURL(`${dir}/${name}`).href;

installDom();
const walk = (n, acc = []) => { acc.push(n); for (const c of n.children || []) walk(c, acc); return acc; };
const find = (n, test) => walk(n).find(test);
const buttonByText = (text, under = document.body) => find(under, (n) => n.tagName === 'BUTTON' && n.textContent.trim() === text);
// 配方卡片：按名字找（卡片里除了名字还有说明）
const cardByName = (name, under = document.body) => find(under, (n) => n.tagName === 'BUTTON' && n.classList.contains('lcard') && walk(n).some((c) => c.classList && c.classList.contains('nm') && c.textContent === name));
const out = {};

const CARD = { id: 'C'.repeat(26), label: 'NO NAME', uuid_state: 'missing' };

function coverage(extra = {}) {
  return { complete: true, dcim: 'present', total: 19, covered: 19, uncovered: [], not_importable: [], system_files: [],
    all_imported: true, photos_imported: true, ...extra };
}
function progress(extra = {}) {
  return { state: 'done', card_id: CARD.id, imported_shots: 0, completed_shots: 0, imported_videos: 0, seen_shots: 0, duplicate_shots: 19,
    failed_shots: 0, done_shots: 19, total_shots: 19, unreadable: [], verify_failed: [], scan_errors: [], write_refused: [],
    coverage: coverage(), message: '', ...extra };
}

if (scenario === 'import-phases') {
  const { state, on } = await import(file('state.js'));
  const I = await import(file('import.js'));
  for (const id of ['banner', 'card-status', 'card-chip', 'card-icon']) {
    const el=document.createElement('div'); el.id=id; document.body.append(el);
  }
  state.card = CARD;
  state.job = { id:'phase-job', running:true, progress:progress({state:'running',phase:'checking',done_shots:48,total_shots:151}) };
  let events=0;
  on('import-state',()=>events++);
  const paint=()=>{
    I.renderBanner(); I.renderCardStatus();
    return {banner:document.getElementById('banner').textContent,chip:document.getElementById('card-status').textContent};
  };
  out.checking=paint();
  state.job.progress.phase='copying';
  out.copying=paint();
  out.events=events;
} else if (scenario === 'summary') {
  const S = await import(file('import_summary.js'));
  const extras = { not_importable: ['DCIM/100RICOH/Thumbs.db', 'DCIM/NOTE.TXT'], all_imported: false };
  // 照片都证明已导入、卡上还有别的文件
  const proven = progress({ coverage: coverage(extras) });
  out.provenSummary = S.summarizeImport(proven);
  out.provenEject = S.canEject(proven);
  out.provenToast = S.importToast(proven);
  // 证明不了（后端没给 photos_imported）：只说个数，不说「都已导入」
  const unproven = progress({ coverage: coverage({ ...extras, photos_imported: false }) });
  out.unprovenSummary = S.summarizeImport(unproven);
  out.unprovenToast = S.importToast(unproven);
  const legacy = progress({ coverage: (({ photos_imported, ...rest }) => rest)(coverage(extras)) });
  out.legacySummary = S.summarizeImport(legacy);
  // 这次导入了新照片
  const fresh = progress({ imported_shots: 3, duplicate_shots: 16, coverage: coverage(extras) });
  out.freshSummary = S.summarizeImport(fresh);
  // 什么都没新增、卡上也没有别的文件
  const clean = progress();
  out.cleanToast = S.importToast(clean);
  out.cleanEject = S.canEject(clean);
  // 有问题时不能说都在图库里
  const bad = progress({ unreadable: ['DCIM/100RICOH/R0000105.JPG'], failed_shots: 1 });
  out.badToast = S.importToast(bad);
  out.badEject = S.canEject(bad);
  out.checkFailedToast = S.importToast(progress({ coverage: coverage({ complete: false }) }));
  out.lines = [S.progressLine({ state: 'scanning', done_shots: 0, total_shots: 0 }), S.progressLine({ state: 'running', done_shots: 3, total_shots: 19 }), S.progressLine({ state: 'checking', done_shots: 19, total_shots: 19 })];
  out.allText = JSON.stringify([out.provenSummary, out.unprovenSummary, out.provenToast, out.cleanToast, out.badToast]);
} else if (scenario === 'calibration-note') {
  const N = await import(file('calibration_note.js'));
  const photo = (extra) => ({ photo: { iso: 200, lens: 'A12 50mm', ...extra }, ed: {} });
  out.isos = N.CALIBRATED_ISOS;
  out.iso400 = N.uncalibratedReason(photo({ iso: 400 }), 'jlaunch');
  out.iso500 = N.uncalibratedReason(photo({ iso: 500 }), 'jlaunch');
  out.noIso = N.uncalibratedReason(photo({ iso: null }), 'jlaunch');
  out.q28 = N.uncalibratedReason(photo({ iso: 200 }), 'qlaunch');
  out.mount = N.uncalibratedReason(photo({ iso: 1600 }), 'l08firm');
  out.unknown = N.uncalibratedReason(photo({ iso: 200 }), null);
  out.lut = N.uncalibratedReason({ photo: { iso: 200 }, ed: { lut: 'abc' } }, 'jlaunch');
  out.cross = N.uncalibratedReason({ photo: { iso: 200 }, ed: { recipe: { unit: 'qlaunch', slot: 4 } } }, 'jlaunch');
  out.other = N.uncalibratedReason(photo({ iso: 200 }), 'jlaunch');
} else if (scenario === 'export-name') {
  const E = await import(file('export.js'));
  const raw = { name: 'R8051145.DNG', has_raw: true, has_jpg: false };
  const pair = { name: 'R8051146.DNG', has_raw: true, has_jpg: true };
  const jpg = { name: 'R8051147.JPG', has_raw: false, has_jpg: true };
  out.plain = E.exportName(pair);
  out.hdr = E.exportName(pair, { hdr: true });
  out.neutral = E.exportName(raw, { neutral: true });
  out.neutralHdr = E.exportName(raw, { neutral: true, hdr: true });
  out.jpgHdr = E.exportName(jpg, { hdr: true });
  out.textPair = E.exportNameText({ single: pair, hdr: false, camdata: true });
  out.textPairHdr = E.exportNameText({ single: pair, hdr: true, camdata: true });
  out.textRawNoData = E.exportNameText({ single: raw, hdr: false, camdata: false });
  out.textRawWithData = E.exportNameText({ single: raw, hdr: false, camdata: true });
  out.textMany = E.exportNameText({ single: null, hdr: false, camdata: true });
  out.textManyHdr = E.exportNameText({ single: null, hdr: true, camdata: true });
  out.textPairReady = E.exportNameText({ single: { ...pair, hdr: 'ready' }, hdr: true, camdata: true });
  out.textPairUnsupported = E.exportNameText({ single: { ...pair, hdr: 'unsupported' }, hdr: true, camdata: true });
  out.textRawNoDataHdr = E.exportNameText({ single: raw, hdr: true, camdata: false });
  out.textManyNoData = E.exportNameText({ single: null, hdr: false, camdata: false });
  out.textManyNoDataHdr = E.exportNameText({ single: null, hdr: true, camdata: false });
  out.note = E.NAME_SUFFIX_NOTE;
  // 对话框里的「文件名」一行跟着 HDR 开关变
  const { state } = await import(file('state.js'));
  state.settings = { export_dir: '/exp' };
  state.prefs = { export_hdr: false };
  state.camdata = { ready: true, state: 'ready' };
  globalThis.fetch = async (url) => ({ ok: true, status: 200, text: async () => JSON.stringify(url.endsWith('/output-size')
    ? { sizes: { original: [1200, 800], large: [1200, 800], medium: [1200, 800], small: [1200, 800] } }
    : { export_hdr: true }) });
  const form = E.exportForm({ ids: ['p1'], label: '这一张', photos: [{ id: 'p1', ...pair }] });
  const row = () => find(form, (n) => n.classList && n.classList.contains('row') && n.textContent.startsWith('文件名')).textContent;
  out.rowBefore = row();
  const sw = find(form, (n) => n.getAttribute && n.getAttribute('aria-label') === 'HDR');
  sw.checked = true;
  sw.dispatchEvent(new Event('change'));
  await tick();
  out.rowAfter = row();
} else if (scenario === 'camera-import' || scenario === 'camera-import-extras') {
  const calls = [];
  let jobs = 0;
  const doneProgress = scenario === 'camera-import'
    ? progress()
    : progress({ coverage: coverage({ not_importable: ['DCIM/100RICOH/Thumbs.db', 'DCIM/NOTE.TXT'], all_imported: false }) });
  const job = (n, p) => ({ id: `j${n}`, kind: 'import', running: p.state === 'running', error: null, progress: p });
  const runningProgress = progress({ state: 'running', done_shots: 3, total_shots: 19, coverage: null });
  let second = job(2, runningProgress);
  const route = (method, url) => {
    if (url === '/api/cards') return [CARD];
    if (url === '/api/camdata/status') return { ready: true, camera_data: { state: 'ready' }, calibration: { state: 'ready' } };
    if (url === '/api/recipes') return { status: {}, units: [] };
    if (url.startsWith('/api/cards/') && url.endsWith('/summary')) return { shots: 19, new_shots: 19, verified: false };
    if (method === 'POST' && url === '/api/import') {
      jobs += 1;
      return jobs === 1 ? job(1, progress({ imported_shots: 19, duplicate_shots: 0, coverage: coverage() })) : second;
    }
    if (url === '/api/jobs/j2') return second;
    throw new Error(`没有替身：${method} ${url}`);
  };
  globalThis.fetch = async (url, opts = {}) => {
    const method = opts.method || 'GET';
    calls.push(`${method} ${url}`);
    let data;
    try { data = route(method, url); } catch (e) { return { ok: false, status: 500, text: async () => JSON.stringify({ message: e.message }) }; }
    return { ok: true, status: 200, text: async () => JSON.stringify(data) };
  };
  for (const id of ['card-status', 'card-chip', 'card-icon', 'banner', 'card-pop']) {
    const n = document.createElement('div');
    n.id = id;
    document.body.append(n);
  }
  const { state } = await import(file('state.js'));
  const imp = await import(file('import.js'));
  const { renderCamera } = await import(file('camera_page.js'));
  state.camdata = { ready: true, state: 'ready' };
  await imp.refreshCards(); // 插卡时自动导入一次（第一个任务：马上就是完成状态）
  const root = document.getElementById('view');
  renderCamera(root, () => true);
  await tick(5);
  out.beforeButton = !!buttonByText('导入照片', root);
  out.beforeResult = (find(root, (n) => n.getAttribute && n.getAttribute('role') === 'status') || { textContent: '' }).textContent;
  // 点「导入照片」：进度出现在原来按钮的位置
  buttonByText('导入照片', root).click();
  await tick(5);
  const prog = () => find(root, (n) => n.dataset && n.dataset.role === 'import-progress');
  out.progressText = prog() ? prog().textContent : null;
  out.importButtonWhileRunning = !!buttonByText('导入照片', root);
  out.cancelButton = !!buttonByText('取消导入', root);
  // 进度推进
  second = job(2, progress({ state: 'running', done_shots: 12, total_shots: 19, coverage: null }));
  await imp.tickImport();
  await tick(3);
  out.progressLater = prog() ? prog().textContent : null;
  // 完成：弹提示，按钮回来，页面上留一句结果
  second = job(2, doneProgress);
  await imp.tickImport();
  await tick(5);
  out.toast = document.getElementById('toast-root').textContent;
  out.progressAfter = !!prog();
  out.buttonAfter = !!buttonByText('导入照片', root);
  out.resultAfter = (find(root, (n) => n.getAttribute && n.getAttribute('role') === 'status') || { textContent: '' }).textContent;
  out.posts = calls.filter((c) => c === 'POST /api/import').length;
  // 离开相机页以后，导入状态再变也不会把按钮画回共享的 root
  root.replaceChildren(Object.assign(document.createElement('div'), { text: 'GALLERY' }));
  imp.renderCardStatus();
  out.leftPage = root.textContent;
} else if (scenario === 'recipes-tab' || scenario === 'recipes-tab-approx') {
  // -approx：只有 JPG 的照片（内置配方是近似、不带旋钮），电脑上的配方不能点
  const approx = scenario === 'recipes-tab-approx';
  const calls = [];
  const info = (slot, name) => ({ slot, name, is_stock: slot === 4, controls: [{ name: 'amount', label: 'Amount', levels: [0, 50, 100], default: 100 }, { name: 'contrast', label: 'Contrast', levels: [-1, 0, 1], default: 0 }] });
  const unit = { id: 'jlaunch', recipes: [info(1, '01 标准'), info(2, '02 鲜艳'), info(4, '04 原厂')], looks: [{ kind: 'look', id: 'night', name: 'NIGHT', slot: 4, controls: [] }],
    users: [{ kind: 'camera_user', id: 'u1', name: 'FCC-001', slot: 4, controls: [] }] };
  const mine = [{ id: 'abc123abc123', name: 'Kodak 风', recipe: { kind: 'builtin', unit: 'jlaunch', slot: 2, levels: { amount: 50, contrast: 1 } }, source: { kind: 'lut', title: 'Portra 400' } },
    { id: 'def456def456', name: '别的单元', recipe: { kind: 'builtin', unit: 'qlaunch', slot: 2, levels: {} }, source: { kind: 'lut', title: null } }];
  globalThis.fetch = async (url, opts = {}) => {
    const method = opts.method || 'GET';
    calls.push({ method, url, body: opts.body ? JSON.parse(opts.body) : null });
    let data = {};
    if (url === '/api/luts') data = { luts: [], groups: [] };
    if (url === '/api/photos/p1/edit') data = { rev: 1, recipe: null, lut: null, adjust: {}, curve: null, geometry: {}, frame: null, stamp: null, memory: {} };
    if (url === '/api/photos/p1/capabilities') data = approx ? { raw: false, recipe: 'approx', reason: '' } : { raw: true, recipe: 'full', reason: '' };
    if (url === '/api/photos/p1/meta') data = {};
    if (url === '/api/photos/p1/crop') data = { preset: 'original', rect: [0, 0, 1, 1], angle_deg: 0, orientation: 'landscape', frame: { width: 3000, height: 2000 }, rev: 1 };
    if (url === '/api/frames') data = { styles: [] };
    if (url === '/api/defaults/finish') data = { frame: { style: 'none' }, stamp: { enabled: false } };
    if (url === '/api/my-recipes') data = { recipes: mine };
    if (url === '/api/recipes') data = { status: {}, units: [unit] };
    if (method === 'PUT') data = { rev: 2 };
    return { ok: true, status: 200, text: async () => JSON.stringify(data) };
  };
  const { state } = await import(file('state.js'));
  const preview = await import(file('preview.js'));
  const { recipePanel } = await import(file('recipes.js'));
  state.camdata = { ready: true, state: 'ready' };
  state.catalog = { units: [unit] };
  const view = { photo: { id: 'p1', iso: 200, lens: 'A12 50mm', has_raw: !approx, has_jpg: true }, onChange() {}, redrawSide: () => { view.redraws = (view.redraws || 0) + 1; } };
  await preview.ensureEdit(view);
  const panel = () => recipePanel(view);
  let el = panel();
  await tick(5);
  out.redraws = view.redraws || 0; // 电脑上的配方读回来以后面板自己重画一次
  el = panel();
  out.text = el.textContent;
  out.secs = walk(el).filter((n) => n.classList && n.classList.contains('sec')).map((n) => n.textContent);
  out.sources = walk(el).filter((n) => n.classList && n.classList.contains('sec-source')).map((n) => n.textContent);
  const btn = cardByName('Kodak 风', el);
  out.hasOtherUnit = !!cardByName('别的单元', el);
  out.mineTitle = btn && btn.getAttribute('title');
  out.cameraTitle = cardByName('FCC-001', el).getAttribute('title');
  out.pressedBefore = btn.getAttribute('aria-pressed');
  out.disabled = btn.getAttribute('aria-disabled');
  btn.click();
  await tick(5);
  out.applied = view.ed.recipe ? JSON.parse(JSON.stringify(view.ed.recipe)) : null;
  out.puts = calls.filter((c) => c.method === 'PUT').length;
  out.pressedAfter = cardByName('Kodak 风', panel()).getAttribute('aria-pressed');
} else {
  throw new Error(`没有这个场景：${scenario}`);
}

console.log(JSON.stringify(out));
process.exit(0);
