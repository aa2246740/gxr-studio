// 「配方」页：导入调色文件（.cube）→ 预览 → 做成相机配方 → 相似度和说明 → 存储为我的配方；下面列出已存的"我的配方"。
// 流程见 research/studio_design/UX.md 3.9 / 流程 E。所有数字都是"电脑预测"。接口见 api/lut_routes.py。

import { api, apiBlob, ApiError } from './api.js';
import { emit, state } from './state.js';
import { h, toast, toastError, errorBody, confirmAction, setFraction } from './dom.js';
import { camdataGuide, camdataReady } from './camdata.js';
import { KNOB_NAMES, recipeLabel } from './preview.js';
import { loadLutLibrary, librarySection, attributionFields } from './lut_library.js';
import { selectedFiles, droppedFiles, importFiles } from './lut_files.js';
import { every } from './poll.js';
import { MY_RECIPES_CHANGED, sourceText } from './my_recipes.js';
import { delayed, SHOW_AFTER_MS } from './busy.js';
import { mostUsedUnit, photoUnit, suggestedName, spaceFields, unitFields, unitName, fitSupported, UNIT_NAMES } from './lut_choices.js';

export const RECIPE_APPLY_HINT = '相机关机再开后，按 ADJ 再按 OK 套用上次的配方';

const flow = {
  cube: null,          // /api/lut/upload 的结果
  spaces: null,        // /api/lut/spaces
  units: null,
  space: 'srgb',
  guessed: false,
  output: 'srgb',
  outputGamut: '',
  photos: null,        // 图库照片：预览和默认镜头组件共用
  photoId: '',
  unit: 'jlaunch',
  unitManual: false,
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
let photoLoad = null;

const LABELS = { 接近: 'ok', 大致: 'warn', 差别明显: 'bad' };
const SIM_WORDS = { 接近: '接近', 大致: '大致', 差别明显: '差别明显' };

export function renderLutPage(view) {
  const unit = new URLSearchParams(location.hash.split('?')[1] || '').get('unit');
  if (Object.hasOwn(UNIT_NAMES, unit) && flow.job?.state !== 'running') {
    if (flow.unit !== unit) flow.job = flow.saved = null;
    flow.unit = unit;
    flow.unitManual = true;
  }
  root = h('div', { class: 'lut-page' });
  view.replaceChildren(h('div', { class: 'lut-scroll' }, root));
  stopPolling();
  if (flow.job && flow.job.state === 'running') startPolling();
  redraw();
  load();
}

async function load() {
  try {
    const [sp, rec] = await Promise.all([api('/api/lut/spaces'), api('/api/my-recipes')]);
    if (sp) { flow.spaces = sp.spaces; flow.units = sp.units; }
    flow.recipes = rec.recipes;
    await loadPhotos();
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
    h('p', { class: 'small dim', dataset: { role: 'page-lead' } }, '用 LUT 做配方，存在这台电脑里。为已校准的镜头单元生成电脑预测配方。'),
    importSection(),
    flow.cube ? h('details', { class: 'lut-library-disclosure' }, h('summary', {}, '选择其他 LUT · 管理 LUT 库'), lutLibrary()) : lutLibrary(),
    recipesSection(),
    h('p', { class: 'hint' }, h('span', { class: 'dim' }, '在相机上：'), h('span', { dataset: { role: 'recipe-hint' } }, RECIPE_APPLY_HINT)),
  );
}

function lutLibrary() {
  return librarySection({ select: selectLut, onImport: resetFlow, selected: flow.cube?.cube_id, redraw: libraryChanged, busy: !!flow.busy || flow.spaceSaving || flow.job?.state === 'running' });
}

// ---------------------------------------------------------------- 导入

function importSection() {
  const sec = h('section', { class: 'lut-card' }, h('h3', {}, '用 LUT 生成配方'));
  const report = importResults();
  if (flow.error) sec.append(h('div', { class: 'result bad small' }, flow.error));
  if (!flow.cube) {
    sec.append(dropzone());
    if (report) sec.append(report);
    return sec;
  }
  const c = flow.cube;
  const size = c.size_3d ? `${c.size_3d}×${c.size_3d}×${c.size_3d}` : `${c.size_1d} 点`;
  sec.append(h('p', { class: 'small lut-file' }, c.title || c.filename || 'LUT'));
  const controls = h('div', { class: 'lut-controls', dataset: { udCheck: 'lut-controls' } },
    spaceFields(flow, afterSpaceChange), unitFields(flow, chooseUnit));
  const workspace = h('div', { class: 'lut-workspace' }, previewBox(), controls);
  sec.append(workspace);
  if (!flow.job || flow.job.state === 'cancelled' || flow.job.state === 'error') {
    const go = h('button', { class: 'btn primary', type: 'button', disabled: !!flow.busy || flow.spaceSaving || !flow.space || !fitSupported(flow), onclick: startFit }, `为${unitName(flow.unit)}生成配方`);
    controls.append(h('div', { class: 'modal-foot start', dataset: { role: 'lut-generate' } }, go,
      h('button', { class: 'btn', type: 'button', disabled: !!flow.busy || flow.spaceSaving, onclick: resetFlow }, '重新选择')));
    if (flow.job?.state === 'error') controls.append(h('div', { class: 'result bad small' }, flow.job.error?.message || '无法生成配方。请重试。'));
    if (flow.job?.state === 'cancelled') controls.append(h('p', { class: 'small dim' }, '已取消'));
    if (flow.errorCode === 'camera_data_missing' || flow.errorCode === 'camera_data_invalid') controls.append(guideFor());
  } else if (flow.job.state === 'running') {
    controls.append(progressBox());
  } else if (flow.job.state === 'done') {
    controls.append(resultBox());
  }
  sec.append(h('details', { class: 'lut-more' }, h('summary', {}, '更多信息（可选）'),
    h('p', { class: 'small dim' }, `${c.filename || 'LUT'} · ${c.kind} · ${size}`), attributionFields(c, libraryChanged)));
  if (report) sec.append(report);
  return sec;
}

function importResults() {
  if (!flow.imports.length) return null;
  const errors = flow.imports.filter((r) => r.status === 'error').length;
  return h('details', { class: 'small lut-import-results' },
    h('summary', {}, `导入结果（${flow.imports.length} 个文件${errors ? `，${errors} 个未能导入` : ''}）`),
    h('ul', {}, flow.imports.map((r) => h('li', {},
      `${r.path} · ${r.status === 'error' ? r.message : r.status === 'duplicate' ? '已在 LUT 库中' : r.status === 'skipped' ? '已跳过' : '已导入'}`))));
}

function guideFor() {
  return camdataReady() ? null : camdataGuide({ onDone: () => { flow.error = null; flow.errorCode = null; redraw(); } });
}

function dropzone() {
  const input = h('input', { type: 'file', accept: '.cube', multiple: true, class: 'hidden', 'aria-label': '导入 LUT', onchange: (e) => takeFiles(selectedFiles(e.target.files)) });
  const folder = h('input', { type: 'file', webkitdirectory: true, multiple: true, class: 'hidden', 'aria-label': '导入文件夹', onchange: (e) => takeFiles(selectedFiles(e.target.files)) });
  const zone = h('div', {
    class: 'dropzone', tabindex: 0, role: 'button', 'aria-label': '选择 LUT',
    onclick: () => !flow.busy && input.click(),
    onkeydown: (e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), !flow.busy && input.click()),
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
  if (flow.busy) {
    // 导入中：区域锁住；150ms 后才显示进度（文件夹显示 n/N），快的时候不闪
    zone.setAttribute('aria-disabled', 'true');
    const text = h('span', { class: 'small' }, flow.busy);
    const bar = h('i', {});
    setFraction(bar, flow.importFrac || 0);
    const row = h('div', { class: 'lut-import-progress', role: 'status', hidden: true }, h('div', { class: 'progress' }, bar), text);
    flow.importUi = { row, text, bar };
    if (flow.importShown) row.hidden = false;
    zone.append(row);
  }
  return h('div', {}, zone, folder, h('button', { class: 'btn', type: 'button', disabled: !!flow.busy, onclick: () => folder.click() }, '导入文件夹'));
}

async function takeFiles(files) {
  if (flow.busy) return;
  flow.error = null;
  flow.busy = files.length > 1 ? `正在导入 0/${files.length}…` : '正在读取…';
  flow.importFrac = 0;
  flow.importShown = false;
  const show = delayed(() => {
    flow.importShown = true;
    if (flow.importUi) flow.importUi.row.hidden = false;
  }, SHOW_AFTER_MS);
  redraw();
  try {
    flow.imports = await importFiles(files, (file, path) => {
      if (file.size > 64 * 1024 * 1024) throw new Error('无法读取 LUT。文件过大。');
      return api('/api/lut/upload', { method: 'POST', body: file, headers: { 'X-GXR-Filename': encodeURIComponent(path) } });
    }, (done, total) => {
      if (total < 2) return;
      flow.busy = `正在导入 ${done}/${total}…`;
      flow.importFrac = done / total;
      if (flow.importUi) {
        flow.importUi.text.textContent = flow.busy;
        setFraction(flow.importUi.bar, flow.importFrac);
      }
    });
    await loadLutLibrary();
    const first = flow.imports.find((r) => r.lut && !r.lut.skipped);
    if (first) await selectLut(first.lut);
  } catch (e) {
    flow.error = e instanceof ApiError ? errorBody(e, '无法读取 LUT。请重试。') : '无法读取 LUT。请重试。';
    flow.cube = null;
  } finally {
    show.cancel();
    flow.busy = null;
    flow.importUi = null;
    redraw();
  }
}

async function selectLut(data) {
  if (flow.job?.state === 'running') {
    try { await api(`/api/lut/fit/${flow.job.job_id}/cancel`, { method: 'POST' }); }
    catch (error) { flow.error = errorBody(error, '没有停下正在生成的配方。请重试。'); redraw(); return; }
  }
  stopPolling();
  Object.assign(flow, { cube: data, job: null, chosen: 0, saved: null, which: 'lut', space: data.input_space || '',
    guessed: ['guessed', 'fallback'].includes(data.default_input_source),
    output: data.output_space || 'srgb', outputGamut: data.output_gamut || '', name: suggestedName(data, flow.recipes), error: null, errorCode: null });
  flow.space = data.default_input_space ?? data.input_space ?? '';
  await loadPhotos();
  await refreshPreview();
  redraw();
  root?.closest('.lut-scroll')?.scrollTo({ top: 0 });
}

function libraryChanged(item) {
  if (item?.deleted === flow.cube?.cube_id) resetFlow();
  else if (item?.cube_id === flow.cube?.cube_id) {
    flow.cube = { ...flow.cube, ...item };
  }
  redraw();
}

async function resetFlow() {
  if (flow.job?.state === 'running') {
    try { await api(`/api/lut/fit/${flow.job.job_id}/cancel`, { method: 'POST' }); }
    catch (error) { flow.error = errorBody(error, '没有停下正在生成的配方。请重试。'); redraw(); return; }
  }
  stopPolling();
  flow.nonce++;
  if (flow.previewUrl) URL.revokeObjectURL(flow.previewUrl);
  Object.assign(flow, { cube: null, job: null, error: null, errorCode: null, saved: null, previewUrl: null, busy: null });
  redraw();
  root?.closest('.lut-scroll')?.scrollTo({ top: 0 });
}

async function loadPhotos() {
  if (flow.photos) return;
  if (!photoLoad) photoLoad = readPhotos();
  return photoLoad;
}

async function readPhotos() {
  try {
    const all = [];
    for (let offset = 0; ; offset += 5000) {
      const page = await api(`/api/photos?limit=5000&offset=${offset}`);
      all.push(...page);
      if (page.length < 5000) break;
    }
    flow.photos = all.map((p) => ({ ...p, unit: photoUnit(p) }));
    const photos = flow.photos;
    const selected = photos.find((p) => state.selection.has(p.id));
    const preferred = flow.unitManual ? flow.unit : mostUsedUnit(photos);
    const photo = selected || photos.find((p) => p.unit === preferred && p.has_raw) || photos.find((p) => p.unit === preferred) || photos[0];
    flow.photoId = photo?.id || '';
    if (!flow.unitManual) flow.unit = Object.hasOwn(UNIT_NAMES, photo?.unit) ? photo.unit : preferred;
  } catch {
    flow.photos = [];
    flow.error = '无法读取预览照片。请重新打开配方页后重试。';
  }
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
  const previous = { space: flow.cube.default_input_space || '', output: flow.cube.output_space || 'srgb', outputGamut: flow.cube.output_gamut || '' };
  flow.job = flow.saved = null;
  flow.which = 'lut';
  redraw();
  try {
    const updated = await api(`/api/luts/${cubeId}`, { method: 'PATCH', body: {
      input_space: flow.space, output_space: flow.output, output_gamut: flow.outputGamut || null,
    } });
    if (flow.cube?.cube_id === cubeId) {
      flow.cube = { ...flow.cube, ...updated };
      flow.guessed = false;
      flow.error = null;
      flow.job = flow.saved = null;
      flow.which = 'lut';
    }
    emit('luts-changed'); // 显卡预览的 LUT 表作废（gpu_stage.js）
    await loadLutLibrary();
  } catch (e) {
    Object.assign(flow, previous);
    flow.error = errorBody(e, '无法保存修改。请重试。');
    await refreshPreview();
    return;
  }
  finally { flow.spaceSaving = false; redraw(); }
  if (flow.cube?.cube_id !== cubeId) return;
  await refreshPreview();
  redraw();
}

async function chooseUnit(unit) {
  if (flow.busy || flow.job?.state === 'running') return;
  flow.unit = unit;
  flow.unitManual = true;
  flow.job = flow.saved = null;
  flow.which = 'lut';
  redraw();
  await refreshPreview();
  redraw();
}

// ---------------------------------------------------------------- 预览

function previewBox() {
  const photoSel = h('select', {
    'aria-label': '预览照片',
    onchange: async (e) => {
      flow.photoId = e.target.value;
      const photo = flow.photos?.find((p) => p.id === flow.photoId);
      if (!flow.unitManual && !flow.job && Object.hasOwn(UNIT_NAMES, photo?.unit)) flow.unit = photo.unit;
      if (photo?.unit !== flow.job?.unit) flow.which = 'lut';
      redraw();
      await refreshPreview();
      redraw();
    },
  }, h('option', { value: '' }, '示例照片'),
  (flow.photos || []).map((p) => h('option', { value: p.id, selected: p.id === flow.photoId }, `${p.name} · ${unitName(p.unit)}`)));
  const done = flow.job?.state === 'done';
  const photo = flow.photos?.find((p) => p.id === flow.photoId);
  const tabs = [['original', '原图'], ['lut', '加 LUT 后']];
  if (done && photo?.unit === flow.job.unit) tabs.push(['recipe', '配方预测']);
  return h('div', { class: 'lut-preview', dataset: { udCheck: 'lut-preview' } },
    h('label', { class: 'field' }, h('span', { class: 'small dim' }, '预览照片'), photoSel),
    h('div', { class: 'seg', role: 'group', 'aria-label': '预览' }, tabs.map(([k, t]) => h('button', {
      type: 'button', 'aria-pressed': String(flow.which === k),
      onclick: async () => { flow.which = k; await refreshPreview(); redraw(); },
    }, t))),
    flow.previewUrl ? h('img', { class: 'lut-img', src: flow.previewUrl, alt: '预览' }) : h('div', { class: 'lut-img empty-img small dim' }, flow.busy || (!flow.space ? '请选择输入色彩空间' : '预览生成中…')),
    h('p', { class: 'small dim' }, `${flow.previewLabel ? flow.previewLabel + ' · ' : ''}电脑预测`, flow.previewNote),
    photo && photo.unit !== 'jlaunch' ? h('p', { class: 'small dim' }, '曝光沿用 50mm。') : null);
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
  if (!fitSupported(flow) || flow.busy || flow.spaceSaving || !flow.space) return;
  flow.error = null;
  flow.errorCode = null;
  flow.busy = '正在开始…';
  redraw();
  try {
    const body = { cube_id: flow.cube.cube_id, unit: flow.unit, space: flow.space, output: flow.output, space_guessed: flow.guessed, profile: 'normal' };
    if (flow.outputGamut) body.output_gamut = flow.outputGamut;
    const r = await api('/api/lut/fit', { method: 'POST', body });
    flow.job = { ...r, state: 'running', progress: null };
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

// 拟合进度每 700ms 读一次：一次只有一个请求在路上，页面在后台时暂停，回来马上补一次（poll.js）
function startPolling() {
  stopPolling();
  timer = every(700, poll);
}

function stopPolling() {
  if (timer) timer.stop();
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
  return h('div', { class: 'result', role: 'status' }, text, h('div', { class: 'progress' }, bar), detail,
    h('div', { class: 'modal-foot start' },
      h('button', { class: 'btn', type: 'button', onclick: cancelFit }, '取消')));
}

function updateProgress() {
  const p = flow.job?.progress;
  const pct = p ? Math.round(p.fraction * 100) : 0;
  const ui = flow.ui;
  if (!ui) return;
  ui.text.textContent = `正在为${unitName(flow.job.unit)}生成配方… ${pct}%`;
  setFraction(ui.bar, pct / 100);
  ui.detail.textContent = !p ? '正在准备颜色…' : p.fraction < 0.2 ? '正在比较配方的颜色…' : '正在调整颜色，让它更接近 LUT…';
}

async function cancelFit() {
  try {
    flow.job = await api(`/api/lut/fit/${flow.job.job_id}/cancel`, { method: 'POST' });
  } catch (e) {
    toastError(e, '没有取消成功。请重试。');
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
    type: 'button', disabled: !!flow.busy, 'aria-pressed': String(i === flow.chosen),
    onclick: async () => { flow.chosen = i; flow.saved = null; if (flow.which === 'recipe') await refreshPreview(); redraw(); },
  }, `方案 ${i + 1} · ${simText(x)}`)));
  const nameInput = h('input', { type: 'text', value: flow.name, maxlength: 40, disabled: !!flow.busy || !!flow.saved, 'aria-label': '名称', oninput: (e) => { flow.name = e.target.value; } });
  const save = h('button', { class: 'btn primary', type: 'button', disabled: !!flow.busy || !!flow.saved, onclick: saveRecipe }, flow.saved ? '配方已保存' : '保存相机配方');
  return h('div', { class: 'result lut-result' },
    h('div', { class: 'lut-result-title', role: 'status' }, `为${unitName(c.unit)}做的配方`),
    h('div', { class: 'sim-line' },
      h('span', { class: 'small dim' }, '相似度'),
      h('b', { class: `sim ${tone}` }, simText(c)),
      h('span', { class: 'tag' }, '电脑预测'),
      h('div', { class: 'sim-bar' }, h('i', { class: tone, style: { width: `${bar}%` } })),
      h('span', { class: `sim-tag ${tone}` }, c.label ? SIM_WORDS[c.label] : '接近 04 标准')),
    h('details', { class: 'lut-candidate-details small' }, h('summary', {}, '比较方案与颜色差异'),
      picks,
      h('div', {}, '基于内置配方：', `${recipeLabel(c.unit, c)}（${describeLevels(c)}）`),
      h('div', { class: 'dim' }, `色差 ${c.holdout.median.toFixed(2)}`),
      h('ul', { class: 'diffs' }, c.differences.map((t) => h('li', {}, t)))),
    h('label', { class: 'field' }, h('span', { class: 'small dim' }, '配方名称'), nameInput),
    h('div', { class: 'small dim' }, '保存在这台电脑的「我的配方」中。不会写入存储卡。'),
    h('div', { class: 'modal-foot start' }, save,
      h('button', { class: 'btn', type: 'button', disabled: !!flow.busy, onclick: resetFlow }, '重新选择')),
    flow.saved ? h('div', { class: 'small', role: 'status' },
      `已保存「${flow.saved.name}」，这是为${unitName(flow.saved.recipe.unit)}做的配方。`,
      h('details', { class: 'lut-followup' }, h('summary', {}, '也想用于其他镜头单元？'),
        h('p', { class: 'dim' }, 'LUT 已留在库中。选择已校准的对应单元，再生成一份配方。'))) : null);
}

async function saveRecipe() {
  if (flow.busy || flow.saved) return;
  flow.busy = '正在保存…';
  redraw();
  try {
    flow.saved = await api('/api/my-recipes', { method: 'POST', body: { job_id: flow.job.job_id, candidate: flow.chosen, name: flow.name } });
    flow.recipes = (await api('/api/my-recipes')).recipes;
    emit(MY_RECIPES_CHANGED);
    toast(`已保存「${flow.saved.name}」 · 为${unitName(flow.saved.recipe.unit)}做的配方`, 'ok');
  } catch (e) {
    toastError(e, '没有存下这个配方。请重试。');
  } finally {
    flow.busy = null;
    redraw();
  }
}

// ---------------------------------------------------------------- 我的配方

function recipesSection() {
  const sec = h('section', { class: 'lut-card', dataset: { role: 'computer-recipes' } }, h('h3', {}, '我的配方 · 电脑上'),
    h('p', { class: 'small dim' }, '由 LUT 生成，存在这台电脑里。在大图的「配方」标签里点一下就能套到照片上。相机里自己存的配方不在这一页，在大图的「配方」标签里。'));
  if (!flow.recipes.length) {
    sec.append(h('p', { class: 'small dim' }, '电脑上还没有我的配方。用上面的 LUT 生成一个，存下来就会出现在这里。'));
    return sec;
  }
  for (const r of flow.recipes) sec.append(recipeCard(r));
  return sec;
}

function recipeCard(r) {
  const c = { amount: r.recipe.levels.amount ?? null, levels: r.recipe.levels };
  const sim = r.fit.similarity === null ? 'N/A' : `${Math.round(r.fit.similarity)}%`;
  const card = h('div', { class: 'recipe-card' });
  const del = h('button', { class: 'btn ghost sm', type: 'button', onclick: () => askRemove(r) }, '删除');
  const rename = h('button', { class: 'btn ghost sm', type: 'button', onclick: () => editName(r) }, '重命名');
  card.append(
    h('div', { class: 'recipe-head' }, h('b', {}, r.name), h('span', { class: 'small dim' }, ` · ${r.camera_short_name}`), h('span', { class: 'grow' }), rename, del),
    h('div', { class: 'small' }, `${unitName(r.recipe.unit)} · ${recipeLabel(r.recipe.unit, r.recipe)}（${describeLevels(c)}）`),
    h('div', { class: 'small dim' }, `${sourceText(r)} · 相似度 ${sim}`,
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
  emit(MY_RECIPES_CHANGED);
  redraw();
}

function editName(r) {
  const next = window.prompt('名称', r.name);
  if (next === null) return;
  api(`/api/my-recipes/${r.id}`, { method: 'PUT', body: { name: next } })
    .then((doc) => { flow.recipes = flow.recipes.map((x) => (x.id === doc.id ? doc : x)); emit(MY_RECIPES_CHANGED); redraw(); })
    .catch((e) => toastError(e, '没有改好名称。请重试。'));
}
