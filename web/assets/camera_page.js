// 「相机」页：只读。存储卡状态 + 每个单元（50mm / 28mm / MOUNT）的配方位置一览。
// 不写存储卡（没有写入入口，也不放点了没反应的按钮）。
// 没导入照片、没导入相机配方文件时，给出清楚的下一步。

import { h, icon, setFraction, platform } from './dom.js';
import { state, bus, on } from './state.js';
import { cardPresent, importNow, isImporting, cancelImport } from './import.js';
import { ejectWords, progressLine, summarizeImport } from './import_summary.js';
import { loadCatalog, slotName } from './preview.js';
import { camdataGuide, camdataSummary, loadCamdata } from './camdata.js';
import { delayed } from './busy.js';

// 这张卡上次导入的结果（一句话）：提示条消失以后这里还看得到
function lastResult() {
  const job = state.job;
  const p = job && !job.running ? job.progress : null;
  if (!p || !state.card || p.card_id !== state.card.id || p.state === 'scanning') return null;
  const r = summarizeImport(p);
  return r.text ? ejectWords(r.text, platform()) : null;
}

// 导入按钮这一行：导入中显示进度和「取消导入」，否则是「导入照片」；结束后留一句结果
function importControls() {
  if (isImporting()) {
    const line = progressLine(state.job.progress);
    const bar = h('i', {});
    setFraction(bar, line.fraction ?? 0);
    return h('div', { class: 'cam-import', dataset: { role: 'import-progress' } },
      h('div', { class: 'small' }, line.text),
      h('div', { class: `progress${line.fraction === null ? ' indet' : ''}` }, bar),
      h('button', { class: 'btn sm', type: 'button', onclick: cancelImport }, '取消导入'));
  }
  return h('button', { class: 'btn primary sm', type: 'button', onclick: () => importNow() }, '导入照片');
}

function cardSection(isCurrent) {
  const action = h('div', { class: 'cam-import-slot' });
  const result = h('div', { class: 'small dim', role: 'status' });
  const title = h('div', {});
  const note = h('div', { class: 'small dim' });
  const paint = () => {
    const present = cardPresent();
    title.textContent = present ? `已插入「${state.card.label}」` : '未插入';
    note.textContent = present ? '导入是只读复制，卡上的文件不会被改动。' : '插入 GXR 的存储卡或读卡器后，这里会显示它。';
    action.replaceChildren(...(present ? [importControls()] : []));
    const text = present && !isImporting() ? lastResult() : null;
    result.textContent = text || '';
    result.hidden = !text;
  };
  paint();
  // 导入状态一有变化（开始、进度、结束、插拔卡）就重画这一块；页面换走了就不再听
  const listener = () => {
    if (!isCurrent() || !action.isConnected) bus.removeEventListener('import-state', listener);
    else paint();
  };
  on('import-state', listener);
  return h('section', { class: 'card-sec' },
    h('h2', { class: 'sec-title' }, '存储卡'),
    h('div', { class: 'cam-row' },
      icon('hard-drive'),
      h('div', { class: 'grow' }, title, note, result),
      action),
    h('p', { class: 'small dim' }, '暂不支持写入存储卡：GXR Studio 不会改动相机或存储卡上的任何文件。'));
}

function unitCard(unit) {
  const recipes = unit.recipes || [];
  return h('section', { class: 'card-sec unit' },
    h('div', { class: 'unit-head' },
      h('h3', {}, unit.label || unit.lens || unit.id),
      unit.calibrated ? h('span', { class: 'tag soft', title: '电脑预测用这个单元自己的颜色模型' }, '专用颜色模型') : h('span', { class: 'tag soft warn', title: '这个单元还没有自己的颜色模型，电脑预测暂用 A12 50mm 的' }, '借用 50mm 颜色模型')),
    h('ol', { class: 'slots' }, recipes.map((r) => h('li', { class: r.is_stock ? 'stock' : '' },
      h('span', { class: 'num' }, r.number || String(r.slot).padStart(2, '0')),
      h('span', { class: 'nm' }, slotName(unit.id, r.slot).slice(3)),
      r.is_stock ? h('span', { class: 'faint small' }, '原厂') : null))),
    (unit.looks && unit.looks.length) ? h('p', { class: 'small dim' }, `另有 ${unit.looks.length} 个 Looks`) : null);
}

export function renderCamera(root, isCurrent = () => true) {
  // 导入相机配方文件是异步的，完成时用户可能已经去了别的页面：那时不能把相机页画回共享的 root（F05）
  if (!isCurrent()) return;
  const units = h('div', { class: 'units', 'aria-busy': 'true' });
  const sum = h('div', {});
  const page = h('div', { class: 'page camera-page' },
    h('div', { class: 'page-inner' },
      h('h1', { class: 'page-title' }, '相机'),
      cardSection(isCurrent),
      h('section', { class: 'card-sec' },
        h('h2', { class: 'sec-title' }, '配方位置一览'),
        h('p', { class: 'small dim' }, '每个单元在相机里的 8 个配方位置（只读）。相机里给配方改的名字读不出来，这里显示的是默认名。'),
        sum,
        units)));
  root.replaceChildren(page);
  const skel = delayed(() => units.replaceChildren(...Array.from({ length: 3 }, () => h('div', { class: 'card-sec unit skel', 'aria-hidden': 'true' }))));
  const paintSummary = () => {
    const s = camdataSummary();
    sum.replaceChildren(h('dl', { class: 'kv' }, h('dt', {}, '相机配方文件'), h('dd', {}, s.camera), h('dt', {}, '校准数据'), h('dd', {}, s.calibration)));
    if (s.camera !== '已导入') sum.append(camdataGuide({ onDone: () => { if (isCurrent()) renderCamera(root, isCurrent); } }));
  };
  loadCamdata().then(() => isCurrent() && paintSummary(), () => {});
  loadCatalog().then((cat) => {
    skel.cancel();
    if (!isCurrent()) return;
    units.setAttribute('aria-busy', 'false');
    units.replaceChildren(...(cat.units || []).map(unitCard));
  }, (e) => {
    skel.cancel();
    if (!isCurrent()) return;
    units.setAttribute('aria-busy', 'false');
    units.replaceChildren(h('p', { class: 'bad-text' }, e.message || '无法读取配方列表。'),
      h('button', { class: 'btn sm', type: 'button', onclick: () => { if (isCurrent()) renderCamera(root, isCurrent); } }, '重试'));
  });
}
