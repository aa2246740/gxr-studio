// 快捷键表和设置。COPY.md 3.8：快捷键集中放在 [?] 打开的表里，面板里不写；不做欢迎引导和标签提示（已去掉）。
// showHint / maybeShowOnboarding / clearHints 保留为空函数，只为了让引用它们的旧代码仍然能加载。

import { api } from './api.js';
import { state, emit } from './state.js';
import { h, openModal, toast, fmtBytes, toastError } from './dom.js';
import { calibrationImportForm, camdataSummary, cameraImportControls, loadCamdata } from './camdata.js';

export function maybeShowOnboarding() {}

export function showHint() {}

export function clearHints() {
  document.getElementById('hints')?.replaceChildren();
}

export function showHelp() {
  const rows = [
    ['R / A / C / F / T / E', '配方 / 调整 / 裁剪 / 边框 / 时间戳 / 导出'],
    ['← / →', '上一张 / 下一张'],
    ['按住空格', '查看原图'],
    ['↑ / ↓', '拉直（每次 0.5°）'],
    ['G / O', '参考线 / 螺旋方向'],
    ['[', '向左旋转'],
    ['双击滑块', '重置'],
    ['⌘ Z', '撤销'],
    ['⌘ E', '导出'],
    ['⌘ ,', '设置'],
    ['Enter / Esc', '打开 / 关闭'],
    ['?', '快捷键'],
  ];
  const body = h('div', {}, h('table', { class: 'keys' }, rows.map(([k, v]) => h('tr', {}, h('td', {}, h('kbd', {}, k)), h('td', {}, v)))));
  openModal('快捷键', body);
}

export async function showSettings() {
  let s;
  try {
    s = await api('/api/settings');
  } catch (e) {
    toastError(e, '无法完成操作。请重试。');
    return;
  }
  state.settings = s;
  const undated = s.undated_policy === 'unknown_folder' ? '未知日期' : '使用文件修改日期';
  await loadCamdata();
  const cameraLine = h('span', {});
  const calibrationLine = h('span', {});
  const paintCamdata = () => {
    const sum = camdataSummary();
    cameraLine.textContent = sum.camera;
    calibrationLine.textContent = sum.calibration;
  };
  paintCamdata();
  const body = h(
    'div',
    {},
    h('p', {}, h('span', { class: 'dim' }, '照片库：'), h('code', {}, s.library_dir)),
    h('p', {}, h('span', { class: 'dim' }, '导出到：'), h('code', {}, s.export_dir)),
    h('p', {}, `${s.photo_count} 张照片 · 可用空间 ${fmtBytes(s.free_bytes)}`),
    h('p', { class: 'small' }, undated),
    h('p', { dataset: { section: 'camdata-status' } }, h('span', { class: 'dim' }, '相机配方文件：'), cameraLine, h('span', { class: 'dim' }, '　校准数据：'), calibrationLine),
    h(
      'div',
      { class: 'field', dataset: { section: 'camdata-import' } },
      h('strong', {}, '相机配方文件'),
      cameraImportControls({ onDone: paintCamdata }),
    ),
    calibrationImportForm({ onDone: paintCamdata }),
    h(
      'div',
      { class: 'modal-foot' },
      h('button', {
        class: 'btn',
        type: 'button',
        onclick: async () => {
          try {
            await api('/api/library/rebuild', { method: 'POST' });
            toast('已重建索引', 'ok');
            emit('library-changed');
          } catch (e) {
            toastError(e, '无法完成操作。请重试。');
          }
        },
      }, '重建索引'),
    ),
  );
  openModal('设置', body, { wide: true });
}
