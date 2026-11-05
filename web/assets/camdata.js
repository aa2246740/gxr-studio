// 相机数据（装有配方程序的 GXRCE.DAT）和颜色标定的状态，以及"还没导入"时的引导。
// 状态读 /api/camdata/status；万一这个接口不可用，退回 /api/render/status（只有 ready 和两项状态）。
// 导入走 /api/camdata/import-card、/api/camdata/import-upload（选中的文件的原始字节）、/api/calibration/import（开发用）。

import { api } from './api.js';
import { state, emit } from './state.js';
import { h, toast } from './dom.js';

// 第 135 代起的旋钮是新版（每项 ±9，Amount 0–150%）；更早的版本只认 ±5，Fade/Warm/Tint/Split/Filt 相机里没有
const NEW_KNOBS_FROM = 135;

export function applyCamdataStatus(s) {
  const prev = state.camdata;
  if (!s) {
    state.camdata = { ...(prev || {}), ready: false, state: 'unknown', message: '无法读取相机配方文件。请重试。', generation: null };
    return state.camdata;
  }
  const cd = s.camera_data || {};
  const cal = s.calibration || {};
  const ready = typeof s.ready === 'boolean' ? s.ready : cd.state === 'ready' && (cal.state === undefined || cal.state === 'ready');
  const dataVersion = s.data_version ?? prev?.dataVersion ?? null;
  state.camdata = {
    ready,
    state: cd.state || s.state || (ready ? 'ready' : 'missing'),
    message: cd.message || s.message || null,
    generation: cd.generation ?? s.generation ?? null,
    calibration: cal.state || (ready ? 'ready' : null),
    calibrationMessage: cal.message || null,
    next: s.next ?? null,
    cards: s.cards || prev?.cards || [],
    projectDir: s.project_dir ?? prev?.projectDir ?? null,
    dataVersion,
  };
  // 数据换了：没有 JPG 的照片的图片地址带着新的 ?v=，图库和大图要重新取一次照片信息
  if (prev?.dataVersion != null && dataVersion != null && prev.dataVersion !== dataVersion) {
    state.catalog = null;
    emit('camdata-changed');
  }
  return state.camdata;
}

export async function loadCamdata() {
  let s = null;
  try {
    s = await api('/api/camdata/status');
  } catch {
    try {
      s = await api('/api/render/status');
    } catch {
      s = null;
    }
  }
  return applyCamdataStatus(s);
}

export function camdataReady() {
  return state.camdata?.ready === true;
}

function cameraDataImported() {
  return state.camdata?.state === 'ready';
}

// 设置里的一行行摘要
export function camdataSummary() {
  const c = state.camdata;
  const camera = cameraDataImported() ? '已导入' : '未导入';
  const calibration = c?.calibration === 'ready' ? '已导入' : c?.calibration === 'invalid' ? '需要重新导入' : '未导入';
  return { camera, calibration };
}

// 第 135 代之前的相机版本不明（没导入）时按旧版处理：标签更保守，不会把"相机能做"说成确定的事
export function isLegacyCamera() {
  const gen = state.camdata?.generation;
  return !(typeof gen === 'number' && gen >= NEW_KNOBS_FROM);
}

// 大白话引导。onDone 在导入成功后调用（让界面重新读取预览）。
// 相机数据还没有 → 从卡导入 / 选文件；相机数据有了、颜色标定还没有 → 指到「设置」里的开发用入口
export function camdataGuide({ onDone } = {}) {
  if (cameraDataImported()) return calibrationGuide();
  const problem = state.camdata?.state;
  const message = ['unsupported', 'invalid', 'unknown'].includes(problem) ? state.camdata.message : '未导入相机配方文件';
  return h(
    'div',
    { class: 'camdata-guide', role: 'region', 'aria-label': '未导入相机配方文件' },
    h('h3', {}, message),
            cameraImportControls({ onDone }),
  );
}

// 「从卡导入」和「选择 GXRCE.DAT 文件…」两个入口（引导里用，设置里也放一份：换了新版配方程序要重新导入）
export function cameraImportControls({ onDone } = {}) {
  const msg = h('p', { class: 'small', role: 'status' });
  const choices = h('div', { class: 'guide-actions' });
  const busy = (on) => {
    for (const b of box.querySelectorAll('button')) b.disabled = on;
  };
  const finish = async (r) => {
    toast('已导入相机配方文件', 'ok');
    msg.textContent = r?.message || '已导入相机配方文件';
    choices.replaceChildren();
    await loadCamdata();
    if (onDone) await onDone();
  };
  const fileInput = h('input', {
    type: 'file',
    accept: '.DAT,.dat',
    hidden: true,
    'aria-label': '选择相机配方文件',
    onchange: async (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      busy(true);
      msg.textContent = '正在导入…';
      try {
        // 浏览器拿不到本地路径，所以直接把文件的字节交给工作室（它只校验、提取，不改你的文件）
        await finish(await api('/api/camdata/import-upload', { method: 'POST', body: file, headers: { 'X-GXR-Filename': file.name } }));
      } catch (err) {
        msg.textContent = err.message;
        if (err.code === 'generation_unsupported') {
          await loadCamdata();
          if (state.camdata?.state === 'unsupported' && onDone) await onDone();
        }
      } finally {
        e.target.value = '';
        busy(false);
      }
    },
  });
  const importCard = async (cardId) => {
    busy(true);
    choices.replaceChildren();
    msg.textContent = '正在读取存储卡…';
    try {
      await finish(await api('/api/camdata/import-card', { method: 'POST', body: cardId ? { card_id: cardId } : {} }));
    } catch (err) {
      msg.textContent = err.message;
      if (err.code === 'generation_unsupported') {
        await loadCamdata();
        if (state.camdata?.state === 'unsupported' && onDone) await onDone();
      }
      if (err.code === 'multiple_cards' && err.data?.cards) {
        choices.replaceChildren(...err.data.cards.map((c) => h('button', { class: 'btn', type: 'button', onclick: () => importCard(c.id) }, `从「${c.label}」导入`)));
      }
    } finally {
      busy(false);
    }
  };
  const box = h(
    'div',
    { class: 'camdata-import' },
    h('div', { class: 'guide-actions' }, h('button', { class: 'btn primary', type: 'button', onclick: () => importCard() }, '从存储卡导入配方')),
        h('div', { class: 'guide-actions' }, h('button', { class: 'btn', type: 'button', onclick: () => fileInput.click() }, '选择文件…'), fileInput),
    choices,
    msg,
  );
  return box;
}

function calibrationGuide() {
  const c = state.camdata;
  const invalid = c.calibration === 'invalid';
  return h(
    'div',
    { class: 'camdata-guide', role: 'region', 'aria-label': '未导入校准数据' },
    h('h3', {}, invalid ? '校准数据与相机配方文件不匹配' : '未导入校准数据'),
    invalid && c.calibrationMessage ? h('details', { class: 'small' }, h('summary', {}, '详细信息'), h('p', {}, c.calibrationMessage)) : null,
        h('div', { class: 'guide-actions' }, h('button', { class: 'btn', type: 'button', onclick: () => emit('open-settings') }, '导入校准数据')),
  );
}

// 「设置」里的入口：把开发机上的标定数据导进来。大白话说清楚"现在只有开发者能用"
export function calibrationImportForm({ onDone } = {}) {
  // 工作室就在项目文件夹里：输入框直接预填项目文件夹，不用手敲（COPY 3.10：标签「校准数据文件夹」）
  const input = h('input', {
    type: 'text',
    'aria-label': '校准数据文件夹',
    value: state.camdata?.projectDir || '',
    class: 'wide-input',
  });
  const msg = h('p', { class: 'small', role: 'status' });
  const button = h('button', { class: 'btn', type: 'button', onclick: run }, '导入校准数据');
  async function run() {
    const path = input.value.trim();
    if (!cameraDataImported()) {
      msg.textContent = '请先导入相机配方文件';
      return;
    }
    if (!path) {
      msg.textContent = '请输入完整路径';
      return;
    }
    button.disabled = true;
    msg.textContent = '正在导入…';
    try {
      const r = await api('/api/calibration/import', { method: 'POST', body: { path } });
      msg.textContent = r.message;
      toast('已导入校准数据', 'ok');
      await loadCamdata();
      if (onDone) onDone();
    } catch (err) {
      msg.textContent = err.message;
    } finally {
      button.disabled = false;
    }
  }
  return h(
    'div',
    { class: 'field', dataset: { section: 'calibration' } },
    h('strong', {}, '校准数据'),
    h('span', { class: 'small dim' }, '校准数据文件夹'),
    input,
    h('div', { class: 'guide-actions' }, button),
    msg,
  );
}
