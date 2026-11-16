// 标志底部面板（「标志」chip 打开）与添加/去白底子视图（照桌面 `web/assets/frame_logo_ui.js`）。
// 内置标志标为「GXR 标志」（spec.logo 仍存 "ricoh"）；自定义标志存在本地 IndexedDB，最多 12 个。

import { previewLogo } from '../core/api';
import {
  cleanLogoName,
  drawCheckerPreview,
  keyWhiteImageData,
  LOGO_ERR,
  LOGO_LIMITS,
  LOGO_WARN,
  stemOf,
  type RgbaPixels,
} from '../lib/logo_key';
import type { LogoMode, LogoSize } from '../lib/frame_spec';
import { button, el, icon } from './dom';
import { autoMonoMessage, commitFrame } from './frame_state';
import { logoStore } from './logo_store';
import type { Photo } from './model';
import { showToast } from './toast';

const CHECK_ICON = 'M5 12.5l4.5 4.5L19 7.5';
const BAN_ICON = 'M5.5 5.5l13 13M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z';

function segGroup<T extends string>(
  items: ReadonlyArray<readonly [T, string]>,
  current: T,
  label: string,
  onPick: (value: T) => void,
): HTMLElement {
  const seg = el('div', 'seg');
  seg.setAttribute('role', 'group');
  seg.setAttribute('aria-label', label);
  for (const [value, text] of items) {
    const btn = button(value === current ? 'on' : '', text);
    btn.addEventListener('click', () => onPick(value));
    seg.append(btn);
  }
  return seg;
}

export function openLogoSheet(
  host: HTMLElement,
  photo: Photo,
  onUpdate: () => void,
): void {
  host.querySelector('.scrim.logo-scrim')?.remove();
  host.querySelector('.ms.logo-sheet')?.remove();

  const scrim = el('div', 'scrim logo-scrim');
  const sheet = el('div', 'ms frame-sheet logo-sheet');

  let viewMode: 'list' | 'upload' = 'list';
  let menuLogoId: string | null = null;
  let editingLogoId: string | null = null;
  let confirmingLogoId: string | null = null;
  let busy = false;

  const close = () => {
    unsub();
    scrim.remove();
    sheet.remove();
  };

  const applyLogo = (logo: string, showLogo: boolean): void => {
    commitFrame(photo, { logo, show_logo: showLogo }, onUpdate);
    const monoMsg = autoMonoMessage(photo.border);
    if (monoMsg) showToast(host, monoMsg);
    render();
  };

  const renderList = (): void => {
    sheet.dataset.state = 'logo';
    const hd = el('div', 'hd');
    hd.append(el('h2', '', '标志'));
    const done = button('nb bold', '完成');
    done.addEventListener('click', close);
    hd.append(done);

    const body = el('div', 'ms-body');
    const cur = photo.border;
    const activeLogo = cur.logo || (cur.show_logo === false ? 'none' : 'ricoh');
    const saved = logoStore.list();

    // 不显示
    const noneBtn = button(`logo-opt${activeLogo === 'none' ? ' on' : ''}`, '');
    noneBtn.dataset.logo = 'none';
    const noneThumb = el('div', 'lth none');
    noneThumb.append(icon(BAN_ICON));
    const noneTx = el('div', 'ltx');
    noneTx.append(el('b', '', '不显示'), el('small', '', '不在边框上画标志'));
    noneBtn.append(noneThumb, noneTx);
    if (activeLogo === 'none') noneBtn.append(el('span', 'lck'));
    if (activeLogo === 'none') noneBtn.lastElementChild?.append(icon(CHECK_ICON));
    noneBtn.addEventListener('click', () => applyLogo('none', false));

    // GXR 标志（内置文字徽标，ID 保持 'ricoh'）
    const gxrBtn = button(`logo-opt${activeLogo === 'ricoh' ? ' on' : ''}`, '');
    gxrBtn.dataset.logo = 'ricoh';
    const gxrThumb = el('div', 'lth', 'GXR');
    const gxrTx = el('div', 'ltx');
    gxrTx.append(el('b', '', 'GXR 标志'), el('small', '', '内置文字徽标，默认样式'));
    gxrBtn.append(gxrThumb, gxrTx);
    if (activeLogo === 'ricoh') {
      const ck = el('span', 'lck');
      ck.append(icon(CHECK_ICON));
      gxrBtn.append(ck);
    }
    gxrBtn.addEventListener('click', () => applyLogo('ricoh', true));

    body.append(noneBtn, gxrBtn);

    // 自定义标志区
    const secHead = el('div', 'fsrow');
    const line = el('div', 'fsline');
    line.append(
      el('span', 'fslab', '我的标志'),
      el('span', 'small dim num', `${saved.length} / ${LOGO_LIMITS.maxLogos}`),
    );
    secHead.append(line);
    body.append(secHead);

    if (!saved.length) {
      body.append(el('p', 'ffine', '还没有自己的标志。上传一张 PNG（背景透明的效果最好）。'));
    }

    for (const item of saved) {
      const wrap = el('div', 'logo-row');
      wrap.dataset.logoRow = item.id;

      if (editingLogoId === item.id) {
        const actRow = el('div', 'logo-row-acts');
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'txtbox';
        input.maxLength = LOGO_LIMITS.nameMax;
        input.value = item.name;
        input.setAttribute('aria-label', '标志名称');
        const cancelBtn = button('btn', '取消');
        cancelBtn.addEventListener('click', () => {
          editingLogoId = null;
          render();
        });
        const saveBtn = button('btn primary', '保存');
        const doRename = async () => {
          if (busy) return;
          busy = true;
          const res = await logoStore.rename(item.id, input.value);
          busy = false;
          if (!res.ok) {
            showToast(host, res.error);
            return;
          }
          editingLogoId = null;
          onUpdate();
          render();
        };
        saveBtn.addEventListener('click', () => void doRename());
        input.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            void doRename();
          } else if (e.key === 'Escape') {
            editingLogoId = null;
            render();
          }
        });
        actRow.append(input, cancelBtn, saveBtn);
        wrap.append(actRow);
        body.append(wrap);
        queueMicrotask(() => input.focus());
        continue;
      }

      const selected = activeLogo === item.id;
      const opt = button(`logo-opt${selected ? ' on' : ''}`, '');
      opt.dataset.logo = item.id;
      const thumb = el('div', 'lth');
      const srcCv = logoStore.canvas(item.id);
      if (srcCv) {
        const copy = document.createElement('canvas');
        copy.width = srcCv.width;
        copy.height = srcCv.height;
        copy.getContext('2d')?.drawImage(srcCv, 0, 0);
        thumb.append(copy);
      }
      const tx = el('div', 'ltx');
      const metaLabel = `${item.opaque ? '不透明' : '透明'} · ${item.w} × ${item.h}`;
      tx.append(el('b', '', item.name), el('small', 'num', metaLabel));
      opt.append(thumb, tx);
      if (selected) {
        const ck = el('span', 'lck');
        ck.append(icon(CHECK_ICON));
        opt.append(ck);
      }

      let pressTimer = 0;
      let longPressed = false;
      opt.addEventListener('pointerdown', () => {
        longPressed = false;
        window.clearTimeout(pressTimer);
        pressTimer = window.setTimeout(() => {
          longPressed = true;
          menuLogoId = menuLogoId === item.id ? null : item.id;
          confirmingLogoId = null;
          render();
        }, 480);
      });
      const cancelPress = () => window.clearTimeout(pressTimer);
      opt.addEventListener('pointerup', cancelPress);
      opt.addEventListener('pointercancel', cancelPress);
      opt.addEventListener('pointermove', cancelPress);
      opt.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        menuLogoId = menuLogoId === item.id ? null : item.id;
        confirmingLogoId = null;
        render();
      });
      opt.addEventListener('click', () => {
        if (longPressed) return;
        applyLogo(item.id, true);
      });

      const rowTop = el('div', 'fsline');
      rowTop.append(opt);
      const moreBtn = button('nb plain', '…');
      moreBtn.setAttribute('aria-label', `${item.name} 更多操作`);
      moreBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        menuLogoId = menuLogoId === item.id ? null : item.id;
        confirmingLogoId = null;
        render();
      });
      rowTop.append(moreBtn);
      wrap.append(rowTop);

      if (confirmingLogoId === item.id) {
        const confirmRow = el('div', 'logo-row-acts');
        confirmRow.append(
          el('span', 'small grow', `删除「${item.name}」？用到它的边框会改回 GXR 标志。`),
        );
        const cancelDel = button('btn', '取消');
        cancelDel.addEventListener('click', () => {
          confirmingLogoId = null;
          render();
        });
        const doDel = button('btn danger', '删除');
        doDel.addEventListener('click', async () => {
          if (busy) return;
          busy = true;
          await logoStore.remove(item.id);
          busy = false;
          confirmingLogoId = null;
          menuLogoId = null;
          if (photo.border.logo === item.id) {
            commitFrame(photo, { logo: 'ricoh', show_logo: true }, onUpdate);
          } else {
            onUpdate();
          }
          showToast(host, `已删除「${item.name}」。`);
          render();
        });
        confirmRow.append(cancelDel, doDel);
        wrap.append(confirmRow);
      } else if (menuLogoId === item.id) {
        const actRow = el('div', 'logo-row-acts');
        const renameBtn = button('btn', '重命名');
        renameBtn.addEventListener('click', () => {
          menuLogoId = null;
          editingLogoId = item.id;
          render();
        });
        const delBtn = button('btn danger', '删除');
        delBtn.addEventListener('click', () => {
          confirmingLogoId = item.id;
          render();
        });
        actRow.append(renameBtn, delBtn);
        wrap.append(actRow);
      }

      body.append(wrap);
    }

    // 自定义标志当前颜色/大小快捷设置
    const isCustom = activeLogo !== 'ricoh' && activeLogo !== 'none' && Boolean(logoStore.info(activeLogo));
    if (isCustom) {
      const modeRow = el('div', 'fsrow');
      modeRow.append(el('span', 'fslab', '标志颜色'));
      modeRow.append(
        segGroup<LogoMode>(
          [
            ['auto', '自动'],
            ['orig', '原色'],
            ['mono', '单色'],
          ],
          cur.logo_mode,
          '标志颜色',
          (logo_mode) => {
            commitFrame(photo, { logo_mode }, onUpdate);
            render();
          },
        ),
      );
      const sizeRow = el('div', 'fsrow');
      sizeRow.append(el('span', 'fslab', '大小'));
      sizeRow.append(
        segGroup<LogoSize>(
          [
            ['small', '小'],
            ['medium', '中'],
            ['large', '大'],
          ],
          cur.logo_size,
          '大小',
          (logo_size) => {
            commitFrame(photo, { logo_size }, onUpdate);
            render();
          },
        ),
      );
      body.append(modeRow, sizeRow);
    }

    const addWrap = el('div', 'fsrow');
    const addBtn = button('btn primary block', '添加标志');
    addBtn.dataset.logoAction = 'upload';
    addBtn.addEventListener('click', () => {
      if (saved.length >= LOGO_LIMITS.maxLogos) {
        showToast(host, LOGO_ERR.limit);
        return;
      }
      viewMode = 'upload';
      render();
    });
    addWrap.append(addBtn);
    body.append(addWrap);

    sheet.replaceChildren(el('div', 'grab'), hd, body);
  };

  const renderUpload = (): void => {
    sheet.dataset.state = 'logo-upload';
    const hd = el('div', 'hd');
    const backBtn = button('nb', '返回');
    backBtn.addEventListener('click', () => {
      viewMode = 'list';
      render();
    });
    const cancelBtn = button('nb', '取消');
    cancelBtn.addEventListener('click', close);
    hd.append(backBtn, el('h2', '', '添加标志'), cancelBtn);

    const body = el('div', 'ms-body');

    let currentFile: File | null = null;
    let origData: RgbaPixels | null = null;
    let whiteBg = false;
    let origOpaque = false;
    let nameTouched = false;
    let uploadBusy = false;

    const fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.accept = 'image/png,image/jpeg,image/webp';
    fileInput.className = 'filepick';
    fileInput.setAttribute('aria-label', '选择标志图片');

    const pickBtn = button('btn', '选择图片…');
    pickBtn.addEventListener('click', () => fileInput.click());
    const fileLabel = el('span', 'small dim grow', '支持 PNG、JPG、WebP（不超过 5 MB）');
    const pickRow = el('div', 'fsline');
    pickRow.append(pickBtn, fileLabel);

    const leftLab = el('small', '', '你选的图片');
    const rightLab = el('small', '', '裁剪边缘后');
    const leftCanvas = document.createElement('canvas');
    leftCanvas.className = 'logo-pv-canvas';
    leftCanvas.width = 248;
    leftCanvas.height = 164;
    const rightCanvas = document.createElement('canvas');
    rightCanvas.className = 'logo-pv-canvas';
    rightCanvas.width = 248;
    rightCanvas.height = 164;
    drawCheckerPreview(leftCanvas, null);
    drawCheckerPreview(rightCanvas, null);

    const pvGrid = el('div', 'logo-pv-grid');
    const leftCol = el('div', 'logo-pv-col');
    leftCol.append(leftLab, leftCanvas);
    const rightCol = el('div', 'logo-pv-col');
    rightCol.append(rightLab, rightCanvas);
    pvGrid.append(leftCol, rightCol);

    const msgEl = el('p', 'ffine');
    msgEl.hidden = true;
    msgEl.setAttribute('role', 'status');

    const keySwitch = document.createElement('input');
    keySwitch.type = 'checkbox';
    keySwitch.className = 'switch';
    keySwitch.disabled = true;
    keySwitch.setAttribute('aria-label', '去掉白底');
    const keyRow = el('label', 'swrow');
    keyRow.append(el('span', 'fslab', '去掉白底'), keySwitch);

    const tolInput = document.createElement('input');
    tolInput.type = 'range';
    tolInput.min = '0';
    tolInput.max = '60';
    tolInput.step = '1';
    tolInput.value = '30';
    tolInput.disabled = true;
    tolInput.setAttribute('aria-label', '容差');
    const tolOut = el('span', 'num small', '30');
    const tolRow = el('div', 'logo-tol-row');
    tolRow.append(el('span', 'fslab', '容差'), tolInput, tolOut);

    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.className = 'txtbox';
    nameInput.maxLength = LOGO_LIMITS.nameMax;
    nameInput.placeholder = '例如：我的签名';
    nameInput.setAttribute('aria-label', '名称');
    nameInput.addEventListener('input', () => {
      nameTouched = true;
    });
    const nameRow = el('div', 'fsrow');
    nameRow.append(el('span', 'fslab', '名称'), nameInput);

    const saveBtn = button('btn primary block', '保存并使用');
    saveBtn.dataset.logoAction = 'save';
    saveBtn.disabled = true;

    const refreshPreview = () => {
      msgEl.hidden = true;
      msgEl.className = 'ffine';
      if (!origData) {
        drawCheckerPreview(leftCanvas, null);
        drawCheckerPreview(rightCanvas, null);
        tolInput.disabled = true;
        return;
      }
      const keyOn = keySwitch.checked;
      const tol = Number(tolInput.value) || 0;
      tolOut.textContent = String(tol);
      tolInput.disabled = !keyOn;
      leftLab.textContent = whiteBg ? '你选的图片（有白底）' : '你选的图片';
      rightLab.textContent = keyOn ? '去掉白底后' : '裁剪边缘后';
      drawCheckerPreview(leftCanvas, origData, { cropToInk: false });
      const processed = keyOn ? keyWhiteImageData(origData, tol) : origData;
      drawCheckerPreview(rightCanvas, processed, { cropToInk: true });
      if (whiteBg && keyOn) {
        msgEl.textContent = LOGO_WARN.whiteBg;
        msgEl.hidden = false;
      } else if (origOpaque && !keyOn) {
        msgEl.textContent = LOGO_WARN.opaque;
        msgEl.hidden = false;
      }
    };

    keySwitch.addEventListener('change', refreshPreview);
    tolInput.addEventListener('input', refreshPreview);

    fileInput.addEventListener('change', () => {
      const file = fileInput.files?.[0];
      fileInput.value = '';
      if (!file) return;
      currentFile = null;
      origData = null;
      saveBtn.disabled = true;
      keySwitch.disabled = true;
      tolInput.disabled = true;
      fileLabel.textContent = file.name;
      if (file.size > LOGO_LIMITS.bytes) {
        refreshPreview();
        msgEl.className = 'ferr';
        msgEl.textContent = LOGO_ERR.tooBig;
        msgEl.hidden = false;
        showToast(host, LOGO_ERR.tooBig);
        return;
      }
      void previewLogo(file).then((res) => {
        if (!res.ok) {
          refreshPreview();
          msgEl.className = 'ferr';
          msgEl.textContent = res.error;
          msgEl.hidden = false;
          showToast(host, res.error);
          return;
        }
        currentFile = file;
        origData = res.imgData;
        whiteBg = res.whiteBg;
        origOpaque = res.origOpaque;
        keySwitch.disabled = !origOpaque;
        keySwitch.checked = whiteBg;
        tolInput.value = '30';
        if (!nameTouched || !nameInput.value.trim()) {
          nameInput.value = stemOf(file.name) || '我的标志';
        }
        saveBtn.disabled = false;
        refreshPreview();
      });
    });

    saveBtn.addEventListener('click', () => {
      if (uploadBusy || !currentFile) return;
      const chosenName = cleanLogoName(nameInput.value.trim(), currentFile.name);
      uploadBusy = true;
      saveBtn.disabled = true;
      void logoStore
        .add(currentFile, {
          name: chosenName,
          keyWhite: keySwitch.checked,
          tol: Number(tolInput.value) || 0,
        })
        .then((res) => {
          uploadBusy = false;
          if (!res.ok) {
            saveBtn.disabled = false;
            msgEl.className = 'ferr';
            msgEl.textContent = res.error;
            msgEl.hidden = false;
            showToast(host, res.error);
            return;
          }
          commitFrame(photo, { logo: res.logo.id, show_logo: true }, onUpdate);
          close();
          showToast(host, `已保存「${res.logo.name}」，并用在这个边框上。`);
          const monoMsg = autoMonoMessage(photo.border, res.logo);
          if (monoMsg) showToast(host, monoMsg);
        });
    });

    const saveRow = el('div', 'fsrow');
    saveRow.append(saveBtn);

    body.append(
      fileInput,
      pickRow,
      pvGrid,
      msgEl,
      el('div', 'fsrow'),
      nameRow,
      el('p', 'ffine', '标志只保存在这台手机上，不会上传。透明背景的 PNG 效果最好。'),
      saveRow,
    );
    const keyTolWrap = body.children[4] as HTMLElement;
    keyTolWrap.append(keyRow, tolRow);

    sheet.replaceChildren(el('div', 'grab'), hd, body);
  };

  const render = (): void => {
    if (viewMode === 'upload') renderUpload();
    else renderList();
  };

  const unsub = logoStore.onChange(() => {
    if (sheet.isConnected && viewMode === 'list') renderList();
  });

  scrim.addEventListener('click', close);
  host.append(scrim, sheet);
  void logoStore.ensureLoaded().then(() => {
    if (sheet.isConnected && viewMode === 'list') renderList();
  });
  render();
}
