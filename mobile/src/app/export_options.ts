// 导出选项面板（05a）：尺寸、品质、HDR 说明与警告，以及单张成品像素估算。
// 编辑器「⋯ → 导出选项」（以及 U2「更多」）标题为「导出选项」，图库多选「导出 N 张」标题为「导出 N 张」。

import type { ExportCore } from '../core/export_api';
import { buildRequest, type QualityTier, type RequestPhoto, type SizeTier } from '../lib/export_request';
import { fontsNeeded } from '../lib/fonts';
import { formatClock, formatDay } from '../lib/group';
import { HALFFRAME_MODES, PAIR_EXPORT_NOTE, pairExportSizeText } from '../lib/halfframe';
import { HDR_NO_RAW, hdrAvailable, hdrFrameLine, hdrLine, hdrMixLine } from '../lib/hdr_text';
import { isDisplaySpace } from '../lib/lut';
import {
  loadExportHalfframe,
  loadExportHdr,
  loadExportQuality,
  loadExportSize,
  saveExportHalfframe,
  saveExportHdr,
  saveExportQuality,
  saveExportSize,
  watchExportHdr,
  type ExportHalfframePref,
} from '../lib/prefs';
import type { Card } from '../lib/recipes';
import { MANY_SIZES, SIZE_FAILED, SIZE_PENDING, sizeText } from '../lib/share_plan';
import type { Book } from './book';
import { button, el } from './dom';
import { EXPORT_ICON, exportIco } from './export_ready';
import { ensureFontPack } from './font_loader';
import { logoStore } from './logo_store';
import type { LutBook } from './lut_book';
import { ensureCameraRecipe, ensureCameraRecipes, photoMs, type Photo } from './model';

declare const __GXR_BUILD__: string;

const SIZES: Array<[SizeTier, string]> = [
  ['original', '原始尺寸'],
  ['large', '大'],
  ['medium', '中'],
  ['small', '小'],
];
const QUALITIES: Array<[QualityTier, string]> = [
  ['high', '高'],
  ['medium', '中'],
  ['low', '低'],
];
const NO_DAT_GUIDE = '未导入相机配方文件时，导出不会带上相机配方效果：有相机 JPG 的照片导出相机 JPG；只有 RAW、又套用了配方的照片可能导不出。';

function timeRange(photos: Photo[]): string {
  if (!photos.length) return 'JPG';
  const times = photos.map((p) => photoMs(p));
  const lo = Math.min(...times);
  const hi = Math.max(...times);
  const a = formatClock(lo);
  const b = formatClock(hi);
  if (formatDay(lo) !== formatDay(hi)) return `${formatDay(lo)} ${a} – ${formatDay(hi)} ${b} · JPG`;
  return `${a === b ? a : `${a}–${b}`} · JPG`;
}

function seg<T extends string>(items: Array<[T, string]>, value: T, pick: (v: T) => void): HTMLElement {
  const box = el('div', 'seg');
  box.setAttribute('role', 'group');
  for (const [v, label] of items) {
    const item = button(v === value ? 'on' : '', label);
    item.dataset.value = v;
    item.addEventListener('click', () => {
      for (const node of box.children) node.classList.toggle('on', node === item);
      pick(v);
    });
    box.append(item);
  }
  return box;
}

export interface ExportOptionsHost {
  shell: HTMLElement;
  core: ExportCore;
  book(): Book;
  lutBook?(): LutBook | undefined;
  cardsFor(book: Book, photo: Photo): Card[];
  requestPhoto(photo: Photo, lutBook?: LutBook, book?: Book): RequestPhoto;
  isBatchClosed(): boolean;
  onStart(photos: Photo[], options: { size: SizeTier; quality: QualityTier; hdr: boolean; halfframe?: ExportHalfframePref }): void;
  onClose(): void;
}

export interface OpenedExportSheet {
  nodes: HTMLElement[];
  cleanup: () => void;
}

/** 打开「导出选项」或「导出 N 张」底部面板；更改尺寸 / 品质 / 半格 / HDR 即刻记住，供单点「存到相册」复用。 */
export function mountExportSheet(
  host: ExportOptionsHost,
  photos: Photo[],
  opts: { title?: string; onStart?: () => void },
): OpenedExportSheet {
  let size: SizeTier = loadExportSize();
  let quality: QualityTier = loadExportQuality();
  const hasDiptych = photos.some((p) => Boolean(p.diptych));
  let halfframe: ExportHalfframePref = loadExportHalfframe();
  const n = photos.length;
  let active = true;

  const isIpadPopover = Boolean(host.shell.dataset.layout?.startsWith('ipad') && opts.title === '导出选项');
  const scrim = el('div', 'scrim');
  scrim.addEventListener('click', () => host.onClose());
  const sheet = el('div', isIpadPopover ? 'ms export popover' : 'ms export');
  sheet.dataset.state = 'export-sheet';
  if (isIpadPopover) sheet.dataset.popover = '1';

  const head = el('div', 'row');
  const cancel = button('nb', isIpadPopover ? '好' : '取消');
  if (isIpadPopover) cancel.dataset.act = 'close';
  cancel.addEventListener('click', () => host.onClose());
  head.append(el('h2', '', opts.title ?? `导出 ${n} 张`), cancel);

  const sizeLine = el('span', 'num size', n === 1 ? SIZE_PENDING : MANY_SIZES);
  const sizeRow = el('div', 'frow');
  const sizeLab = el('div', 'lab');
  sizeLab.append(el('b', '', '尺寸'), sizeLine);
  let sized: { sizes: Record<string, [number, number]>; ratio: [number, number] | null } | null = null;
  const paintSize = () => {
    if (n !== 1) return;
    if (photos[0].diptych) {
      const dip = sized ? (sized.sizes[size] ?? sized.sizes.original) : null;
      if (dip || photos[0].frame) sizeLine.textContent = pairExportSizeText(photos[0].frame, dip, size, halfframe);
      return;
    }
    if (sized) sizeLine.textContent = sizeText(sized.sizes[size] ?? sized.sizes.original, sized.ratio);
  };
  paintSize();
  sizeRow.append(
    sizeLab,
    seg(SIZES, size, (v) => {
      size = v;
      saveExportSize(v);
      paintSize();
    }),
  );

  const qualityRow = el('div', 'frow');
  const qualityLab = el('div', 'lab');
  qualityLab.append(el('b', '', '品质'));
  qualityRow.append(
    qualityLab,
    seg(QUALITIES, quality, (v) => {
      quality = v;
      saveExportQuality(v);
    }),
  );

  let hfRow: HTMLElement | null = null;
  if (hasDiptych) {
    hfRow = el('div', 'frow');
    hfRow.dataset.role = 'halfframe-row';
    const hfLab = el('div', 'lab');
    hfLab.append(el('b', '', '半格'));
    hfRow.append(
      hfLab,
      seg(HALFFRAME_MODES, halfframe, (v) => {
        halfframe = v;
        saveExportHalfframe(v);
        paintSize();
      }),
      el('p', 'hf-note', PAIR_EXPORT_NOTE),
    );
  }

  const canHdr = hdrAvailable(photos);
  let hdrOn = canHdr && loadExportHdr();
  const hdrRow = el('div', 'frow');
  const hdr = el('div', 'hdrrow');
  const hdrText = el('div', 'tx');
  hdrText.append(el('b', '', 'HDR'), el('small', '', canHdr ? hdrLine() : HDR_NO_RAW));
  const sw = document.createElement('input');
  sw.type = 'checkbox';
  sw.className = 'switch';
  sw.disabled = !canHdr;
  sw.checked = hdrOn;
  sw.setAttribute('role', 'switch');
  sw.setAttribute('aria-label', 'HDR');
  hdr.append(hdrText, sw);
  hdrRow.append(hdr);

  const mix = hdrMixLine(photos);
  const mixBox = mix ? el('div', 'bn grey hdrmix') : null;
  if (mixBox && mix) {
    mixBox.dataset.state = 'hdr-mix';
    const ic = el('span', 'dim');
    ic.append(exportIco(EXPORT_ICON.info, 'md'));
    mixBox.append(ic, el('div', 'tx dim', mix));
    mixBox.hidden = !hdrOn;
    hdrRow.append(mixBox);
  }
  const frameWarn = hdrFrameLine(photos);
  const frameBox = frameWarn ? el('div', 'bn grey hdrmix') : null;
  if (frameBox && frameWarn) {
    frameBox.dataset.state = 'hdr-frame';
    const ic = el('span', 'dim');
    ic.append(exportIco(EXPORT_ICON.alert, 'md'));
    frameBox.append(ic, el('div', 'tx dim', frameWarn));
    frameBox.hidden = !hdrOn;
    hdrRow.append(frameBox);
  }
  const syncHdrWarnings = () => {
    if (mixBox) mixBox.hidden = !hdrOn;
    if (frameBox) frameBox.hidden = !hdrOn;
  };
  sw.addEventListener('change', () => {
    if (!canHdr) return;
    hdrOn = sw.checked;
    saveExportHdr(hdrOn);
    syncHdrWarnings();
  });
  const offHdr = watchExportHdr(() => {
    if (!active || !canHdr) return;
    hdrOn = loadExportHdr();
    sw.checked = hdrOn;
    syncHdrWarnings();
  });

  const form = el('div');
  form.style.marginTop = '10px';
  form.append(sizeRow, qualityRow);
  if (hfRow) form.append(hfRow);
  form.append(hdrRow);
  if (!host.book().dat) {
    const guide = el('p', 'guide', NO_DAT_GUIDE);
    guide.dataset.state = 'no-dat-guide';
    form.append(guide);
  }

  const go = button('btn primary lg block go', '');
  go.append(exportIco(EXPORT_ICON.export, 'md'), document.createTextNode('存到相册'));
  go.addEventListener('click', () => {
    host.onClose();
    opts.onStart?.();
    host.onStart(photos, { size, quality, hdr: hdrOn, halfframe: hasDiptych ? halfframe : 'diptych' });
  });
  const foot = el('p', 'foot', '「存到相册」每次都按这里的设置做。做好后点「存进相册」，在系统面板里点「存储图像」。');
  if (isIpadPopover) {
    sheet.append(el('i', 'arrow up'), head, el('p', 'lead', '「存到相册」每次都按这里的设置做。'), form, go);
  } else {
    sheet.append(el('div', 'grab'), head, el('p', 'lead', timeRange(photos)), form, go, foot);
  }
  host.shell.append(scrim, sheet);

  if (n === 1) {
    const photo = photos[0];
    void (async () => {
      const fontKeys = fontsNeeded(photo.border, photo.stamp);
      const [, fonts] = await Promise.all([
        logoStore.ensureLoaded(),
        fontKeys.length ? ensureFontPack(fontKeys) : Promise.resolve({ fontIndex: '', fontPack: null }),
        ensureCameraRecipe(photo, host.book(), { checkMissingDat: true }),
      ]);
      if (!active || !sheet.isConnected) return;
      const lutBook = host.lutBook?.();
      const book = host.book();
      const reqPhoto = host.requestPhoto(photo, lutBook, book);
      const req = buildRequest(reqPhoto, host.cardsFor(book, photo), { size: 'original', quality: 'high' }, __GXR_BUILD__);
      const isLogLut = Boolean(reqPhoto.lutInputSpace && !isDisplaySpace(reqPhoto.lutInputSpace));
      const isLocalRecipe = (req.side.recipe as { kind?: unknown } | undefined)?.kind === 'local';
      const useJpg = photo.jpg !== null && !isLogLut && (!req.side.recipe || photo.dng === null || (!isLocalRecipe && !book.pack));
      const reply = await host.core.sizes({
        dng: photo.dng,
        jpg: photo.jpg,
        side: req.side,
        layout: req.layout,
        useJpg,
        stem: photo.stem,
        mtimeMs: req.mtimeMs,
        tzOffsetMin: req.tzOffsetMin,
        fontIndex: fonts.fontIndex,
        fontPack: fonts.fontPack,
      });
      if (host.isBatchClosed()) host.core.release();
      if (!active || !sheet.isConnected) return;
      if (!reply.ok || !reply.json) {
        sizeLine.textContent = SIZE_FAILED;
        return;
      }
      sized = JSON.parse(reply.json) as typeof sized;
      paintSize();
    })().catch(() => {
      if (active && sheet.isConnected) sizeLine.textContent = SIZE_FAILED;
    });
  } else {
    const warmFonts = [...new Set(photos.flatMap((p) => fontsNeeded(p.border, p.stamp)))];
    void logoStore.ensureLoaded();
    void ensureCameraRecipes(photos, host.book(), { checkMissingDat: true });
    if (warmFonts.length) void ensureFontPack(warmFonts).catch(() => undefined);
  }

  return {
    nodes: [scrim, sheet],
    cleanup: () => {
      active = false;
      offHdr();
    },
  };
}
