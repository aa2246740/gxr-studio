// 边框面板里的「标志」区块、上传去白底弹窗与「我的标志」管理弹窗（DESIGN.md §10.5–§10.6）。

import { api } from './api.js';
import { refreshDefaults } from './caps.js';
import { h, icon, openModal, toast, toastError } from './dom.js';
import { flushPhotos, saveRecord } from './saves.js';
import { seg, toggle, notice } from './controls.js';
import { normalizeFrame, clipText } from './finish.js';
import {
  LOGO_STYLES,
  pickLogoMode,
  logosLoaded,
  ensureLogosLoaded,
  getLogoList,
  getLogoMap,
  onLogoCatalog,
  useLogos,
} from './finish_frame_core.js';
import {
  sniffImageMagic,
  isOpaqueImageData,
  hasWhiteBackground,
  keyWhiteImageData,
  drawCheckerPreview,
} from './logo_key.js';

const MAX_BYTES = 5 * 1024 * 1024;
const MAX_PIXELS = 25_000_000;
const MAX_DIM = 16_384;
const MAX_LOGOS = 12;
const NAME_MAX = 20;

const ERR_TOO_BIG = '文件太大了（超过 5 MB）。请换一张小一点的。';
const ERR_UNREADABLE = '这个文件打不开。请换一张 PNG、JPG 或 WebP 图片。';
const ERR_LIMIT = '已经保存了 12 个标志。删除一个，再上传新的。';
const WARN_WHITE_BG = '这张图有白色背景。去掉后，标志放在彩色或深色边框上才好看。';
const WARN_OPAQUE = '这张图是不透明的，标志可能带着背景。可以打开「去掉白底」。';

const reportedMissing = new Set();

function styleBgLum(frame) {
  const s = frame.style;
  const c = frame.color || 'white';
  if (s === 'cinema') return 8;
  if (s === 'postage_stamp') return 250;
  if (s === 'slide_mount') return c === 'black' ? 30 : 240;
  if (s === 'exhibition_label') return c === 'black' ? 22 : 250;
  if (s === 'thin_keyline') return c === 'black' ? 18 : (c === 'cream' ? 238 : 245);
  return c === 'black' ? 18 : 245;
}

export function maybeNotifyAutoMono(frame, explicitInfo = null) {
  if (!frame || !LOGO_STYLES.includes(frame.style) || frame.style === 'blur_fill') return;
  const id = frame.logo || (frame.show_logo === false ? 'none' : 'ricoh');
  if (id === 'ricoh' || id === 'none' || frame.show_logo === false) return;
  if ((frame.logo_mode || 'auto') !== 'auto') return;
  const info = explicitInfo || getLogoMap()[id];
  if (!info || info.opaque) return;
  const bgLum = styleBgLum(frame);
  if (pickLogoMode(info, 'auto', bgLum) === 'mono') {
    const tone = bgLum < 128 ? '白色' : '深色';
    toast(`标志和这个边框的颜色太接近，已自动改成${tone}。要用原色，把「颜色」改成「原色」。`, 'info');
  }
}

function checkMissingLogo(view, setFrame, options) {
  const ed = view.ed;
  if (!ed || !ed.frame || !logosLoaded()) return false;
  const id = ed.frame.logo;
  if (!id || id === 'ricoh' || id === 'none') return false;
  if (getLogoMap()[id]) return false;
  const msgKey = `${ed.id || ''}:${id}`;
  setFrame(view, { logo: 'ricoh', show_logo: true }, options);
  if (!reportedMissing.has(msgKey)) {
    reportedMissing.add(msgKey);
    toast('找不到这个标志，已改回 RICOH 标志。', 'info');
  }
  return true;
}

function stemOf(filename) {
  const base = String(filename || '').replace(/\\/g, '/').split('/').pop() || '';
  const dot = base.lastIndexOf('.');
  const raw = dot > 0 ? base.slice(0, dot) : base;
  return clipText(raw.trim(), NAME_MAX);
}

function decodePreviewImage(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      const w = img.naturalWidth, h = img.naturalHeight;
      if (!w || !h || w > MAX_DIM || h > MAX_DIM || w * h > MAX_PIXELS) {
        reject(new Error('dimensions'));
        return;
      }
      const scale = Math.min(1, 480 / Math.max(w, h));
      const pw = Math.max(1, Math.round(w * scale)), ph = Math.max(1, Math.round(h * scale));
      const cv = document.createElement('canvas');
      cv.width = pw;
      cv.height = ph;
      const ctx = cv.getContext('2d');
      ctx.drawImage(img, 0, 0, pw, ph);
      resolve({ imgData: ctx.getImageData(0, 0, pw, ph), origW: w, origH: h });
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('decode'));
    };
    img.src = url;
  });
}

export function openUploadDialog(view, setFrame) {
  if (logosLoaded() && getLogoList().length >= MAX_LOGOS) {
    toast(ERR_LIMIT, 'error');
    return;
  }
  let currentFile = null;
  let origData = null;
  let whiteBg = false;
  let origOpaque = false;
  let nameTouched = false;
  let busy = false;
  let closeDialog = () => {};

  const leftLabel = h('div', { class: 'logo-pv-label' }, '你选的图片');
  const rightLabel = h('div', { class: 'logo-pv-label' }, '裁剪边缘后');
  const leftCanvas = h('canvas', { class: 'logo-pv-canvas', width: 248, height: 164 });
  const rightCanvas = h('canvas', { class: 'logo-pv-canvas', width: 248, height: 164 });
  drawCheckerPreview(leftCanvas, null);
  drawCheckerPreview(rightCanvas, null);

  const fileNameSpan = h('span', { class: 'small dim logo-file-name' }, '支持 PNG、JPG、WebP（不超过 5 MB）');
  const fileInput = h('input', {
    type: 'file',
    accept: 'image/png,image/jpeg,image/webp',
    class: 'logo-file-input',
    'aria-label': '选择标志图片',
  });
  const pickBtn = h('button', {
    type: 'button',
    class: 'btn sm',
    onclick: () => fileInput.click(),
  }, icon('upload'), h('span', {}, '选择图片…'));

  const hintSlot = h('div', { class: 'logo-hint-slot' });

  const keyInput = h('input', {
    type: 'checkbox',
    class: 'switch-input',
    role: 'switch',
    'aria-label': '去掉白底',
    disabled: true,
  });
  const keyRow = h('label', { class: 'switch-row' }, h('span', {}, '去掉白底'), keyInput);

  const tolOut = h('output', { class: 'num' }, '30');
  const tolInput = h('input', {
    type: 'range',
    min: '0',
    max: '60',
    step: '1',
    value: '30',
    'aria-label': '容差',
    disabled: true,
  });
  const tolRow = h('div', { class: 'logo-tol-row' }, h('span', {}, '容差'), tolInput, tolOut);

  const nameInput = h('input', {
    type: 'text',
    maxlength: String(NAME_MAX),
    placeholder: '例如：我的签名',
    'aria-label': '名称',
  });
  nameInput.addEventListener('input', () => { nameTouched = true; });
  const nameRow = h('label', { class: 'field-row logo-name-row' }, h('span', {}, '名称'), nameInput);

  const saveBtn = h('button', {
    type: 'button',
    class: 'btn primary',
    disabled: true,
  }, '保存并使用');
  const cancelBtn = h('button', {
    type: 'button',
    class: 'btn',
    onclick: () => closeDialog(),
  }, '取消');

  function refreshPreview() {
    hintSlot.replaceChildren();
    if (!origData) {
      drawCheckerPreview(leftCanvas, null);
      drawCheckerPreview(rightCanvas, null);
      tolInput.disabled = true;
      return;
    }
    const keyOn = keyInput.checked;
    const tol = Number(tolInput.value) || 0;
    tolOut.textContent = String(tol);
    tolInput.disabled = !keyOn;
    leftLabel.textContent = whiteBg ? '你选的图片（有白底）' : '你选的图片';
    rightLabel.textContent = keyOn ? '去掉白底后' : '裁剪边缘后';
    drawCheckerPreview(leftCanvas, origData, { cropToInk: false });
    const processed = keyOn ? keyWhiteImageData(origData, tol) : origData;
    drawCheckerPreview(rightCanvas, processed, { cropToInk: true });
    if (whiteBg && keyOn) {
      hintSlot.append(notice('amber', WARN_WHITE_BG));
    } else if (origOpaque && !keyOn) {
      hintSlot.append(notice('amber', WARN_OPAQUE));
    }
  }

  keyInput.addEventListener('change', refreshPreview);
  tolInput.addEventListener('input', refreshPreview);

  async function handleFile(file) {
    if (!file) return;
    currentFile = null;
    origData = null;
    saveBtn.disabled = true;
    keyInput.disabled = true;
    tolInput.disabled = true;
    fileNameSpan.textContent = file.name || '';
    if (file.size > MAX_BYTES) {
      refreshPreview();
      hintSlot.replaceChildren(notice('grey', ERR_TOO_BIG));
      toast(ERR_TOO_BIG, 'error');
      return;
    }
    try {
      const head = new Uint8Array(await file.slice(0, 16).arrayBuffer());
      if (!sniffImageMagic(head)) {
        refreshPreview();
        hintSlot.replaceChildren(notice('grey', ERR_UNREADABLE));
        toast(ERR_UNREADABLE, 'error');
        return;
      }
      const decoded = await decodePreviewImage(file);
      currentFile = file;
      origData = decoded.imgData;
      whiteBg = hasWhiteBackground(origData);
      origOpaque = isOpaqueImageData(origData);
      keyInput.disabled = !origOpaque;
      keyInput.checked = whiteBg;
      tolInput.value = '30';
      if (!nameTouched || !nameInput.value.trim()) {
        nameInput.value = stemOf(file.name) || '我的标志';
      }
      saveBtn.disabled = false;
      refreshPreview();
      if (origOpaque && !whiteBg) toast(WARN_OPAQUE, 'info');
    } catch {
      refreshPreview();
      hintSlot.replaceChildren(notice('grey', ERR_UNREADABLE));
      toast(ERR_UNREADABLE, 'error');
    }
  }

  fileInput.addEventListener('change', () => {
    const f = fileInput.files && fileInput.files[0];
    if (f) handleFile(f);
  });

  saveBtn.addEventListener('click', async () => {
    if (busy || !currentFile) return;
    const chosenName = clipText(nameInput.value.trim(), NAME_MAX) || stemOf(currentFile.name) || '我的标志';
    busy = true;
    saveBtn.disabled = true;
    cancelBtn.disabled = true;
    saveBtn.setAttribute('aria-busy', 'true');
    try {
      const qs = new URLSearchParams({
        name: chosenName,
        filename: currentFile.name || '',
        key_white: keyInput.checked ? 'true' : 'false',
        tol: String(Number(tolInput.value) || 0),
      });
      const res = await api(`/api/logos?${qs.toString()}`, { method: 'POST', body: currentFile });
      useLogos(res.logos);
      closeDialog();
      setFrame(view, { logo: res.logo.id, show_logo: true });
      toast(`已保存「${res.logo.name}」，并用在这个边框上。`, 'ok');
      if (view.ed?.frame) maybeNotifyAutoMono(view.ed.frame, res.logo);
    } catch (err) {
      toastError(err, '无法保存这个标志。请重试。');
      busy = false;
      saveBtn.disabled = false;
      cancelBtn.disabled = false;
      saveBtn.setAttribute('aria-busy', 'false');
    }
  });

  const body = h(
    'div',
    {
      class: 'logo-upload-body',
      ondragover: (e) => { e.preventDefault(); },
      ondrop: (e) => {
        e.preventDefault();
        const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
        if (f) handleFile(f);
      },
    },
    h('div', { class: 'logo-pick-bar' }, pickBtn, fileNameSpan, fileInput),
    h(
      'div',
      { class: 'logo-pv-grid' },
      h('div', { class: 'logo-pv-col' }, leftLabel, leftCanvas),
      h('div', { class: 'logo-pv-col' }, rightLabel, rightCanvas),
    ),
    hintSlot,
    keyRow,
    tolRow,
    nameRow,
    h('p', { class: 'small dim logo-privacy-note' }, '标志只保存在这台电脑上，不会上传。透明背景的 PNG 效果最好。'),
  );

  const foot = h('div', { class: 'modal-foot' }, cancelBtn, saveBtn);
  closeDialog = openModal('添加标志', body, { wide: true, foot, initialFocus: pickBtn });
}

export function openManageDialog(view, setFrame) {
  let closeDialog = () => {};
  let editingId = null;
  let confirmingId = null;
  let busy = false;

  const counterEl = h('span', { class: 'small dim num logo-count-badge' }, '');
  const listWrap = h('div', { class: 'logo-manage-list' });

  function renderRows() {
    const logos = getLogoList();
    counterEl.textContent = `${logos.length} / ${MAX_LOGOS}`;
    listWrap.replaceChildren();

    for (const item of logos) {
      const thumb = h(
        'div',
        { class: 'logo-manage-thumb' },
        h('img', { src: `/api/logos/${encodeURIComponent(item.id)}/image`, alt: '' }),
      );
      const metaText = `${item.opaque ? '不透明' : '透明'} PNG · ${item.w} × ${item.h}`;

      if (editingId === item.id) {
        const input = h('input', {
          type: 'text',
          class: 'logo-rename-input',
          value: item.name,
          maxlength: String(NAME_MAX),
          'aria-label': '标志名称',
        });
        const saveRename = async () => {
          const nextName = clipText(input.value.trim(), NAME_MAX);
          if (!nextName) {
            toast('请输入标志名称（最多 20 个字）。', 'error');
            return;
          }
          if (busy) return;
          busy = true;
          try {
            const res = await api(`/api/logos/${encodeURIComponent(item.id)}`, {
              method: 'PUT',
              body: { name: nextName },
            });
            useLogos(res.logos);
            editingId = null;
            renderRows();
            view.redrawSide?.();
          } catch (err) {
            toastError(err, '无法重命名这个标志。请重试。');
          } finally {
            busy = false;
          }
        };
        input.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') { e.preventDefault(); saveRename(); }
          if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); editingId = null; renderRows(); }
        });
        const row = h(
          'div',
          { class: 'logo-manage-row', 'data-logo-row': item.id },
          h(
            'div',
            { class: 'logo-manage-main' },
            thumb,
            h('div', { class: 'logo-manage-info' }, input, h('div', { class: 'small dim num' }, metaText)),
            h(
              'div',
              { class: 'logo-manage-acts' },
              h('button', { type: 'button', class: 'btn sm', onclick: () => { editingId = null; renderRows(); } }, '取消'),
              h('button', { type: 'button', class: 'btn sm primary', onclick: saveRename }, '保存'),
            ),
          ),
        );
        listWrap.append(row);
        queueMicrotask(() => input.focus());
        continue;
      }

      const isConfirm = confirmingId === item.id;
      const acts = isConfirm
        ? h(
            'div',
            { class: 'logo-manage-acts' },
            h('button', { type: 'button', class: 'btn sm', onclick: () => { confirmingId = null; renderRows(); } }, '取消'),
            h('button', {
              type: 'button',
              class: 'btn sm danger',
              onclick: async () => {
                if (busy) return;
                busy = true;
                try {
                  if (view.photo?.id) await flushPhotos([view.photo.id]).catch(() => {});
                  const res = await api(`/api/logos/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
                  useLogos(res.logos);
                  confirmingId = null;
                  for (const [pid, nextRev] of Object.entries(res.reverted_revs || {})) {
                    if (typeof nextRev === 'number') saveRecord(pid).rev = nextRev;
                  }
                  if (res.reverted_defaults) {
                    refreshDefaults();
                  }
                  if (view.ed?.frame?.logo === item.id) {
                    view.ed.frame = normalizeFrame({ ...view.ed.frame, logo: 'ricoh', show_logo: true });
                  }
                  view.onChange?.();
                  view.redrawSide?.();
                  toast(res.message || `已删除「${item.name}」。`, 'info');
                  renderRows();
                } catch (err) {
                  toastError(err, '无法删除这个标志。请重试。');
                } finally {
                  busy = false;
                }
              },
            }, '删除'),
          )
        : h(
            'div',
            { class: 'logo-manage-acts' },
            h('button', { type: 'button', class: 'btn ghost sm', onclick: () => { confirmingId = null; editingId = item.id; renderRows(); } }, '重命名'),
            h('button', { type: 'button', class: 'btn ghost sm logo-del-link', onclick: () => { editingId = null; confirmingId = item.id; renderRows(); } }, '删除'),
          );

      const confirmBar = isConfirm
        ? h('div', { class: 'logo-confirm-bar' }, `删除「${item.name}」？用到它的边框会改回 RICOH 标志。`)
        : null;

      listWrap.append(
        h(
          'div',
          { class: `logo-manage-row${isConfirm ? ' confirming' : ''}`, 'data-logo-row': item.id },
          h(
            'div',
            { class: 'logo-manage-main' },
            thumb,
            h('div', { class: 'logo-manage-info' }, h('div', { class: 'logo-manage-name' }, item.name), h('div', { class: 'small dim num' }, metaText)),
            acts,
          ),
          confirmBar,
        ),
      );
    }

    if (logos.length < MAX_LOGOS) {
      listWrap.append(
        h(
          'button',
          {
            type: 'button',
            class: 'logo-add-slot',
            onclick: () => {
              closeDialog();
              openUploadDialog(view, setFrame);
            },
          },
          '＋  上传新的标志（PNG、JPG、WebP，不超过 5 MB）',
        ),
      );
    }
  }

  renderRows();
  const body = h(
    'div',
    { class: 'logo-manage-body' },
    h('div', { class: 'logo-manage-subhead' }, h('span', { class: 'small dim' }, '已保存的标志'), counterEl),
    listWrap,
  );
  const doneBtn = h('button', { type: 'button', class: 'btn primary', onclick: () => closeDialog() }, '完成');
  closeDialog = openModal('我的标志', body, { wide: true, foot: h('div', { class: 'modal-foot' }, doneBtn), initialFocus: doneBtn });
}

function logoTile({ id, label, selected, boxClass, content, onClick }) {
  return h(
    'button',
    {
      type: 'button',
      class: 'logo-tile',
      'data-logo': id,
      'aria-pressed': String(selected),
      onclick: onClick,
    },
    h(
      'div',
      { class: `logo-tile-box ${boxClass}` },
      content,
      selected ? h('span', { class: 'logo-tile-check', 'aria-hidden': 'true' }, icon('check')) : null,
    ),
    h('div', { class: 'logo-tile-label' }, label),
  );
}

export function renderLogoControls(view, setFrame) {
  const ed = view.ed;
  const cur = ed.frame;
  if (!view.logoCatalogWatch) {
    view.logoCatalogWatch = onLogoCatalog(() => {
      if (!view.disposed) checkMissingLogo(view, setFrame, { redraw: false });
    });
  }
  if (!logosLoaded()) {
    ensureLogosLoaded()
      .then(() => {
        if (view.disposed) return;
        if (!checkMissingLogo(view, setFrame)) {
          view.onChange?.();
          view.redrawSide?.();
        }
      })
      .catch(() => {});
  } else if (cur.logo && cur.logo !== 'ricoh' && cur.logo !== 'none' && !getLogoMap()[cur.logo]) {
    queueMicrotask(() => {
      if (!view.disposed) checkMissingLogo(view, setFrame);
    });
  }

  const saved = getLogoList();
  const activeLogo = cur.logo || (cur.show_logo === false ? 'none' : 'ricoh');
  const activeInfo = activeLogo !== 'ricoh' && activeLogo !== 'none' ? getLogoMap()[activeLogo] : null;
  const isCustom = !!(activeLogo !== 'ricoh' && activeLogo !== 'none' && activeInfo);

  const tiles = [
    logoTile({
      id: 'ricoh',
      label: 'RICOH',
      selected: activeLogo === 'ricoh' || (activeLogo !== 'none' && !activeInfo),
      boxClass: 'light',
      content: h('img', { src: '/api/finish/assets/ricoh.png', alt: '' }),
      onClick: () => {
        if (activeLogo !== 'ricoh') setFrame(view, { logo: 'ricoh', show_logo: true });
      },
    }),
    ...saved.map((item) =>
      logoTile({
        id: item.id,
        label: item.name,
        selected: activeLogo === item.id,
        boxClass: 'light',
        content: h('img', { src: `/api/logos/${encodeURIComponent(item.id)}/image`, alt: '' }),
        onClick: () => {
          if (activeLogo !== item.id) {
            setFrame(view, { logo: item.id, show_logo: true });
            if (view.ed?.frame) maybeNotifyAutoMono(view.ed.frame, item);
          }
        },
      }),
    ),
    h(
      'button',
      {
        type: 'button',
        class: 'logo-tile action',
        'data-logo-action': 'upload',
        onclick: () => openUploadDialog(view, setFrame),
      },
      h('div', { class: 'logo-tile-box dashed' }, icon('plus')),
      h('div', { class: 'logo-tile-label' }, '上传…'),
    ),
    logoTile({
      id: 'none',
      label: '不显示',
      selected: activeLogo === 'none',
      boxClass: 'plain',
      content: icon('ban'),
      onClick: () => {
        if (activeLogo !== 'none') setFrame(view, { logo: 'none', show_logo: false });
      },
    }),
  ];

  const out = [
    h(
      'div',
      { class: 'sec logo-sec-head' },
      h('span', {}, '标志'),
      h('span', { class: 'logo-sec-note' }, 'RICOH 是默认；也可以用自己的'),
    ),
    h('div', { class: 'logo-tiles', role: 'group', 'aria-label': '标志' }, ...tiles),
  ];

  if (logosLoaded() && saved.length === 0) {
    out.push(
      h(
        'div',
        { class: 'logo-empty' },
        h('div', { class: 'logo-empty-title' }, '还没有自己的标志'),
        h('div', { class: 'logo-empty-sub' }, '上传一张 PNG（背景透明的效果最好）。'),
      ),
    );
  }

  if (isCustom) {
    out.push(
      h(
        'div',
        { class: 'logo-subcontrols', 'data-logo-controls': '1' },
        seg(
          [['auto', '自动'], ['orig', '原色'], ['mono', '单色']],
          cur.logo_mode || 'auto',
          (logo_mode) => setFrame(view, { logo_mode }),
          { label: '标志颜色' },
        ),
        seg(
          [['small', '小'], ['medium', '中'], ['large', '大']],
          cur.logo_size || 'medium',
          (logo_size) => setFrame(view, { logo_size }),
          { label: '大小' },
        ),
      ),
    );
  }

  if (cur.style === 'leica' && activeLogo !== 'none') {
    out.push(toggle('型号文字「GXR」', cur.model !== false, (model) => setFrame(view, { model })));
  }

  if (saved.length > 0) {
    out.push(
      h(
        'button',
        {
          type: 'button',
          class: 'logo-manage-link',
          onclick: () => openManageDialog(view, setFrame),
        },
        '管理我的标志…',
      ),
    );
  }

  return out;
}
