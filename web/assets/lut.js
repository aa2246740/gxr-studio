// 「配方」页：导入调色文件（.cube）→ 预览 → 做成相机配方 → 相似度和说明 → 存储为我的配方；下面列出已存的"我的配方"。
// 流程见 research/studio_design/UX.md 3.9 / 流程 E。所有数字都是"电脑预测"。接口见 api/lut_routes.py。

import { api, apiBlob, ApiError } from './api.js';
import { emit } from './state.js';
import { h, toast, fmtBytes, toastError, errorBody, confirmAction } from './dom.js';
import { camdataGuide, camdataReady } from './camdata.js';
import { BASE_NAMES, KNOB_NAMES } from './preview.js';
import { loadLutLibrary, librarySection, attributionFields } from './lut_library.js';
import { selectedFiles, droppedFiles, importFiles } from './lut_files.js';

export const RECIPE_APPLY_HINT = '相机关机再开后，按 ADJ 再按 OK 套用上次的配方';

const flow = {
  cube: null,          // /api/lut/upload 的结果
  spaces: null,        // /api/lut/spaces
  space: 'srgb',
  guessed: false,
  output: 'srgb',
  outputGamut: '',
  photos: null,        // 可选的预览照片（有 RAW 的）
  photoId: '',
  which: 'lut',        // original | lut | recipe
  job: null,           // /api/lut/fit/{id}
  chosen: 0,
  name: '',
  saved: null,
  recipes: [],
  busy: null,
  spaceSaving: false,
  error: null,
  errorCode: null,
  imports: [],
  ui: null,            // 进度条的几个节点（轮询时只改它们，不重画整页，预览图不会闪）
  previewUrl: null,
  previewLabel: '',
  previewNote: '',
  nonce: 0,
};
let root = null;
let timer = null;

const LABELS = { 接近: 'ok', 大致: 'warn', 差别明显: 'bad' };
const SIM_WORDS = { 接近: '接近', 大致: '大致', 差别明显: '差别明显' };

export function renderLutPage(view) {
  root = h('div', { class: 'lut-page' });
  view.replaceChildren(h('div', { class: 'lut-scroll' }, root));
  stopPolling();
  if (flow.job && flow.job.state === 'running') startPolling();
  redraw();
  load();
}

async function load() {
  try {
    const [sp, rec] = await Promise.all([flow.spaces ? null : api('/api/lut/spaces'), api('/api/my-recipes')]);
    if (sp) flow.spaces = sp.spaces;
    flow.recipes = rec.recipes;
    await loadLutLibrary();
  } catch (e) {
    flow.error = errorBody(e, '无法读取配方。请重试。');
  }
  redraw();
}

function alive() {
  return root && root.isConnected;
}

function redraw() {
  if (!alive()) return;
  root.replaceChildren(
    h('h2', {}, '配方'),
    h('p', { class: 'hint', dataset: { role: 'recipe-hint' } }, RECIPE_APPLY_HINT),
    librarySection({ select: selectLut, onImport: resetFlow, selected: flow.cube?.cube_id, redraw: libraryChanged, busy: !!flow.busy }),
    importSection(),
    recipesSection(),
  );
}

// ---------------------------------------------------------------- 导入

function importSection() {
  const sec = h('section', { class: 'lut-card' }, h('h3', {}, '导入 LUT'));
  if (flow.error) sec.append(h('div', { class: 'result bad small' }, flow.error));
  if (flow.imports.length) sec.append(h('ul', { class: 'small lut-import-results', role: 'status' }, flow.imports.map((r) => h('li', {},
    `${r.path} · ${r.status === 'error' ? r.message : r.status === 'duplicate' ? '已在 LUT 库中' : r.status === 'skipped' ? '已跳过' : '已导入'}`))));
  if (!flow.cube) {
    sec.append(dropzone());
    return sec;
  }
  const c = flow.cube;
  const size = c.size_3d ? `${c.size_3d}×${c.size_3d}×${c.size_3d}` : `${c.size_1d} 点`;
  sec.append(
    h('p', { class: 'small' }, `${c.filename || 'LUT'} · ${c.kind} · ${size}`, c.title ? ` · “${c.title}”` : ''),
    spaceFields(),
    previewBox(),
    attributionFields(c, libraryChanged),
  );
  if (!flow.job || flow.job.state === 'cancelled' || flow.job.state === 'error') {
    const go = h('button', { class: 'btn primary', type: 'button', disabled: !!flow.busy || flow.spaceSaving || !flow.space, onclick: startFit }, '生成配方');
    sec.append(h('div', { class: 'modal-foot start' }, go,
      h('button', { class: 'btn', type: 'button', onclick: resetFlow }, '重新选择')));
    if (flow.job?.state === 'error') sec.append(h('div', { class: 'result bad small' }, flow.job.error?.message || '无法生成配方。请重试。'));
    if (flow.job?.state === 'cancelled') sec.append(h('p', { class: 'small dim' }, '已取消'));
    if (flow.errorCode === 'camera_data_missing' || flow.errorCode === 'camera_data_invalid') sec.append(guideFor());
  } else if (flow.job.state === 'running') {
    sec.append(progressBox());
  } else if (flow.job.state === 'done') {
    sec.append(resultBox());
  }
  return sec;
}

function guideFor() {
  return camdataReady() ? null : camdataGuide({ onDone: () => { flow.error = null; flow.errorCode = null; redraw(); } });
}

function dropzone() {
  const input = h('input', { type: 'file', accept: '.cube', multiple: true, class: 'hidden', 'aria-label': '导入 LUT', onchange: (e) => takeFiles(selectedFiles(e.target.files)) });
  const folder = h('input', { type: 'file', webkitdirectory: true, multiple: true, class: 'hidden', 'aria-label': '导入文件夹', onchange: (e) => takeFiles(selectedFiles(e.target.files)) });
  const zone = h('div', {
    class: 'dropzone', tabindex: 0, role: 'button', 'aria-label': '选择 LUT',
    onclick: () => input.click(),
    onkeydown: (e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), input.click()),
    ondragover: (e) => { e.preventDefault(); zone.classList.add('over'); },
    ondragleave: () => zone.classList.remove('over'),
    ondrop: async (e) => {
      e.preventDefault();
      zone.classList.remove('over');
      try { if (e.dataTransfer) await takeFiles(await droppedFiles(e.dataTransfer)); }
      catch (error) { flow.error = '无法读取文件夹。请重新选择。'; redraw(); }
    },
  },
  h('div', {}, '将 LUT 文件拖到这里，或点击选择'),
    input);
  return h('div', {}, zone, folder, h('button', { class: 'btn', type: 'button', disabled: !!flow.busy, onclick: () => folder.click() }, '导入文件夹'));
}

async function takeFiles(files) {
  if (flow.busy) return;
  flow.error = null;
  flow.busy = '正在读取…';
  redraw();
  try {
    flow.imports = await importFiles(files, (file, path) => {
      if (file.size > 64 * 1024 * 1024) throw new Error('无法读取 LUT。文件过大。');
      return api('/api/lut/upload', { method: 'POST', body: file, headers: { 'X-GXR-Filename': encodeURIComponent(path) } });
    });
    await loadLutLibrary();
    const first = flow.imports.find((r) => r.lut && !r.lut.skipped);
    if (first) await selectLut(first.lut);
  } catch (e) {
    flow.error = e instanceof ApiError ? errorBody(e, '无法读取 LUT。请重试。') : '无法读取 LUT。请重试。';
    flow.cube = null;
  } finally {
    flow.busy = null;
    redraw();
  }
}

async function selectLut(data) {
  if (flow.job?.state === 'running') {
    try { await api(`/api/lut/fit/${flow.job.job_id}/cancel`, { method: 'POST' }); }
    catch (error) { flow.error = errorBody(error, '无法完成操作。请重试。'); redraw(); return; }
  }
  stopPolling();
  Object.assign(flow, { cube: data, job: null, chosen: 0, saved: null, which: 'lut', space: data.input_space || '',
    guessed: !data.input_space_manual && ['known', 'guessed'].includes(data.input_space_state), fileGuess: false,
    output: data.output_space || 'srgb', outputGamut: data.output_gamut || '', name: nextName(), error: null });
  await loadPhotos();
  await refreshPreview();
  redraw();
}

function libraryChanged(item) {
  if (item?.deleted === flow.cube?.cube_id) resetFlow();
  else if (item?.cube_id === flow.cube?.cube_id) {
    flow.cube = { ...flow.cube, ...item };
  }
  redraw();
}

function nextName() {
  const used = new Set(flow.recipes.map((r) => r.name));
  for (let n = 1; ; n++) {
    const name = `我的配方 ${n}`;
    if (!used.has(name)) return name;
  }
}

async function resetFlow() {
  if (flow.job?.state === 'running') {
    try { await api(`/api/lut/fit/${flow.job.job_id}/cancel`, { method: 'POST' }); }
    catch (error) { flow.error = errorBody(error, '无法完成操作。请重试。'); redraw(); return; }
  }
  stopPolling();
  flow.nonce++;
  if (flow.previewUrl) URL.revokeObjectURL(flow.previewUrl);
  Object.assign(flow, { cube: null, job: null, error: null, saved: null, previewUrl: null, busy: null });
  redraw();
}

async function loadPhotos() {
  if (flow.photos) return;
  try {
    const all = await api('/api/photos?limit=300');
    flow.photos = all.filter((p) => p.has_raw).slice(0, 60);
  } catch {
    flow.photos = [];
  }
}

function spaceFields() {
  const sp = flow.spaces || [];
  const opt = (s) => h('option', { value: s.id, selected: s.id === flow.space }, s.label);
  const select = h('select', {
    'aria-label': '输入色彩空间',
    disabled: flow.spaceSaving,
    onchange: (e) => { flow.space = e.target.value; flow.guessed = false; flow.fileGuess = false; afterSpaceChange(); },
  },
  h('option', { value: '', selected: !flow.space, disabled: true }, '请选择输入色彩空间'),
  h('optgroup', { label: '画面' }, sp.filter((s) => s.kind === 'display').map(opt)),
  h('optgroup', { label: 'Log' }, sp.filter((s) => s.kind === 'log').map(opt)));
  const dunno = h('button', { class: 'btn ghost small', type: 'button', disabled: flow.spaceSaving, onclick: () => { flow.space = 'srgb'; flow.guessed = true; flow.fileGuess = false; afterSpaceChange(); } }, '不确定');
  // 输入色彩空间是推测的：小标签「推测」，按文件名判断的理由放进它的提示气泡（COPY 3.10）
  const guessed = flow.guessed || flow.fileGuess;
  const evidence = flow.cube.space_evidence || {};
  const why = flow.cube.input_space_state === 'conflict' && evidence.content ? `${flow.cube.guess_reason || ''}；内容：${evidence.content === 'display' ? '显示类' : 'Log 类'}` : flow.cube.guess_reason;
  const adv = h('details', { class: 'small' },
    h('summary', {}, '输出色彩空间'),
    h('div', { class: 'field-row' },
      h('select', { 'aria-label': '输出色彩空间', disabled: flow.spaceSaving, onchange: (e) => { flow.output = e.target.value; afterSpaceChange(); } },
        sp.filter((s) => s.kind === 'display').map((s) => h('option', { value: s.id, selected: s.id === flow.output }, s.label))),
      h('select', { 'aria-label': '输出色域', disabled: flow.spaceSaving, onchange: (e) => { flow.outputGamut = e.target.value; afterSpaceChange(); } },
        [['', '默认'], ['rec709', 'Rec.709'], ['rec2020', 'Rec.2020']].map(([v, t]) => h('option', { value: v, selected: v === flow.outputGamut }, t)))));
  return h('div', { class: 'field' },
    h('span', {}, '输入色彩空间'),
    h('div', { class: 'field-row' }, select, guessed ? h('span', { class: 'tag', title: why || null }, '推测') : null,
      flow.cube.input_space_state === 'conflict' && !flow.cube.input_space_manual ? h('span', { class: 'tag', title: why }, '需要选择') : null, dunno),
    adv);
}

function spaceQuery() {
  const q = new URLSearchParams({ space: flow.space, output: flow.output });
  if (flow.outputGamut) q.set('output_gamut', flow.outputGamut);
  return q;
}

async function afterSpaceChange() {
  if (flow.spaceSaving) return;
  flow.spaceSaving = true;
  const cubeId = flow.cube.cube_id;
  redraw();
  try {
    const updated = await api(`/api/luts/${cubeId}`, { method: 'PATCH', body: {
      input_space: flow.space, output_space: flow.output, output_gamut: flow.outputGamut || null,
    } });
    if (flow.cube?.cube_id === cubeId) flow.cube = { ...flow.cube, ...updated };
    emit('luts-changed'); // 显卡预览的 LUT 表作废（gpu_stage.js）
    await loadLutLibrary();
  } catch (e) { flow.error = errorBody(e, '无法保存修改。请重试。'); redraw(); return; }
  finally { flow.spaceSaving = false; redraw(); }
  if (flow.cube?.cube_id !== cubeId) return;
  if (flow.job && flow.job.state !== 'running') flow.job = null;
  await refreshPreview();
  redraw();
}

// ---------------------------------------------------------------- 预览

function previewBox() {
  const photoSel = h('select', {
    'aria-label': '预览照片',
    onchange: async (e) => { flow.photoId = e.target.value; await refreshPreview(); redraw(); },
  }, h('option', { value: '' }, '示例照片'),
  (flow.photos || []).map((p) => h('option', { value: p.id, selected: p.id === flow.photoId }, `${p.name} · ISO ${p.iso ?? '?'}`)));
  const done = flow.job?.state === 'done';
  const tabs = [['original', '原图'], ['lut', '加 LUT 后']];
  if (done && flow.photoId) tabs.push(['recipe', '电脑预测']);
  return h('div', { class: 'lut-preview' },
    h('div', { class: 'field-row' }, h('span', { class: 'small dim' }, '预览照片'), photoSel),
    h('div', { class: 'seg', role: 'group', 'aria-label': '预览' }, tabs.map(([k, t]) => h('button', {
      type: 'button', 'aria-pressed': String(flow.which === k),
      onclick: async () => { flow.which = k; await refreshPreview(); redraw(); },
    }, t))),
    flow.previewUrl ? h('img', { class: 'lut-img', src: flow.previewUrl, alt: '预览' }) : h('div', { class: 'lut-img empty-img small dim' }, flow.busy || (!flow.space ? '请选择输入色彩空间' : '预览生成中…')),
    h('p', { class: 'small dim' }, `${flow.previewLabel ? flow.previewLabel + ' · ' : ''}电脑预测`, flow.previewNote));
}

async function refreshPreview() {
  const n = ++flow.nonce;
  if (!flow.space) {
    if (flow.previewUrl) URL.revokeObjectURL(flow.previewUrl);
    flow.previewUrl = null;
    flow.previewLabel = '请选择输入色彩空间';
    flow.previewNote = '';
    return;
  }
  if (!flow.cube) return;
  try {
    let res;
    let labelText = '';
    let noteText = '';
    if (flow.which === 'recipe' && flow.photoId && flow.job?.state === 'done') {
      const c = flow.job.result.candidates[flow.chosen];
      res = await apiBlob(`/api/photos/${flow.photoId}/render?max_edge=640`, { method: 'POST', body: { recipe: { kind: 'builtin', unit: c.unit, slot: c.slot, levels: c.levels } } });
      labelText = '电脑预测';
    } else {
      const q = spaceQuery();
      q.set('which', flow.which === 'original' ? 'original' : 'lut');
      if (flow.photoId) q.set('photo', flow.photoId);
      res = await apiBlob(`/api/lut/${flow.cube.cube_id}/preview?${q}`, { method: 'GET' });
      const label = decodeURIComponent(res.headers.get('X-GXR-Label') || '');
      labelText = flow.which === 'original' ? label : `LUT（${label}）`;
      noteText = res.headers.get('X-GXR-Nominal-Scene') === '1' ? ' 示例' : '';
    }
    if (n !== flow.nonce) return;
    flow.previewLabel = labelText;
    flow.previewNote = noteText;
    if (flow.previewUrl) URL.revokeObjectURL(flow.previewUrl);
    flow.previewUrl = URL.createObjectURL(res.blob);
  } catch (e) {
    if (n !== flow.nonce) return;
    flow.previewUrl = null;
    flow.previewLabel = '';
    flow.previewNote = e.message;
  }
}

// ---------------------------------------------------------------- 拟合

async function startFit() {
  flow.error = null;
  flow.errorCode = null;
  flow.busy = '正在开始…';
  redraw();
  try {
    const body = { cube_id: flow.cube.cube_id, space: flow.space, output: flow.output, space_guessed: flow.guessed, profile: 'normal' };
    if (flow.outputGamut) body.output_gamut = flow.outputGamut;
    const r = await api('/api/lut/fit', { method: 'POST', body });
    flow.job = { job_id: r.job_id, state: 'running', progress: null };
    startPolling();
  } catch (e) {
    flow.error = errorBody(e, '无法生成配方。请重试。');
    flow.errorCode = e.code || null;
    if (e.code === 'fit_busy') toast(e.message, 'info');
  } finally {
    flow.busy = null;
    redraw();
  }
}

function startPolling() {
  stopPolling();
  timer = setInterval(poll, 700);
}

function stopPolling() {
  if (timer) clearInterval(timer);
  timer = null;
}

async function poll() {
  if (!flow.job) return stopPolling();
  if (!alive()) return stopPolling();       // 离开了这一页：停止轮询（任务本身在后台继续，回来还能看到）
  try {
    const st = await api(`/api/lut/fit/${flow.job.job_id}`);
    flow.job = st;
    if (st.state !== 'running') {
      stopPolling();
      flow.chosen = 0;
      if (st.state === 'done' && flow.photoId) flow.which = 'lut';
    }
  } catch (e) {
    flow.job = { ...flow.job, state: 'error', error: { message: e.message } };
    stopPolling();
  }
  if (flow.job.state === 'running') updateProgress();
  else redraw();
}

function progressBox() {
  const text = h('div', {});
  const bar = h('i', {});
  const detail = h('div', { class: 'small dim' });
  flow.ui = { text, bar, detail };
  updateProgress();
  return h('div', { class: 'result' }, text, h('div', { class: 'progress' }, bar), detail,
    h('div', { class: 'modal-foot start' },
      h('button', { class: 'btn', type: 'button', onclick: cancelFit }, '取消')));
}

function updateProgress() {
  const p = flow.job?.progress;
  const pct = p ? Math.round(p.fraction * 100) : 0;
  const ui = flow.ui;
  if (!ui) return;
  ui.text.textContent = `正在生成配方… ${pct}%`;
  ui.bar.style.width = `${pct}%`;
  ui.detail.textContent = p ? `${p.phase}${p.message ? ' · ' + p.message : ''} · 已试 ${p.evaluations} 种组合 · ${p.elapsed ?? 0} 秒` : '准备中…';
}

async function cancelFit() {
  try {
    flow.job = await api(`/api/lut/fit/${flow.job.job_id}/cancel`, { method: 'POST' });
  } catch (e) {
    toastError(e, '无法完成操作。请重试。');
  }
  stopPolling();
  redraw();
}

// ---------------------------------------------------------------- 结果

function simText(c) {
  return c.similarity === null ? 'N/A' : `${Math.round(c.similarity)}%`;
}

function describeLevels(c) {
  const parts = [];
  if (c.amount !== null && c.amount !== 100) parts.push(`强度 ${c.amount}%`);
  for (const [k, v] of Object.entries(c.levels)) {
    if (k === 'amount' || v === 0) continue;
    parts.push(`${KNOB_NAMES[k] || k} ${v > 0 ? '+' : ''}${v}`);
  }
  return parts.length ? parts.join('，') : '无调整';
}

function resultBox() {
  const res = flow.job.result;
  const c = res.candidates[flow.chosen];
  const tone = LABELS[c.label] || 'warn';
  const bar = Math.max(0, Math.min(100, c.similarity ?? 0));
  const picks = h('div', { class: 'seg', role: 'group', 'aria-label': '最接近的内置配方' }, res.candidates.map((x, i) => h('button', {
    type: 'button', 'aria-pressed': String(i === flow.chosen),
    onclick: async () => { flow.chosen = i; flow.saved = null; if (flow.which === 'recipe') await refreshPreview(); redraw(); },
  }, `${x.number} ${BASE_NAMES[x.slot - 1]} ${simText(x)}`)));
  const nameInput = h('input', { type: 'text', value: flow.name, maxlength: 40, 'aria-label': '名称', oninput: (e) => { flow.name = e.target.value; } });
  const save = h('button', { class: 'btn primary', type: 'button', disabled: !!flow.busy, onclick: saveRecipe }, '存储为我的配方');
  return h('div', { class: 'result lut-result' },
    h('div', { class: 'small dim' }, '最接近的内置配方'),
    picks,
    h('div', { class: 'sim-line' },
      h('span', { class: 'small dim' }, '相似度'),
      h('b', { class: `sim ${tone}` }, simText(c)),
      h('span', { class: 'tag' }, '电脑预测'),
      h('div', { class: 'sim-bar' }, h('i', { class: tone, style: { width: `${bar}%` } })),
      h('span', { class: `sim-tag ${tone}` }, c.label ? SIM_WORDS[c.label] : '接近 04 标准')),
    h('div', { class: 'small' }, '内置配方：', `${c.number} ${BASE_NAMES[c.slot - 1]}（${describeLevels(c)}）`),
    h('div', { class: 'small dim' }, `色差 ${c.holdout.median.toFixed(2)}`),
    h('div', { class: 'small dim' }, '差异'),
    h('ul', { class: 'diffs' }, c.differences.map((t) => h('li', {}, t))),
    h('div', { class: 'field' }, h('span', { class: 'small dim' }, '名称'), nameInput),
    h('div', { class: 'modal-foot start' }, save,
      h('button', { class: 'btn', type: 'button', onclick: resetFlow }, '重新选择')),
    flow.saved ? h('div', { class: 'small ok-text' }, `已存储为「${flow.saved.name}」`) : null);
}

async function saveRecipe() {
  flow.busy = '正在保存…';
  redraw();
  try {
    flow.saved = await api('/api/my-recipes', { method: 'POST', body: { job_id: flow.job.job_id, candidate: flow.chosen, name: flow.name } });
    flow.recipes = (await api('/api/my-recipes')).recipes;
    toast(`已存储为「${flow.saved.name}」`, 'ok');
    flow.name = nextName();
  } catch (e) {
    toastError(e, '无法完成操作。请重试。');
  } finally {
    flow.busy = null;
    redraw();
  }
}

// ---------------------------------------------------------------- 我的配方

function recipesSection() {
  const sec = h('section', { class: 'lut-card' }, h('h3', {}, '我的配方'));
  if (!flow.recipes.length) {
    sec.append(h('p', { class: 'small dim' }, '还没有我的配方'));
    return sec;
  }
  for (const r of flow.recipes) sec.append(recipeCard(r));
  return sec;
}

function recipeCard(r) {
  const c = { amount: r.recipe.levels.amount ?? null, levels: r.recipe.levels };
  const sim = r.fit.similarity === null ? 'N/A' : `${Math.round(r.fit.similarity)}%`;
  const card = h('div', { class: 'recipe-card' });
  const del = h('button', { class: 'btn ghost small', type: 'button', onclick: () => askRemove(r) }, '删除');
  const rename = h('button', { class: 'btn ghost small', type: 'button', onclick: () => editName(r) }, '重命名');
  card.append(
    h('div', { class: 'recipe-head' }, h('b', {}, r.name), h('span', { class: 'small dim' }, ` · ${r.camera_short_name}`), h('span', { class: 'grow' }), rename, del),
    h('div', { class: 'small' }, `${String(r.recipe.slot).padStart(2, '0')} ${BASE_NAMES[r.recipe.slot - 1]}（${describeLevels(c)}）`),
    h('div', { class: 'small dim' }, `来自 LUT${r.source.title ? `「${r.source.title}」` : ''} · 相似度 ${sim}`,
      r.source.input_space_guessed ? h('span', { class: 'tag' }, '推测') : null),
  );
  return card;
}

// 删除确认（COPY 3.10）：标题「删除「名称」？」，按钮 [取消] [删除]
function askRemove(r) {
  confirmAction(`删除「${r.name}」？`, '删除', () => removeRecipe(r));
}

async function removeRecipe(r) {
  await api(`/api/my-recipes/${r.id}`, { method: 'DELETE' });
  flow.recipes = flow.recipes.filter((x) => x.id !== r.id);
  redraw();
}

function editName(r) {
  const next = window.prompt('名称', r.name);
  if (next === null) return;
  api(`/api/my-recipes/${r.id}`, { method: 'PUT', body: { name: next } })
    .then((doc) => { flow.recipes = flow.recipes.map((x) => (x.id === doc.id ? doc : x)); redraw(); })
    .catch((e) => toastError(e, '无法完成操作。请重试。'));
}
