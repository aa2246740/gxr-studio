// 快捷键表（?、顶栏键盘按钮）和设置（⌘,）。
// 原来的四步引导对话框删掉了：它一打开页面就弹，挡住还没导入的空图库（U9）。
// 第一次使用的说明改放在图库的空状态里（gallery.js 的 emptyLibrary），没有照片时才出现，导入后自然消失。

import { api } from './api.js';
import { state, emit } from './state.js';
import { h, openModal, toast, fmtBytes, platform, errorBody } from './dom.js';
import { calibrationImportForm, camdataSummary, cameraImportControls, loadCamdata } from './camdata.js';
import { toggle } from './controls.js';
import { gpuPreference, setGpuPreference } from './gpu_pref.js';
import { busyButton, delayed } from './busy.js';

export { gpuPreference };

// 快捷键（按平台写法）：Mac 用 ⌘ ⇧，Windows 用 Ctrl+ Shift+。浏览器自己占用的组合键不用。
export function shortcutGroups(p = platform()) {
  const mod = p === 'mac' ? '⌘' : 'Ctrl+';
  const shift = p === 'mac' ? '⇧' : 'Shift+';
  return [
    ['全局', [
      [`${mod}E`, '导出'],
      [`${mod},`, '设置'],
      ['?', '快捷键'],
      ['Esc', '关闭对话框 / 回到图库'],
    ]],
    ['图库', [
      ['← → ↑ ↓', '移动焦点'],
      ['Enter', '打开照片'],
      ['空格', '选择 / 取消选择'],
      [`${mod}点击`, '加选 / 取消一张'],
      [`${shift}点击`, '连续选择'],
      [`${mod}A`, '全选'],
    ]],
    ['编辑', [
      ['R / A / C / F / T / I', '配方 / 调整 / 裁剪 / 边框 / 时间戳 / 信息'],
      ['← / →', '上一张 / 下一张'],
      ['按住空格', '临时查看原图'],
      ['\\', '原图 / 编辑后'],
      ['Y', '左右对比'],
      ['Z', '适合 / 100%'],
      [`${mod}Z`, '撤销'],
      [p === 'mac' ? '⇧⌘Z' : 'Ctrl+Y', '重做'],
      ['双击滑块', '重置这一项'],
    ]],
    ['裁剪', [
      ['↑ / ↓', '拉直（每次 0.5°）'],
      ['O', '换参考线'],
      [`${shift}O`, '螺旋方向'],
      ['[', '向左旋转'],
    ]],
  ];
}

export function showHelp() {
  const groups = shortcutGroups();
  const body = h('div', { class: 'keys-grid' }, groups.map(([title, rows]) => h('section', {},
    h('h3', { class: 'keys-group' }, title),
    h('table', { class: 'keys' }, rows.map(([k, v]) => h('tr', {}, h('td', {}, h('kbd', {}, k)), h('td', {}, v)))))));
  openModal('快捷键', body, { wide: true });
}

// 顶栏的键盘按钮直接打开快捷键表（原来多一层「帮助」菜单，里面只有快捷键和引导）
export const showHelpMenu = showHelp;

function settingsBody(s) {
  const undated = s.undated_policy === 'unknown_folder' ? '放进「未知日期」' : '使用文件修改日期';
  const cameraLine = h('span', {});
  const calibrationLine = h('span', {});
  const paintCamdata = () => {
    const sum = camdataSummary();
    cameraLine.textContent = sum.camera;
    calibrationLine.textContent = sum.calibration;
  };
  paintCamdata();
  // 重建索引要把图库整个读一遍：点了锁住，150ms 后按钮里写「正在重建…」，失败时按钮旁边给「重试」
  const rebuild = h('button', {
    class: 'btn',
    type: 'button',
    onclick: () => busyButton(rebuild, async () => {
      await api('/api/library/rebuild', { method: 'POST' });
      toast('已重建索引', 'ok');
      emit('library-changed');
    }, { busyText: '正在重建…' }),
  }, h('span', {}, '重建索引'));
  const gpuAvailable = s.gpu_preview === true;
  const gpuRow = toggle('显卡实时预览', gpuAvailable && gpuPreference(), (on) => {
    setGpuPreference(on);
    toast(on ? '已打开显卡实时预览' : '已关闭显卡实时预览，改用服务器出图', 'info');
  }, { disabled: !gpuAvailable });
  loadCamdata().then(paintCamdata, () => {});
  return h(
    'div',
    { class: 'settings' },
    h('section', { class: 'section' }, h('h3', { class: 'section-head' }, '照片库'),
      h('dl', { class: 'kv' },
        h('dt', {}, '位置'), h('dd', { class: 'num path' }, s.library_dir),
        h('dt', {}, '导出到'), h('dd', { class: 'num path' }, s.export_dir),
        h('dt', {}, '照片'), h('dd', { class: 'num' }, `${s.photo_count} 张 · 可用空间 ${fmtBytes(s.free_bytes)}`),
        h('dt', {}, '没有拍摄时间'), h('dd', {}, undated)),
      h('div', { class: 'field-line' }, rebuild, h('span', { class: 'small dim' }, '照片显示不对时用：按照片库里的文件重新建立列表。'))),
    h('section', { class: 'section' }, h('h3', { class: 'section-head' }, '预览'),
      gpuRow,
      h('p', { class: 'small dim' }, gpuAvailable ? '拖滑块时由显卡直接出图。画面不对时可以关掉，改由电脑端出图。' : '这台电脑的 GXR Studio 没有打开显卡预览。')),
    h('section', { class: 'section', dataset: { section: 'camdata-status' } }, h('h3', { class: 'section-head' }, '相机配方文件与校准数据'),
      h('dl', { class: 'kv' }, h('dt', {}, '相机配方文件'), h('dd', {}, cameraLine), h('dt', {}, '校准数据'), h('dd', {}, calibrationLine)),
      h('div', { class: 'field', dataset: { section: 'camdata-import' } }, cameraImportControls({ onDone: paintCamdata })),
      calibrationImportForm({ onDone: paintCamdata })),
  );
}

// 设置：先打开对话框（骨架占位），数据到了再填，不再等请求回来才有反应
export function showSettings() {
  const holder = h('div', {});
  openModal('设置', holder, { wide: true });
  return loadSettings(holder);
}

// 启动时已经读过设置：先用手上的这份马上画出来，后台再读一次，有变化才重画（不出骨架、不闪）。
// 没有现成的才等接口；150ms 内回来不显示骨架。
async function loadSettings(holder) {
  const cached = state.settings;
  if (cached) holder.replaceChildren(settingsBody(cached));
  holder.setAttribute('aria-busy', 'true');
  const skel = cached ? null : delayed(() => holder.replaceChildren(h('div', { class: 'skel line' }), h('div', { class: 'skel line' }), h('div', { class: 'skel line' })));
  try {
    const s = await api('/api/settings');
    state.settings = s;
    if (!cached || JSON.stringify(s) !== JSON.stringify(cached)) holder.replaceChildren(settingsBody(s));
  } catch (e) {
    if (!cached) {
      holder.replaceChildren(
        h('p', { class: 'bad-text' }, errorBody(e, '无法读取设置。')),
        h('button', { class: 'btn', type: 'button', onclick: () => loadSettings(holder) }, '重试'),
      );
    }
  } finally {
    skel?.cancel();
    holder.setAttribute('aria-busy', 'false');
  }
}
