// 快捷键、设置、首次使用的四步引导。快捷键仍由 [?] 打开（COPY 3.8）。

import { api } from './api.js';
import { state, emit } from './state.js';
import { h, openModal, toast, fmtBytes, toastError, closeTopModal } from './dom.js';
import { calibrationImportForm, camdataSummary, cameraImportControls, loadCamdata } from './camdata.js';

export const GUIDE_STEPS = [
  ['插入存储卡', '自动导入照片'],
  ['在图库里浏览、编辑', '裁剪、曝光、边框'],
  ['用 LUT 库管理 LUT', '套用到照片'],
  ['导出', '可选 HDR'],
];

export function maybeShowOnboarding() {
  if (state.prefs && state.prefs.onboarding_dismissed === true) return;
  showGuide();
}

export function showHint() {}

export function clearHints() {
  document.getElementById('hints')?.replaceChildren();
}

function paintGuide(holder, step, onSkip) {
  const [title, text] = GUIDE_STEPS[step];
  const last = step === GUIDE_STEPS.length - 1;
  holder.replaceChildren(
    h('p', { class: 'small dim', dataset: { guide: 'count' } }, `${step + 1}/${GUIDE_STEPS.length}`),
    h('p', { dataset: { guide: 'title' } }, title),
    h('p', { class: 'small dim', dataset: { guide: 'text' } }, text),
    h(
      'div',
      { class: 'modal-foot' },
      h('button', { class: 'btn', type: 'button', style: { marginRight: 'auto' }, onclick: onSkip }, '跳过'),
      step > 0 ? h('button', { class: 'btn', type: 'button', onclick: () => paintGuide(holder, step - 1, onSkip) }, '上一步') : null,
      h('button', {
        class: 'btn primary',
        type: 'button',
        onclick: () => (last ? onSkip() : paintGuide(holder, step + 1, onSkip)),
      }, last ? '完成' : '下一步'),
    ),
  );
}

export function showGuide() {
  const holder = h('div', {});
  let close = null;
  const dismiss = async () => {
    try {
      state.prefs = await api('/api/prefs', { method: 'PUT', body: { onboarding_dismissed: true } });
      if (close) close();
    } catch (e) {
      toastError(e, '无法保存修改。请重试。');
    }
  };
  paintGuide(holder, 0, dismiss);
  close = openModal('引导', holder);
}

export function showHelpMenu() {
  openModal(
    '帮助',
    h(
      'div',
      { class: 'modal-foot', style: { justifyContent: 'flex-start', marginTop: '0' } },
      h('button', { class: 'btn', type: 'button', onclick: () => { closeTopModal(); showHelp(); } }, '快捷键'),
      h('button', { class: 'btn', type: 'button', onclick: () => { closeTopModal(); showGuide(); } }, '引导'),
    ),
  );
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
      { class: 'field' },
      h('span', { class: 'small dim' }, '引导'),
      h('button', { class: 'btn', type: 'button', onclick: () => { closeTopModal(); showGuide(); } }, '打开'),
    ),
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
