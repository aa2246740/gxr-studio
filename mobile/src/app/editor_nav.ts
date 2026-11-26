// 编辑器顶栏（返回、配方名与第几张、HDR、⋯）、⋯ 菜单和照片信息面板。只在编辑器分包里：⋯ 菜单直接打开导出选项和关机图。
// 从 paste_ui.ts 原样移出，网格那边的复制 / 套用不再连带导出模块。

import { unitOfLens } from '../lib/calib';
import { copyrightFromJpeg, mergeCopyright, mergeMeta } from '../lib/exif';
import { formatClock, formatDay } from '../lib/group';
import { findFirstHalfNeighbor } from '../lib/halfframe';
import { formatShootingRecipeTag, parseRecipeTag } from '../lib/recipe_tags';
import { HDR_NO_RAW } from '../lib/hdr_text';
import { loadExportHdr, saveExportHdr, watchExportHdr } from '../lib/prefs';
import { adoptedSelectionOf, DEFAULT_CARD, namesOnly, recipeUnitOf, shelf, shelves, tagAmountLevels } from '../lib/recipes';
import { NOTHING_COPIED, ONLY_SOURCE } from '../lib/paste_text';
import { button, el, icon, ICON } from './dom';
import { arrowsOwned, sheetOpen } from './keys';
import { openExportOptions } from './export_ui';
import { onCameraRecipe, photoMs, type AppApi, type Photo } from './model';
import { openOffImageSheet } from './off_image_sheet';
import { closePasteSheets, groupBody, groupsNow, openConfirm, openCopySheet, overlay } from './paste_ui';
import { groupsAnd, groupsText, pasteState, rangeOf } from './paste_state';
import { showToast } from './toast';

const jpegCopyrightCache = new WeakMap<File, Promise<string | null>>();

async function photoCopyright(photo: Photo): Promise<string | null> {
  const known = mergeCopyright(photo.jpgMeta?.copyright, photo.dngMeta?.copyright);
  if (known !== null) return known;
  if (!photo.jpg) return null;
  let pending = jpegCopyrightCache.get(photo.jpg);
  if (!pending) {
    pending = photo.jpg
      .slice(0, 128 << 10)
      .arrayBuffer()
      .then((buf) => copyrightFromJpeg(new Uint8Array(buf)))
      .catch(() => null);
    jpegCopyrightCache.set(photo.jpg, pending);
  }
  return pending;
}

/** 当前照片生效的配方名（用于编辑器顶栏与照片信息；绝不返回文件名）。 */
export function currentRecipeLabel(api: AppApi, photo: Photo): string {
  if (photo.diptych) return '半格双联';
  if (!photo.dng) return '相机 JPG';
  if (photo.recipeOff) return '中性转换';
  const book = api.book();
  const dat = typeof book.usableDatFor === 'function' ? book.usableDatFor(photo) : book.dat;
  const adopted = adoptedSelectionOf(photo, dat);
  const sel = photo.choice ?? adopted?.choice ?? null;
  if (dat) {
    const photoUnit = unitOfLens(mergeMeta(photo.jpgMeta, photo.dngMeta).lens);
    const unit = recipeUnitOf(photo.choice, adopted, photoUnit);
    const cards = shelf(shelves(dat.listing, unit), 'all');
    const id = sel?.id ?? DEFAULT_CARD;
    const card = cards.find((c) => c.id === id) ?? cards[0] ?? null;
    if (card?.name) return card.name;
  }
  if (photo.choice) {
    const named = namesOnly().find((c) => c.id === photo.choice?.id);
    if (named?.name) return named.name;
  }
  return !book.dat && photo.jpg ? '相机 JPG' : '中性转换';
}

/** 照片信息底部面板：保留文件名、拍摄时间、格式、当前配方、尺寸、镜头和 ISO；半格双联补每格拍摄配方。 */
export function openPhotoInfoSheet(host: HTMLElement, api: AppApi, photo: Photo): void {
  closePasteSheets(host);
  const sheet = el('div', 'ms paste-sheet');
  sheet.dataset.state = 'photo-info';
  const head = el('div', 'hd');
  head.append(el('h2', '', '照片信息'));
  const done = button('nb', '完成');
  head.append(done);
  const view = overlay(host, sheet, () => view.close());
  done.addEventListener('click', () => view.close());

  const ms = photoMs(photo);
  const meta = mergeMeta(photo.jpgMeta, photo.dngMeta);
  const names = [photo.dng?.name, photo.jpg?.name].filter((v): v is string => Boolean(v));
  const format = photo.diptych
    ? '相机 JPG（半格双联）'
    : photo.dng && photo.jpg
      ? 'RAW + JPG'
      : photo.dng
        ? '只有 RAW'
        : '只有 JPG';
  const rows: Array<[string, string]> = [
    ['文件名', names.length ? names.join(' · ') : photo.stem],
    ['拍摄时间', `${formatDay(ms)} ${formatClock(ms)}`],
    ['格式', format],
    ['当前配方', currentRecipeLabel(api, photo)],
  ];
  const tailRows: Array<[string, string]> = [];
  if (photo.frame) tailRows.push(['尺寸', `${photo.frame.width} × ${photo.frame.height}`]);
  if (meta.lens) tailRows.push(['镜头', meta.lens]);
  if (meta.iso != null) tailRows.push(['ISO', `ISO ${meta.iso}`]);

  const appendRow = (label: string, value: string): void => {
    const line = el('div', 'lrow');
    const text = el('div');
    text.append(el('b', '', label), el('small', 'num', value));
    line.append(text);
    sheet.append(line);
  };

  sheet.append(el('div', 'grab'), head);
  for (const [label, value] of rows) appendRow(label, value);

  if (photo.diptych) {
    const line = el('div', 'lrow');
    line.dataset.role = 'halfframe-recipes';
    const text = el('div');
    const fmtTag = (raw: string | null | undefined) => formatShootingRecipeTag(raw, tagAmountLevels(parseRecipeTag(raw), api.book().dat));
    const secondKnown = mergeCopyright(photo.jpgMeta?.copyright, photo.dngMeta?.copyright);
    const secondEl = el('small', 'num', `第 2 格：${fmtTag(secondKnown)}`);
    secondEl.dataset.half = 'second';
    text.append(el('b', '', '拍摄配方'), secondEl);
    line.append(text);
    sheet.append(line);

    const library = api.photos();
    void (async () => {
      if (secondKnown === null && photo.jpg) {
        const secondTag = await photoCopyright(photo);
        if (sheet.isConnected) secondEl.textContent = `第 2 格：${fmtTag(secondTag)}`;
      }
      const prev = await findFirstHalfNeighbor(photo, library);
      if (!sheet.isConnected) return;
      if (!prev) {
        line.dataset.firstChecked = 'absent';
        return;
      }
      const firstTag = await photoCopyright(prev);
      if (!sheet.isConnected) return;
      const firstEl = el('small', 'num', `第 1 格：${fmtTag(firstTag)}`);
      firstEl.dataset.half = 'first';
      text.insertBefore(firstEl, secondEl);
      line.dataset.firstChecked = 'ready';
    })();
  }

  for (const [label, value] of tailRows) appendRow(label, value);
}

/** 编辑器顶栏 ⋯ 菜单：复制调整、粘贴、套用到这一组、导出选项、照片信息，全部复用既有面板。 */
export function openEditorMoreSheet(host: HTMLElement, api: AppApi, photo: Photo): void {
  closePasteSheets(host);
  const isIpad = Boolean(host.dataset.layout?.startsWith('ipad'));
  const sheet = el('div', isIpad ? 'ms paste-sheet popover' : 'ms paste-sheet');
  sheet.dataset.state = 'editor-more';
  if (isIpad) sheet.dataset.popover = '1';
  const head = el('div', 'hd');
  head.append(el('h2', '', '更多'));
  const done = button('nb', '完成');
  head.append(done);
  const view = overlay(host, sheet, () => view.close());
  done.addEventListener('click', () => view.close());

  const clip = pasteState.clip;
  const group = groupsNow(api).find((item) => item.ids.includes(photo.id));
  const groupDetail = group ? `${group.rangeLabel} · ${group.ids.length} 张` : '当前照片所在分组';

  const entries: Array<[string, string, string, () => void]> = [
    [
      'more-copy',
      '复制调整',
      '选要复制的项目，套用到别的照片',
      () => openCopySheet(host, api, photo, () => undefined),
    ],
  ];
  if (clip) {
    entries.push(
      [
        'more-paste',
        '粘贴',
        `来自 ${clip.from.stem} · ${groupsText(clip.groups)}`,
        () => {
          const current = pasteState.clip;
          if (!current) {
            showToast(host, NOTHING_COPIED);
            return;
          }
          const targets = pasteState.targets(api, [photo.id]);
          if (!targets.length) {
            showToast(host, ONLY_SOURCE);
            return;
          }
          openConfirm(host, api, {
            title: '套用到这张？',
            body: `这张会换成 ${current.from.stem} 的${groupsAnd(current.groups)}。别的设置不变。`,
            targets,
            range: rangeOf(targets),
            afterApply: () => undefined,
            leaveFirst: false,
          });
        },
      ],
      [
        'more-apply-group',
        '套用到这一组',
        groupDetail,
        () => {
          const current = pasteState.clip;
          if (!current) {
            showToast(host, NOTHING_COPIED);
            return;
          }
          const nowGroup = groupsNow(api).find((item) => item.ids.includes(photo.id));
          const ids = nowGroup?.ids ?? [photo.id];
          const targets = pasteState.targets(api, ids);
          if (!targets.length) {
            showToast(host, ONLY_SOURCE);
            return;
          }
          openConfirm(host, api, {
            title: `套用到这一组的 ${targets.length} 张？`,
            body: groupBody(nowGroup?.rangeLabel ?? '', targets.length, ids.includes(current.from.id), current.from.stem),
            targets,
            range: nowGroup?.rangeLabel ?? rangeOf(targets),
            afterApply: () => undefined,
            leaveFirst: true,
          });
        },
      ],
    );
  }
  entries.push(
    [
      'more-export-options',
      '导出选项',
      '尺寸、品质与 HDR 说明',
      () => {
        openExportOptions(host, [photo.id]);
      },
    ],
    [
      'more-off-image',
      '做成关机图',
      '生成 640×480 的 GXROFF.JPG',
      () => openOffImageSheet(host, api, photo),
    ],
    [
      'more-photo-info',
      '照片信息',
      '文件名、拍摄时间与镜头',
      () => openPhotoInfoSheet(host, api, photo),
    ],
  );

  sheet.append(isIpad ? el('i', 'arrow up') : el('div', 'grab'), head);
  for (const [act, label, detail, run] of entries) {
    const line = button('lrow', '');
    line.dataset.act = act;
    const text = el('div');
    text.append(el('b', '', label), el('small', '', detail));
    line.append(text);
    line.addEventListener('click', () => {
      view.close();
      run();
    });
    sheet.append(line);
  }
}

export interface EditorNavHandle {
  el: HTMLElement;
  refresh(): void;
  setHolding(on: boolean): void;
  destroy(): void;
}

/** 编辑器顶栏：返回「照片」、中间配方名 +「第几张 / 共几张」（不含文件名，长按看原图时第二行切为「原图」）、右侧 HDR 开关与可用 ⋯。 */
export function mountEditorNav(shell: HTMLElement, api: AppApi, photo: Photo, index: number, total: number): EditorNavHandle {
  const nav = el('header', 'nav');
  const back = button('nb nav-back', '');
  back.setAttribute('aria-label', '照片');
  back.append(icon(ICON.back), el('span', 'nav-back-lab', '照片'));
  back.addEventListener('click', () => api.back());

  const title = el('div', 't');
  const recipeEl = el('b', '', currentRecipeLabel(api, photo));
  const posText = `${index + 1} / ${total}`;
  const posEl = el('small', 'num', posText);
  title.append(recipeEl, posEl);

  const right = el('div', 'nav-r');
  const canHdr = photo.dng !== null;
  const hdrWrap = el('span', 'hdr-wrap');
  const hdrBtn = button('hdr-toggle', 'HDR');
  hdrBtn.dataset.act = 'hdr-toggle';
  hdrBtn.setAttribute('role', 'switch');
  hdrBtn.setAttribute('aria-label', 'HDR');
  const syncHdr = () => {
    if (!canHdr) {
      hdrBtn.disabled = true;
      hdrBtn.classList.remove('on');
      hdrBtn.setAttribute('aria-checked', 'false');
      hdrBtn.dataset.reason = HDR_NO_RAW;
      hdrBtn.title = HDR_NO_RAW;
      return;
    }
    const on = loadExportHdr();
    hdrBtn.disabled = false;
    hdrBtn.classList.toggle('on', on);
    hdrBtn.setAttribute('aria-checked', on ? 'true' : 'false');
    delete hdrBtn.dataset.reason;
    hdrBtn.removeAttribute('title');
  };
  syncHdr();
  hdrBtn.addEventListener('click', () => {
    if (!canHdr) return;
    saveExportHdr(!loadExportHdr());
  });
  hdrWrap.addEventListener('click', () => {
    if (!canHdr) showToast(shell, HDR_NO_RAW);
  });
  hdrWrap.append(hdrBtn);

  const more = button('nb plain', '…');
  more.dataset.act = 'editor-more';
  more.setAttribute('aria-label', '更多');
  more.addEventListener('click', () => openEditorMoreSheet(shell, api, photo));

  right.append(hdrWrap, more);
  nav.append(back, title, right);

  const refresh = () => {
    recipeEl.textContent = currentRecipeLabel(api, photo);
    syncHdr();
  };
  const setHolding = (on: boolean) => {
    posEl.textContent = on ? '原图' : posText;
    posEl.classList.toggle('holding', on);
  };
  // 电脑键盘：焦点不在输入框、滑块、旋转尺、菜单这类自己用方向键的控件上，也没开着面板时，←/→ 换上一张、下一张（同左右滑）。
  const onArrow = (ev: KeyboardEvent) => {
    if (ev.defaultPrevented || (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') || ev.altKey || ev.ctrlKey || ev.metaKey || ev.shiftKey) return;
    if (!nav.isConnected || sheetOpen() || arrowsOwned(ev.target)) return;
    ev.preventDefault();
    api.swipe(photo.id, ev.key === 'ArrowLeft' ? -1 : 1);
  };
  window.addEventListener('keydown', onArrow);
  const offHdr = watchExportHdr(syncHdr);
  const offBook = api.book().onChange(() => refresh());
  const offCam = onCameraRecipe((id) => {
    if (id === photo.id) refresh();
  });

  return {
    el: nav,
    refresh,
    setHolding,
    destroy: () => {
      window.removeEventListener('keydown', onArrow);
      offHdr();
      offBook();
      offCam();
    },
  };
}
