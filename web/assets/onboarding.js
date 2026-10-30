// 新手引导：第一次打开的三张卡（可跳过，之后在 ? 里重看）；标签第一次出现时的小提示（关掉就不再出现）；
// 快捷键、设置。

import { api } from './api.js';
import { state, emit } from './state.js';
import { h, openModal, toast, fmtBytes } from './dom.js';
import { calibrationImportForm, camdataSummary, cameraImportControls, loadCamdata } from './camdata.js';

const ONBOARD_KEY = 'gxr.onboarding.v1';
const HINT_PREFIX = 'gxr.hint.v1.';

const CARDS = [
  { title: '插上卡，照片自动进来', body: '把相机的存储卡插进电脑的读卡器，照片会自动读进来，按拍摄日期排好。卡上的文件一个都不会改。' },
  { title: '点一张，裁一裁', body: '点开一张照片，可以裁成 XPan 全景条或其他比例，也可以拉直。裁切随时能还原，原图不动。' },
  { title: '调好了，导出 JPEG', body: '在右侧的「导出」里选尺寸和质量，就能存成 JPEG。想带回相机用，去「相机」页（那一部分还没做好）。' },
];

export function maybeShowOnboarding() {
  if (!localStorage.getItem(ONBOARD_KEY)) showOnboarding();
}

export function showOnboarding() {
  let i = 0;
  let close = null;
  const finish = () => {
    localStorage.setItem(ONBOARD_KEY, '1');
    close && close();
  };
  const render = () => {
    const card = CARDS[i];
    const last = i === CARDS.length - 1;
    body.replaceChildren(
      h('p', { class: 'dim small' }, `第 ${i + 1} 步，共 ${CARDS.length} 步`),
      h('h2', {}, card.title),
      h('p', {}, card.body),
      h(
        'div',
        { class: 'modal-foot' },
        h('button', { class: 'btn ghost', type: 'button', onclick: finish }, '跳过'),
        i > 0 ? h('button', { class: 'btn', type: 'button', onclick: () => { i--; render(); } }, '上一步') : null,
        h('button', { class: 'btn primary', type: 'button', onclick: last ? finish : () => { i++; render(); } }, last ? '开始使用' : '下一步'),
      ),
    );
  };
  const body = h('div', {});
  close = openModal('欢迎使用 GXR 电脑工作室', body, { onClose: () => localStorage.setItem(ONBOARD_KEY, '1') });
  render();
}

// 第一次出现的标签给一句小字提示（放在顶部的提示条里，不挡住底部的按钮）；关掉就不再出现
export function showHint(key, text) {
  if (localStorage.getItem(HINT_PREFIX + key)) return;
  const slot = document.getElementById('hints');
  if (!slot || slot.querySelector(`[data-hint="${key}"]`)) return;
  const el = h(
    'div',
    { class: 'hint-strip', dataset: { hint: key }, role: 'note' },
    h('span', {}, text),
    h('button', {
      class: 'btn ghost',
      type: 'button',
      title: '关掉，以后不再显示',
      'aria-label': '关掉提示',
      onclick: () => {
        localStorage.setItem(HINT_PREFIX + key, '1');
        el.remove();
      },
    }, '✕'),
  );
  slot.append(el);
}

// 切换页面时收起当前页面的提示（没关掉的下次进入同一页面还会出现）
export function clearHints() {
  document.getElementById('hints')?.replaceChildren();
}

export function showHelp() {
  const rows = [
    ['← / →', '上一张 / 下一张（大图）'],
    ['Space（按住）', '看原图，松开恢复'],
    ['Enter', '打开最近点过的照片（图库）'],
    ['Esc', '关闭弹窗；大图里返回图库'],
    ['C / R / A / E', '裁切 / 配方 / 调整 / 导出 标签（大图）'],
    ['↑ / ↓', '拉直微调 0.5°（裁切标签）'],
    ['G / O', '构图辅助线切换 / 黄金螺旋方向（裁切标签）'],
    ['⌘ Z', '撤销上一步裁切'],
    ['⌘ E', '导出（当前照片或选中的照片）'],
    ['⌘ ,', '设置'],
    ['?', '显示这张表'],
  ];
  const body = h(
    'div',
    {},
    h('table', { class: 'keys' }, rows.map(([k, v]) => h('tr', {}, h('td', {}, h('kbd', {}, k)), h('td', {}, v)))),
    h(
      'div',
      { class: 'modal-foot' },
      h('button', { class: 'btn', type: 'button', onclick: () => { showOnboarding(); } }, '重看新手引导'),
    ),
  );
  openModal('快捷键与帮助', body);
}

export async function showSettings() {
  let s;
  try {
    s = await api('/api/settings');
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  state.settings = s;
  const undated = s.undated_policy === 'unknown_folder'
    ? '没有拍摄时间的照片放进「未知日期」'
    : '没有拍摄时间的照片按文件的修改时间归档（标了「日期是估计的」）';
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
    h('p', {}, h('span', { class: 'dim' }, '导出文件夹：'), h('code', {}, s.export_dir)),
    h('p', { class: 'small dim' }, '要改存放位置，需要重新启动工作室并设置对应的环境变量（见 README）。'),
    h('p', {}, `照片 ${s.photo_count} 张 · 电脑剩余空间 ${fmtBytes(s.free_bytes)}`),
    h('p', { class: 'small' }, undated),
    h('p', { dataset: { section: 'camdata-status' } }, h('span', { class: 'dim' }, '相机数据：'), cameraLine, h('span', { class: 'dim' }, '　颜色标定：'), calibrationLine),
    h(
      'div',
      { class: 'field', dataset: { section: 'camdata-import' } },
      h('strong', {}, '相机数据'),
      h('p', { class: 'small dim' }, '换了新版的配方程序（刷过新固件）时，在这里重新导入。插好卡点「从卡导入」，或选一个 GXRCE.DAT 文件。'),
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
            const r = await api('/api/library/rebuild', { method: 'POST' });
            toast(`索引已重建：${r.shots} 张照片`, 'ok');
            emit('library-changed');
          } catch (e) {
            toast(e.message, 'error');
          }
        },
      }, '重建照片索引'),
    ),
  );
  openModal('设置', body, { wide: true });
}
