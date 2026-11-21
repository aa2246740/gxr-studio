// 桌面端「导出关机图」：编辑器导出下拉菜单 + 640×480 关机图预览与导出对话框。

import { api } from './api.js';
import { state } from './state.js';
import { h, button, openModal, fmtBytes, toast, toastError, errorBody } from './dom.js';
import { busyButton } from './busy.js';
import { flushPhotos } from './saves.js';
import { exportDestination } from './export_destination.js';
import { revealLabel } from './export.js';

export const NOTE_CAMERA_COUNT = '相机会把当天张数叠在关机画面上，下部别放重要内容。';
export const NOTE_CARD_ROOT = '把 GXROFF.JPG 拷到存储卡最外层（和 GXRCE.DAT 放在一起）。';

let exportMenuBtn = null;
let exportMenuEl = null;
let menuListenersBound = false;

function closeExportMenu() {
  if (exportMenuEl) {
    exportMenuEl.remove();
    exportMenuEl = null;
  }
  if (exportMenuBtn) exportMenuBtn.setAttribute('aria-expanded', 'false');
}

function bindMenuGlobalListeners() {
  if (menuListenersBound) return;
  menuListenersBound = true;
  document.addEventListener('pointerdown', (e) => {
    if (!exportMenuEl) return;
    if (exportMenuEl.contains(e.target)) return;
    if (exportMenuBtn && exportMenuBtn.contains(e.target)) return;
    closeExportMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && exportMenuEl) {
      e.preventDefault();
      e.stopPropagation();
      closeExportMenu();
      exportMenuBtn?.focus();
    }
  }, true);
}

// 编辑器顶栏「导出」按钮旁的下拉菜单：保留「导出」直达常规导出，展开可见「做成关机图…」。
export function mountEditorExportMenu(view) {
  unmountEditorExportMenu();
  const topBtn = document.getElementById('btn-export-top');
  if (!topBtn || !topBtn.parentElement) return;
  bindMenuGlobalListeners();

  const btn = button('', {
    kind: 'icon',
    ico: 'chevron-down',
    title: '更多导出方式',
    cls: 'export-menu-btn',
    onclick: () => {
      if (exportMenuEl) {
        closeExportMenu();
        return;
      }
      openMenu(btn, view);
    },
  });
  btn.id = 'btn-export-menu';
  btn.setAttribute('aria-haspopup', 'menu');
  btn.setAttribute('aria-expanded', 'false');
  topBtn.insertAdjacentElement('afterend', btn);
  exportMenuBtn = btn;
}

export function unmountEditorExportMenu() {
  closeExportMenu();
  if (exportMenuBtn) {
    exportMenuBtn.remove();
    exportMenuBtn = null;
  }
}

function openMenu(anchor, view) {
  closeExportMenu();
  const rect = anchor.getBoundingClientRect();
  const menuWidth = 200;
  const left = Math.max(8, Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - 8));
  const top = rect.bottom + 6;
  const itemExport = h('button', {
    type: 'button',
    class: 'mi',
    role: 'menuitem',
    dataset: { act: 'export-photo' },
    onclick: () => {
      closeExportMenu();
      document.getElementById('btn-export-top')?.click();
    },
  }, '导出照片…');
  const itemOff = h('button', {
    type: 'button',
    class: 'mi',
    role: 'menuitem',
    dataset: { act: 'export-off-image' },
    onclick: () => {
      closeExportMenu();
      openOffImageDialog(view);
    },
  }, '做成关机图…');
  const menu = h('div', {
    class: 'menu export-menu',
    role: 'menu',
    'aria-label': '导出菜单',
    style: { left: `${left}px`, top: `${top}px` },
  }, itemExport, itemOff);
  document.body.append(menu);
  exportMenuEl = menu;
  anchor.setAttribute('aria-expanded', 'true');
  itemOff.focus();
}

// 浏览器能解的图片（如 macOS/Safari 下的 HEIC）在 Pillow 不支持时转成无损 PNG 再送后端。
async function decodeViaBrowserToPng(file) {
  if (typeof createImageBitmap !== 'function') return null;
  let bitmap = null;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: 'from-image', colorSpaceConversion: 'default' });
    const canvas = document.createElement('canvas');
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const ctx = canvas.getContext('2d', { colorSpace: 'srgb' });
    if (!ctx) return null;
    ctx.fillStyle = '#000000';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(bitmap, 0, 0);
    return await new Promise((resolve) => canvas.toBlob((blob) => resolve(blob), 'image/png'));
  } catch {
    return null;
  } finally {
    bitmap?.close();
  }
}

function revealOrCopyButton(exportId, savedPath) {
  if (revealLabel() && exportId) {
    const btn = h('button', {
      class: 'btn sm',
      type: 'button',
      onclick: () => busyButton(btn, () => api(`/api/exports/${exportId}/reveal`, { method: 'POST' }), {
        onError: (e) => { toastError(e, '无法在访达中显示。文件可能已被移动。'); return false; },
      }),
    }, h('span', {}, '在访达中显示'));
    return btn;
  }
  if (savedPath && globalThis.navigator && navigator.clipboard) {
    return h('button', {
      class: 'btn sm',
      type: 'button',
      onclick: () => navigator.clipboard.writeText(savedPath).then(
        () => toast('已复制路径', 'ok'),
        (e) => toastError(e, '无法复制路径。'),
      ),
    }, '复制路径');
  }
  return null;
}

export function openOffImageDialog(view) {
  const photo = view && view.photo ? view.photo : null;
  let closed = false;
  let seq = 0;
  let busy = false;
  let preview = null;
  let close = null;

  const previewImg = h('img', {
    class: 'off-img-preview',
    width: 640,
    height: 480,
    alt: '关机图预览',
    hidden: true,
  });
  const previewState = h('div', { class: 'off-img-state small dim' }, '正在生成关机图预览…');
  const stage = h('div', { class: 'off-img-stage' }, previewImg, previewState);

  const sourceLabel = h('span', { class: 'small dim off-img-source' }, photo ? photo.name : '未选择图片');
  const fileInput = h('input', {
    type: 'file',
    accept: 'image/*',
    hidden: true,
    'aria-label': '选择图片文件',
    onchange: () => {
      const picked = fileInput.files && fileInput.files[0];
      fileInput.value = '';
      if (picked) void loadFile(picked).catch(() => {});
    },
  });
  const pickBtn = h('button', {
    class: 'btn sm',
    type: 'button',
    dataset: { act: 'pick-off-image' },
    onclick: () => fileInput.click(),
  }, '换一张图…');

  const metaLine = h('p', { class: 'off-img-meta small num dim', dataset: { role: 'off-meta' } }, '640 × 480');
  const notes = h(
    'div',
    { class: 'off-img-notes' },
    h('p', { class: 'small dim' }, NOTE_CAMERA_COUNT),
    h('p', { class: 'small dim' }, NOTE_CARD_ROOT),
  );

  const destination = exportDestination(state.settings ? state.settings.export_dir : '');
  const destInput = destination.input;
  const resultEl = h('div', { class: 'export-result', role: 'status' });
  const cancelBtn = h('button', { class: 'btn', type: 'button', onclick: () => close && close() }, '取消');
  const exportBtn = h('button', {
    class: 'btn primary',
    type: 'button',
    disabled: true,
    dataset: { act: 'export-off-image-submit' },
    onclick: () => { void submit().catch(() => {}); },
  }, '导出');

  function syncExportEnabled() {
    const can = !busy && Boolean(preview && preview.ok && preview.preview_token);
    exportBtn.disabled = !can;
    if (can) exportBtn.removeAttribute('aria-disabled');
    else exportBtn.setAttribute('aria-disabled', 'true');
  }

  function paintLoading(text) {
    preview = null;
    previewImg.hidden = true;
    previewImg.removeAttribute('src');
    previewState.hidden = false;
    previewState.textContent = text;
    metaLine.textContent = '640 × 480 · 正在计算…';
    resultEl.replaceChildren();
    syncExportEnabled();
  }

  function paintPreview(res) {
    preview = res;
    if (res && res.ok && res.preview_data_url) {
      previewImg.src = res.preview_data_url;
      previewImg.hidden = false;
      previewState.hidden = true;
      metaLine.textContent = `640 × 480 · ${fmtBytes(res.bytes)}（${res.bytes.toLocaleString('en-US')} B）· 品质 ${res.quality}`;
      resultEl.replaceChildren();
    } else {
      previewImg.hidden = true;
      previewImg.removeAttribute('src');
      previewState.hidden = false;
      previewState.textContent = '关机图校验未通过';
      metaLine.textContent = res ? `640 × 480 · ${fmtBytes(res.bytes || 0)} · 品质 ${res.quality ?? '—'}` : '640 × 480';
      const reasons = (res && Array.isArray(res.errors) ? res.errors : []).map((e) => e.message).filter(Boolean);
      resultEl.replaceChildren(h('div', { class: 'result bad' }, reasons.join(' ') || '关机图校验未通过，无法导出。'));
    }
    syncExportEnabled();
  }

  async function loadPhoto(target) {
    if (!target) return;
    const mySeq = ++seq;
    sourceLabel.textContent = target.name || '当前照片';
    paintLoading('正在生成关机图预览…');
    try {
      await flushPhotos([target.id]).catch(() => {});
      if (closed || mySeq !== seq) return;
      const res = await api(`/api/off-image/preview?shot_id=${encodeURIComponent(target.id)}`, { method: 'POST' });
      if (closed || mySeq !== seq) return;
      paintPreview(res);
    } catch (err) {
      if (closed || mySeq !== seq) return;
      preview = null;
      previewState.hidden = false;
      previewState.textContent = '预览生成失败';
      resultEl.replaceChildren(h('div', { class: 'result bad' }, errorBody(err, '无法生成关机图预览。请重试。')));
      syncExportEnabled();
    }
  }

  async function loadFile(file) {
    const mySeq = ++seq;
    sourceLabel.textContent = file.name || '已选图片';
    paintLoading('正在处理所选图片…');
    try {
      let res;
      try {
        res = await api('/api/off-image/preview', { method: 'POST', body: file });
      } catch (err) {
        if (err && err.code === 'unsupported_image') {
          const fallback = await decodeViaBrowserToPng(file);
          if (closed || mySeq !== seq) return;
          if (!fallback) throw err;
          res = await api('/api/off-image/preview', { method: 'POST', body: fallback });
        } else {
          throw err;
        }
      }
      if (closed || mySeq !== seq) return;
      paintPreview(res);
    } catch (err) {
      if (closed || mySeq !== seq) return;
      preview = null;
      previewState.hidden = false;
      previewState.textContent = '无法读取这张图片';
      resultEl.replaceChildren(h('div', { class: 'result bad' }, errorBody(err, '无法读取这张图片。请选择 JPEG 或 PNG 图片。')));
      syncExportEnabled();
    }
  }

  async function submit() {
    if (busy || destination.picking || !preview || !preview.ok || !preview.preview_token) return;
    const dest = destInput.value.trim();
    if (!dest) {
      destInput.setAttribute('aria-invalid', 'true');
      resultEl.replaceChildren(h('div', { class: 'result bad' }, '请填写导出位置（文件夹的完整路径）。'));
      destInput.focus();
      return;
    }
    busy = true;
    syncExportEnabled();
    exportBtn.setAttribute('aria-busy', 'true');
    resultEl.replaceChildren();
    try {
      const res = await api('/api/off-image/export', {
        method: 'POST',
        body: { preview_token: preview.preview_token, dest_dir: dest },
      });
      if (closed) return;
      destination.remember(dest);
      cancelBtn.textContent = '关闭';
      resultEl.replaceChildren(h('div', { class: 'result ok' }, h('div', {},
        h('p', { class: 'res-main' }, '已导出 GXROFF.JPG'),
        h('p', { class: 'small num path' }, res.path),
        h('p', { class: 'small dim num' }, `${res.width} × ${res.height} · ${fmtBytes(res.bytes)} · 品质 ${res.quality}`),
        h('div', { class: 'res-acts' }, revealOrCopyButton(res.export_id, res.path)))));
    } catch (err) {
      if (closed) return;
      resultEl.replaceChildren(h('div', { class: 'result bad' }, errorBody(err, '导出失败。请重试。')));
    } finally {
      busy = false;
      exportBtn.setAttribute('aria-busy', 'false');
      syncExportEnabled();
    }
  }

  const body = h(
    'div',
    { class: 'export-form off-img-dialog', dataset: { state: 'off-image-dialog' } },
    stage,
    h('div', { class: 'off-img-bar' }, metaLine, h('div', { class: 'off-img-src' }, sourceLabel, pickBtn, fileInput)),
    notes,
    h('div', { class: 'row form' }, h('span', { class: 'lab' }, '导出到'), h('div', { class: 'ctl' }, destination.el)),
    h('div', { class: 'row form' }, h('span', { class: 'lab' }, '文件名'), h('div', { class: 'ctl' }, h('span', { class: 'num' }, 'GXROFF.JPG'))),
    resultEl,
    h('div', { class: 'modal-foot' }, cancelBtn, exportBtn),
  );

  close = openModal('导出关机图', body, {
    wide: false,
    onClose: () => {
      closed = true;
      seq += 1;
    },
  });

  if (photo) void loadPhoto(photo).catch(() => {});
  else paintLoading('请点「换一张图…」选择一张图片。');
  return close;
}
