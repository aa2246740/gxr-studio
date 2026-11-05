// 「相机」页：只读。存储卡状态 + 每个单元（50mm / 28mm / MOUNT）的配方位置一览。
// 不写存储卡（没有写入入口，也不放点了没反应的按钮）。
// 没导入照片、没导入相机配方文件时，给出清楚的下一步。

import { h, icon } from './dom.js';
import { state } from './state.js';
import { cardPresent, importNow, isImporting } from './import.js';
import { loadCatalog, slotName } from './preview.js';
import { camdataGuide, camdataSummary, loadCamdata } from './camdata.js';

function cardSection() {
  const present = cardPresent();
  const label = state.card ? `「${state.card.label}」` : '';
  return h('section', { class: 'card-sec' },
    h('h2', { class: 'sec-title' }, '存储卡'),
    h('div', { class: 'cam-row' },
      icon('hard-drive'),
      h('div', { class: 'grow' },
        h('div', {}, present ? `已插入 ${label}` : '未插入'),
        h('div', { class: 'small dim' }, present ? '导入是只读复制，卡上的文件不会被改动。' : '插入 GXR 的存储卡或读卡器后，这里会显示它。')),
      present && !isImporting()
        ? h('button', { class: 'btn primary sm', type: 'button', onclick: () => importNow() }, '导入照片')
        : null),
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
  const units = h('div', { class: 'units', 'aria-busy': 'true' });
  const sum = h('div', {});
  const page = h('div', { class: 'page camera-page' },
    h('div', { class: 'page-inner' },
      h('h1', { class: 'page-title' }, '相机'),
      cardSection(),
      h('section', { class: 'card-sec' },
        h('h2', { class: 'sec-title' }, '配方位置一览'),
        h('p', { class: 'small dim' }, '每个单元在相机里的 8 个配方位置（只读）。相机里给配方改的名字读不出来，这里显示的是默认名。'),
        sum,
        units)));
  root.replaceChildren(page);
  const paintSummary = () => {
    const s = camdataSummary();
    sum.replaceChildren(h('dl', { class: 'kv' }, h('dt', {}, '相机配方文件'), h('dd', {}, s.camera), h('dt', {}, '校准数据'), h('dd', {}, s.calibration)));
    if (s.camera !== '已导入') sum.append(camdataGuide({ onDone: () => renderCamera(root, isCurrent) }));
  };
  loadCamdata().then(() => isCurrent() && paintSummary(), () => {});
  loadCatalog().then((cat) => {
    if (!isCurrent()) return;
    units.setAttribute('aria-busy', 'false');
    units.replaceChildren(...(cat.units || []).map(unitCard));
  }, (e) => {
    if (!isCurrent()) return;
    units.setAttribute('aria-busy', 'false');
    units.replaceChildren(h('p', { class: 'bad-text' }, e.message || '无法读取配方列表。'),
      h('button', { class: 'btn sm', type: 'button', onclick: () => renderCamera(root, isCurrent) }, '重试'));
  });
}
