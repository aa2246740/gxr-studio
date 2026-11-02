// 持久化 LUT 库与照片编辑共用列表，所有名称都用文本节点显示。
import { api } from './api.js';
import { emit } from './state.js';
import { h, toast, toastError, openModal, confirmAction } from './dom.js';

let library = { luts: [], groups: [] };
let loaded = false;
const drafts = new Map();
let filter = '';
let starred = false;

export async function loadLutLibrary() {
  library = await api('/api/luts');
  loaded = true;
  return library;
}

export function librarySection({ select, onImport, selected, redraw, busy = false }) {
  const groupOptions = [['', '全部'], ['ungrouped', '未分组'], ...library.groups.map((g) => [g.id, g.name])];
  const groupSelect = h('select', { 'aria-label': '分组', onchange: (e) => { filter = e.target.value; redraw(); } },
    groupOptions.map(([value, text]) => h('option', { value, selected: filter === value }, text)));
  const section = h('section', { class: 'lut-card', dataset: { udCheck: 'lut-library' } },
    h('div', { class: 'recipe-head' }, h('h3', {}, 'LUT 库'), h('span', { class: 'grow' }),
      h('button', { class: 'btn primary', type: 'button', onclick: onImport }, '导入 LUT')),
    h('div', { class: 'field-row lut-tools' }, groupSelect,
      h('label', {}, h('input', { type: 'checkbox', checked: starred, onchange: (e) => { starred = e.target.checked; redraw(); } }), '已标星'),
      h('button', { class: 'btn', type: 'button', onclick: () => newGroup(redraw) }, '新建分组'),
      filter && filter !== 'ungrouped' ? h('button', { class: 'btn', type: 'button', onclick: () => renameGroup(redraw) }, '重命名') : null,
      filter && filter !== 'ungrouped' ? h('button', { class: 'btn', type: 'button', onclick: () => removeGroup(redraw) }, '删除分组') : null,
      filter ? h('button', { class: 'btn', type: 'button', onclick: () => exportGroup() }, '导出分组') : null));
  const rows = library.luts.filter((l) => (!starred || l.starred) && (!filter || l.group === (filter === 'ungrouped' ? null : filter)));
  if (!rows.length) section.append(h('p', { class: 'small dim' }, !loaded ? '正在读取…' : library.luts.length ? '没有符合条件的 LUT' : '还没有 LUT'));
  for (const lut of rows) {
    const group = library.groups.find((g) => g.id === lut.group)?.name || '未分组';
    const row = h('div', { class: 'lut-library-row' },
      h('button', { class: 'btn lut-library-name', type: 'button', 'aria-pressed': String(selected === lut.cube_id), onclick: () => select(lut) }, lut.name),
      h('span', { class: 'small dim lut-group-name' }, group),
      lut.technical ? h('span', { class: 'tag' }, '转换 LUT') : null,
      h('button', { class: 'btn small', type: 'button', 'aria-label': lut.starred ? '取消标星' : '标星', 'aria-pressed': String(lut.starred),
        onclick: () => change(lut.cube_id, { starred: !lut.starred }, redraw) }, lut.starred ? '已标星' : '标星'),
      h('button', { class: 'btn small', type: 'button', onclick: () => renameLut(lut, redraw) }, '重命名'),
      h('button', { class: 'btn small', type: 'button', onclick: () => exportLut(lut) }, '导出'),
      h('button', { class: 'btn small', type: 'button', onclick: () => removeLut(lut, redraw) }, '删除'));
    section.append(row);
  }
  if (busy) for (const control of section.querySelectorAll('button,input,select')) control.disabled = true;
  return section;
}

async function change(id, body, redraw) {
  try {
    const result = await api(`/api/luts/${id}`, { method: 'PATCH', body });
    emit('luts-changed'); // 显卡预览的 LUT 表作废（gpu_stage.js）
    await loadLutLibrary();
    redraw(result);
  } catch (e) { toastError(e, '无法完成操作。请重试。'); }
}

async function newGroup(redraw) {
  const name = window.prompt('分组名称');
  if (name === null) return;
  try {
    const group = await api('/api/lut-groups', { method: 'POST', body: { name } });
    filter = group.id;
    await loadLutLibrary();
    redraw();
  } catch (e) { toastError(e, '无法完成操作。请重试。'); }
}

async function renameGroup(redraw) {
  const group = library.groups.find((g) => g.id === filter);
  const name = window.prompt('分组名称', group.name);
  if (name === null) return;
  try {
    await api(`/api/lut-groups/${group.id}`, { method: 'PATCH', body: { name } });
    await loadLutLibrary();
    redraw();
  } catch (e) { toastError(e, '无法完成操作。请重试。'); }
}

function removeGroup(redraw) {
  const group = library.groups.find((g) => g.id === filter);
  confirmAction(`删除「${group.name}」？`, '删除', async () => {
    await api(`/api/lut-groups/${group.id}`, { method: 'DELETE' });
    filter = '';
    await loadLutLibrary();
    redraw();
  });
}

function renameLut(lut, redraw) {
  const name = window.prompt('名称', lut.name);
  if (name !== null) change(lut.cube_id, { name }, redraw);
}

function removeLut(lut, redraw) {
  confirmAction(`删除「${lut.name}」？`, '删除', async () => {
    await api(`/api/luts/${lut.cube_id}`, { method: 'DELETE' });
    emit('luts-changed');
    drafts.delete(lut.cube_id);
    await loadLutLibrary();
    redraw({ deleted: lut.cube_id });
  });
}

function exportReceipt(exports) {
  const body = h('div', { class: 'lut-export-receipt' }, h('div', {}, '导出到'), exports.map((r) => h('p', { class: 'small' }, r.path)));
  openModal(`已导出 ${exports.length} 个 LUT`, body);
}

async function exportLut(lut) {
  try { exportReceipt([await api(`/api/luts/${lut.cube_id}/export`, { method: 'POST' })]); }
  catch (e) { toastError(e, '无法完成操作。请重试。'); }
}

async function exportGroup() {
  try { exportReceipt((await api('/api/lut-groups/export', { method: 'POST', body: { group: filter === 'ungrouped' ? null : filter } })).exports); }
  catch (e) { toastError(e, '无法完成操作。请重试。'); }
}

export function attributionFields(lut, onSaved) {
  const inputs = {};
  const draft = drafts.get(lut.cube_id) || { group: lut.group || '', source: lut.source || '', author: lut.author || '', license: lut.license || '' };
  if (draft.group && !library.groups.some((g) => g.id === draft.group)) draft.group = lut.group || '';
  const form = h('form', { class: 'lut-attribution', dataset: { udCheck: 'lut-attribution' } });
  const group = h('select', { 'aria-label': 'LUT 分组', onchange: (e) => { draft.group = e.target.value; drafts.set(lut.cube_id, draft); } }, [['', '未分组'], ...library.groups.map((g) => [g.id, g.name])]
    .map(([value, text]) => h('option', { value, selected: value === draft.group }, text)));
  form.append(h('label', { class: 'field' }, h('span', {}, '分组'), group));
  for (const [key, text] of [['source', '来源'], ['author', '作者'], ['license', '许可']]) {
    inputs[key] = h('textarea', { value: draft[key], rows: 2, maxLength: 8192, 'aria-label': text,
      oninput: (e) => { draft[key] = e.target.value; drafts.set(lut.cube_id, draft); } });
    form.append(h('label', { class: 'field' }, h('span', {}, text), inputs[key]));
  }
  const button = h('button', { class: 'btn', type: 'submit' }, '存储');
  form.append(button);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    button.disabled = true;
    await change(lut.cube_id, { group: group.value || null, ...Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value])) }, (item) => {
      drafts.delete(lut.cube_id);
      toast('已存储', 'ok');
      onSaved(item);
    });
    button.disabled = false;
  });
  return form;
}

export function photoLutField(view, { beginChange, persist, endChange, schedulePreview }) {
  const select = h('select', { 'aria-label': 'LUT', onchange: () => {
    beginChange(view);
    view.ed.lut = select.value || null;
    persist(view, { lut: view.ed.lut }).then(() => endChange(view));
    schedulePreview(view, { full: true });
  } });
  const field = h('label', { class: 'field', dataset: { udCheck: 'photo-lut' } }, h('span', {}, 'LUT'), select);
  function fill(list) {
    const current = view.ed.lut || '';
    select.replaceChildren(h('option', { value: '', selected: !current }, '无'), ...list.luts.map((l) => h('option', {
      value: l.cube_id, selected: l.cube_id === current, disabled: !l.input_space || ((!view.photo.has_raw || !String(view.photo.lens || '').includes('50mm')) && !['srgb', 'rec709', 'gamma24'].includes(l.input_space)),
    }, l.input_space ? l.name : `${l.name} · 请选择输入色彩空间`)));
    if (current && !list.luts.some((l) => l.cube_id === current)) select.append(h('option', { value: current, selected: true }, 'LUT 已删除'));
  }
  fill(library);
  loadLutLibrary().then((list) => { if (field.isConnected) fill(list); }).catch((e) => toastError(e, '无法读取 LUT 库。请重试。'));
  return field;
}
