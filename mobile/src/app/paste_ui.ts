// 复制调整（04a）、套用到哪里（04b）、套用前确认（04c）和网格底栏（02a「已复制」、04d、04e）。
// 字都是设计稿和桌面 paste_core.js 的原话；逻辑在 lib/paste.ts 和 paste_state.ts。

import { unitOfLens } from '../lib/calib';
import { copyrightFromJpeg, mergeCopyright, mergeMeta } from '../lib/exif';
import { formatClock, formatDay, groupByGap } from '../lib/group';
import { presetLabel, rectFromArray } from '../lib/crop';
import { FRAME_NAMES } from '../lib/frame_spec';
import { findFirstHalfNeighbor } from '../lib/halfframe';
import { formatShootingRecipeTag, parseRecipeTag } from '../lib/recipe_tags';
import { HDR_NO_RAW } from '../lib/hdr_text';
import { loadExportHdr, saveExportHdr, watchExportHdr } from '../lib/prefs';
import { adoptedSelectionOf, DEFAULT_CARD, namesOnly, photoWithAdopted, recipeUnitOf, shelf, shelves, tagAmountLevels } from '../lib/recipes';
import { NOT_SET, NOTHING_COPIED, ONLY_SOURCE, PASTE_GROUPS, chosenGroups, rememberGroups, type GroupId } from '../lib/paste_text';
import { curveSummary, groupsPresent } from '../lib/paste';
import { button, el, icon, ICON } from './dom';
import { ensureCameraRecipe, needsCameraRecipe, onCameraRecipe, photoMs, type AppApi, type Knobs, type Photo } from './model';
import { groupsAnd, groupsText, pasteState, rangeOf } from './paste_state';
import { showToast } from './toast';
import './paste.css';

export const PASTE_ICON = {
  check: 'M5 12.5l4.5 4.5L19 7.5',
  copy: 'M8.5 8.5h11.5v11.5H8.5zM15.5 8.5V7A2.5 2.5 0 0 0 13 4.5H7A2.5 2.5 0 0 0 4.5 7v6A2.5 2.5 0 0 0 7 15.5h1.5',
  undo: 'M9 14L4 9l5-5M4 9h9a6 6 0 0 1 0 12h-3',
  pencil: 'M4 20l1-4L16.5 4.5a2 2 0 0 1 3 3L8 19z',
} as const;

/** 调整的名字和格式（编辑器 SLIDERS 的名字、formatKnob 的写法）。 */
const KNOB_LABEL: Array<[keyof Knobs, string]> = [
  ['exposure_ev', '曝光'],
  ['contrast', '对比度'],
  ['highlight', '高光'],
  ['shadow', '阴影'],
  ['black_point', '黑点'],
  ['warm', '色温'],
  ['tint', '色调'],
  ['saturation', '饱和度'],
  ['vibrance', '自然饱和度'],
  ['vignette', '暗角'],
];

function knobText(key: keyof Knobs, value: number): string {
  if (key === 'exposure_ev') return value > 0 ? `+${value.toFixed(1)}` : value.toFixed(1);
  return value > 0 ? `+${value}` : String(value);
}

/** 04b 那句「Kodachrome 25，曝光 +0.7」：配方的名字、动过的调整、拉直、裁剪比例、边框与时间戳。 */
export function clipSummary(api: AppApi): string {
  const clip = pasteState.clip;
  if (!clip) return '';
  const parts: string[] = [];
  const dat = api.book().dat;
  if (clip.recipe) {
    const card = dat ? shelf(shelves(dat.listing, clip.recipe.choice.unit), 'all').find((item) => item.id === clip.recipe?.choice.id) : null;
    if (card) parts.push(card.name);
  }
  if (clip.lut) {
    parts.push(api.lutBook().get(clip.lut)?.name ?? 'LUT 已删除');
  }
  if (clip.knobs) {
    for (const [key, label] of KNOB_LABEL) {
      const value = clip.knobs[key] ?? 0;
      if (value !== 0) parts.push(`${label} ${knobText(key, value)}`);
    }
  }
  const curves = curveSummary(clip.curve);
  if (curves) parts.push(curves);
  if (clip.geometry && clip.geometry.straighten !== 0) {
    const v = clip.geometry.straighten;
    parts.push(`拉直 ${v > 0 ? `+${v.toFixed(1)}` : v.toFixed(1)}°`);
  }
  if (clip.crop) {
    const srcFrame = api.photo(clip.from.id)?.frame;
    const canvas = srcFrame ? { w: srcFrame.width, h: srcFrame.height } : null;
    parts.push(presetLabel(clip.crop.preset, rectFromArray(clip.crop.rect), canvas));
  }
  if (clip.border && clip.border.style !== 'none') {
    parts.push(FRAME_NAMES[clip.border.style]);
  }
  if (clip.stamp && clip.stamp.enabled) {
    parts.push('时间戳');
  }
  return parts.join('，');
}

/** 照片现在的分组（当前的间隔和顺序）。 */
export function groupsNow(api: AppApi): ReturnType<typeof groupByGap> {
  return groupByGap(
    api.photos().map((photo) => ({ id: photo.id, ms: photoMs(photo) })),
    api.gap(),
    api.order(),
  );
}

function thumbs(api: AppApi, ids: readonly string[], source: string | null, small = false): HTMLElement {
  const row = el('div', small ? 'pthumbs sm' : 'pthumbs');
  for (const id of ids.slice(0, 6)) {
    const photo = api.photo(id);
    const box = el('div', id === source ? 't src' : 't');
    box.dataset.id = id;
    if (photo?.bitmap) {
      const canvas = document.createElement('canvas');
      canvas.width = photo.bitmap.width;
      canvas.height = photo.bitmap.height;
      canvas.getContext('2d')?.drawImage(photo.bitmap, 0, 0);
      box.append(canvas);
    }
    row.append(box);
  }
  return row;
}

interface Overlay {
  close(): void;
}

function overlay(host: HTMLElement, node: HTMLElement, onScrim: () => void): Overlay {
  const scrim = el('div', 'scrim paste-scrim');
  scrim.addEventListener('click', onScrim);
  host.append(scrim, node);
  return {
    close: () => {
      scrim.remove();
      node.remove();
    },
  };
}

/** 04a：选要复制的组。勾选记在本机（桌面同一个键），没设置过的组标「这张没有设置」。 */
export function openCopySheet(host: HTMLElement, api: AppApi, photo: Photo, onCopied: () => void): void {
  closePasteSheets(host);
  const chosen = new Set<GroupId>(chosenGroups());
  let present = new Set(groupsPresent(photoWithAdopted(photo, api.book().dat)));
  const sheet = el('div', 'ms paste-sheet');
  sheet.dataset.state = 'copy-options';
  const head = el('div', 'hd');
  head.append(el('h2', '', '复制调整'));
  const cancel = button('nb', '取消');
  head.append(cancel);
  const rows = el('div', 'popts');
  const go = button('btn primary lg block', '');
  const note = el('p', 'pnote faint');
  note.hidden = true;
  const paint = (): void => {
    for (const row of rows.querySelectorAll<HTMLButtonElement>('.optrow')) {
      const on = chosen.has(row.dataset.group as GroupId);
      row.classList.toggle('on', on);
      row.setAttribute('aria-pressed', on ? 'true' : 'false');
      const cc = row.querySelector('.cc');
      cc?.classList.toggle('on', on);
      cc?.replaceChildren(...(on ? [icon(PASTE_ICON.check)] : []));
    }
    go.textContent = `复制 ${chosen.size} 项`;
    go.disabled = chosen.size === 0;
  };
  let recipeSmall: HTMLElement | null = null;
  let recipeHint = '';
  for (const [id, name, hint] of PASTE_GROUPS) {
    const row = button('optrow', '');
    row.dataset.group = id;
    const text = el('div', 'tx');
    const small = el('small', '', hint);
    if (!present.has(id)) small.append(el('i', '', NOT_SET));
    if (id === 'recipe') {
      recipeSmall = small;
      recipeHint = hint;
    }
    text.append(el('b', '', name), small);
    row.append(el('span', 'cc'), text);
    row.addEventListener('click', () => {
      if (chosen.has(id)) chosen.delete(id);
      else chosen.add(id);
      paint();
    });
    rows.append(row);
  }
  sheet.append(el('div', 'grab'), head, el('p', 'lead', '套用时只改勾选的项目，别的保持不变。勾选会记住，下次还是这几项。'), rows, note, go);
  const view = overlay(host, sheet, () => view.close());
  cancel.addEventListener('click', () => view.close());
  const ready = needsCameraRecipe(photo, api.book().dat)
    ? ensureCameraRecipe(photo, api.book()).then(() => {
        if (!sheet.isConnected || !recipeSmall) return;
        present = new Set(groupsPresent(photoWithAdopted(photo, api.book().dat)));
        recipeSmall.replaceChildren(document.createTextNode(recipeHint));
        if (!present.has('recipe')) recipeSmall.append(el('i', '', NOT_SET));
      })
    : null;
  const finishCopy = () => {
    const ids = PASTE_GROUPS.map((g) => g[0]).filter((id) => chosen.has(id));
    rememberGroups(ids);
    const result = pasteState.copy(api, photo, ids);
    if (!result.ok) {
      // 桌面：勾选的项目里没有修改，没有复制。剪贴板不变。
      note.textContent = result.line;
      note.hidden = false;
      return;
    }
    view.close();
    openWhereSheet(host, api, photo, result.line, onCopied);
  };
  go.addEventListener('click', () => {
    if (ready && needsCameraRecipe(photo, api.book().dat)) {
      void ready.then(() => {
        if (sheet.isConnected) finishCopy();
      });
      return;
    }
    finishCopy();
  });
  paint();
}

/** 04b：复制好了，套用到这一组、自己选，或先不套用。 */
export function openWhereSheet(host: HTMLElement, api: AppApi, photo: Photo, title: string, onApplied: () => void): void {
  closePasteSheets(host);
  const sheet = el('div', 'ms paste-sheet');
  sheet.dataset.state = 'copied-where';
  const head = el('div', 'hd lead-row');
  const ok = el('span', 'okc');
  ok.append(icon(PASTE_ICON.check));
  head.append(ok, el('h2', '', title));
  const summary = clipSummary(api);
  const lead = el('p', 'lead', `来自 ${photo.stem}${summary ? `：${summary}` : ''}。要套用到哪些照片？`);
  const group = groupsNow(api).find((item) => item.ids.includes(photo.id));
  const ids = group?.ids ?? [photo.id];
  const targets = pasteState.targets(api, ids);
  const card = el('div', 'gpick');
  const ghd = el('div', 'ghd');
  ghd.append(el('b', 'num', group?.rangeLabel ?? ''), el('span', 'faint', `这张所在的一组 · ${ids.length} 张`));
  const apply = button('btn primary lg block', `套用到这一组（${targets.length} 张）`);
  apply.dataset.act = 'apply-group';
  apply.disabled = targets.length === 0;
  card.append(ghd, thumbs(api, ids, photo.id), apply, el('p', 'gapline faint', `按拍摄时间自动分组：前后隔不到 ${api.gap()} 分钟的算一组`));
  const acts = el('div', 'acts');
  const pick = button('btn lg', '选照片…');
  const later = button('btn lg ghost', '先不套用');
  acts.append(pick, later);
  sheet.append(el('div', 'grab'), head, lead, card, acts, el('p', 'pfoot faint', '先不套用时，复制的内容会留着：回到照片网格，点组标题上的「套用到这一组」。'));
  const view = overlay(host, sheet, () => view.close());
  later.addEventListener('click', () => view.close());
  pick.addEventListener('click', () => {
    view.close();
    pasteState.pickNext = true;
    api.back();
  });
  apply.addEventListener('click', () => {
    view.close();
    openConfirm(host, api, {
      title: `套用到这一组的 ${targets.length} 张？`,
      body: groupBody(group?.rangeLabel ?? '', targets.length, true, photo.stem),
      targets,
      range: group?.rangeLabel ?? rangeOf(targets),
      afterApply: onApplied,
      leaveFirst: true,
    });
  });
}

/** 04c 的说明句。来源在这一组里时用设计稿的原句；否则去掉「除了这张以外」。 */
export function groupBody(range: string, n: number, sourceInside: boolean, stem: string): string {
  const groups = pasteState.clip ? groupsAnd(pasteState.clip.groups) : '';
  const who = sourceInside ? `${range} 这一组里，除了这张以外的 ${n} 张` : `${range} 这一组的 ${n} 张`;
  return `${who}会换成 ${stem} 的${groups}。别的设置不变。`;
}

/** 选的照片（04d、选择栏）的说明句。 */
export function pickBody(n: number, stem: string): string {
  const groups = pasteState.clip ? groupsAnd(pasteState.clip.groups) : '';
  return `选中的 ${n} 张会换成 ${stem} 的${groups}。别的设置不变。`;
}

export interface ConfirmOptions {
  title: string;
  body: string;
  targets: Photo[];
  range: string;
  afterApply: () => void;
  /** 从编辑器套用：先回到网格，结果写在网格的底栏上（设计稿 04e）。 */
  leaveFirst?: boolean;
  /** 点了「套用到 N 张」、开始套用之前（选择模式在这时退出）。 */
  onGo?: () => void;
}

/** 04c：套用前确认。 */
export function openConfirm(host: HTMLElement, api: AppApi, options: ConfirmOptions): void {
  closePasteSheets(host);
  const dlg = el('div', 'dlg paste-dlg');
  dlg.dataset.state = 'confirm';
  const acts = el('div', 'acts');
  const cancel = button('btn lg', '取消');
  const go = button('btn primary lg', `套用到 ${options.targets.length} 张`);
  go.dataset.act = 'confirm';
  acts.append(cancel, go);
  const strip = el('div', 'pcenter');
  strip.append(thumbs(api, options.targets.map((photo) => photo.id), null, true));
  dlg.append(el('h2', '', options.title), el('p', '', options.body), strip, el('p', 'pundo', '套用后可以撤销。存储卡上的照片不会变。'), acts);
  const view = overlay(host, dlg, () => view.close());
  cancel.addEventListener('click', () => view.close());
  go.addEventListener('click', () => {
    view.close();
    options.onGo?.();
    if (options.leaveFirst) api.back();
    void pasteState.apply(api, options.targets, options.range).then(() => options.afterApply());
  });
}

export function closePasteSheets(host: HTMLElement): void {
  for (const node of host.querySelectorAll('.paste-scrim, .paste-sheet, .paste-dlg')) node.remove();
}

/** 网格底栏：已复制（02a）、套用结果和撤销（04e）、一行提示。没有要写的就是 null。 */
export function pasteBar(api: AppApi, onChange: () => void): HTMLElement | null {
  const bar = pasteState.bar;
  const clip = pasteState.clip;
  const dock = el('div', 'dock pastebar');
  if (bar?.kind === 'applied') {
    dock.dataset.state = 'applied';
    const ok = el('span', 'okc');
    ok.append(icon(PASTE_ICON.check));
    const text = el('div', 'tx');
    text.append(el('b', '', bar.line), el('small', '', bar.sub));
    const undo = button('fbtn ctl', '撤销');
    undo.prepend(icon(PASTE_ICON.undo));
    undo.dataset.act = 'undo';
    undo.addEventListener('click', () => {
      pasteState.revert(api);
      onChange();
    });
    dock.append(ok, text, undo);
    return dock;
  }
  if (bar?.kind === 'info') {
    dock.dataset.state = 'info';
    const text = el('div', 'tx');
    text.append(el('b', '', bar.line));
    dock.append(text);
    return dock;
  }
  if (!clip) return null;
  dock.dataset.state = 'copied';
  const acc = el('span', 'accc');
  acc.append(icon(PASTE_ICON.copy));
  const text = el('div', 'tx');
  text.append(el('b', '', `已复制：${groupsText(clip.groups)}`), el('small', '', `来自 ${clip.from.stem} · 点组标题上的「套用到这一组」`));
  const drop = button('nb', '清除');
  drop.dataset.act = 'clear-clip';
  drop.addEventListener('click', () => {
    pasteState.drop();
    onChange();
  });
  dock.append(acc, text, drop);
  return dock;
}

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
  const dat = api.book().dat;
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
  return '中性转换';
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
        // 延迟导入导出与关机图模块，避免网格首图阶段经 paste_ui 提前拉取 export_ui。
        void import('./export_ui').then(({ openExportOptions }) => {
          openExportOptions(host, [photo.id]);
        });
      },
    ],
    [
      'more-off-image',
      '做成关机图',
      '生成 640×480 的 GXROFF.JPG',
      () => {
        void import('./off_image_sheet').then(({ openOffImageSheet }) => {
          openOffImageSheet(host, api, photo);
        });
      },
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
      offHdr();
      offBook();
      offCam();
    },
  };
}
