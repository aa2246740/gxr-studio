// 导出请求准备与缩略图条：拆出 ExportFlow 的纯装配逻辑，避免单文件过长。

import { unitOfLens } from '../lib/calib';
import { mergeMeta } from '../lib/exif';
import { buildRequest, usedLogoId, type RequestPhoto } from '../lib/export_request';
import { adoptedSelectionOf, cameraRecipeErrorOf, photoWithAdopted, recipeUnitOf, shelf, shelves, type Card } from '../lib/recipes';
import type { Book } from './book';
import { el } from './dom';
import type { BatchItem, ExportBatch } from './export_batch';
import type { ExportOptions } from './export_queue';
import { EXPORT_ICON, exportIco } from './export_ready';
import { logoStore } from './logo_store';
import type { LutBook } from './lut_book';
import type { Photo } from './model';

export function cardsFor(book: Book, photo: Photo): Card[] {
  const dat = book.dat;
  if (!dat) return [];
  const adopted = adoptedSelectionOf(photo, dat);
  const unit = recipeUnitOf(photo.choice, adopted, unitOfLens(mergeMeta(photo.jpgMeta, photo.dngMeta).lens));
  return shelf(shelves(dat.listing, unit), 'all');
}

export function requestPhoto(photo: Photo, lutBook?: LutBook, book?: Book): RequestPhoto {
  const view = book ? photoWithAdopted(photo, book.dat) : photo;
  const meta = mergeMeta(photo.jpgMeta, photo.dngMeta);
  const source = photo.jpg ?? photo.dng;
  const lutRecord = photo.lut ? lutBook?.get(photo.lut) : undefined;
  const border = photo.border ? { ...photo.border } : null;
  const stamp = photo.stamp ? { ...photo.stamp } : null;
  const logoId = usedLogoId(border);
  return {
    stem: photo.stem,
    dngName: photo.dng?.name ?? null,
    jpgName: photo.jpg?.name ?? null,
    lens: meta.lens,
    iso: meta.iso,
    choice: view.choice ? { ...view.choice } : null,
    levels: new Map([...view.levels].map(([k, v]) => [k, { ...v }])),
    memory: new Map([...photo.memory].map(([k, v]) => [k, { ...v }])),
    lut: photo.lut,
    lutInputSpace: lutRecord?.input_space ?? null,
    knobs: { ...photo.knobs },
    curve: photo.curve ?? null,
    frame: photo.frame ? { ...photo.frame } : null,
    geometry: photo.geometry ? { ...photo.geometry } : undefined,
    crop: photo.crop
      ? {
          preset: photo.crop.preset,
          rect: [photo.crop.rect[0], photo.crop.rect[1], photo.crop.rect[2], photo.crop.rect[3]],
          angle_deg: photo.crop.angle_deg,
        }
      : null,
    border,
    stamp,
    logoInfo: logoId ? logoStore.info(logoId) : null,
    sourceModified: source?.lastModified ?? photo.lastModified,
  };
}

export interface ExportSnapshot {
  reqPhoto: RequestPhoto;
  cards: Card[];
  explicitChoice: boolean;
  recipeOff: boolean;
}

export function applyAdoptedAndLogos(
  photos: Photo[],
  snapshots: ExportSnapshot[],
  items: BatchItem[],
  dat: Book['dat'],
  needAdopt: boolean,
  hasLogos: boolean,
  options: ExportOptions,
  build: string,
  binding?: string,
): void {
  photos.forEach((photo, idx) => {
    const snap = snapshots[idx];
    let changed = false;
    const logoId = hasLogos ? usedLogoId(snap.reqPhoto.border) : null;
    if (logoId) {
      snap.reqPhoto.logoInfo = logoStore.info(logoId);
      changed = true;
    }
    if (needAdopt && !snap.explicitChoice && !snap.recipeOff) {
      const adopted = adoptedSelectionOf(photo, dat);
      if (adopted) {
        snap.reqPhoto.choice = { ...adopted.choice };
        snap.reqPhoto.levels = new Map([[adopted.choice.id, { ...adopted.levels }]]);
        if (dat) snap.cards = shelf(shelves(dat.listing, recipeUnitOf(null, adopted, unitOfLens(snap.reqPhoto.lens))), 'all');
        changed = true;
      }
      const note = cameraRecipeErrorOf(photo, dat)?.message;
      if (note) items[idx].cameraRecipeNote = note;
    }
    if (changed) items[idx].req = buildRequest(snap.reqPhoto, snap.cards, options, build, binding);
  });
}

export function buildExportThumbs(batch: ExportBatch, photoById: (id: string) => Photo | undefined): HTMLElement {
  const row = el('div', 'pthumbs');
  const { done } = batch.progress();
  const start = Math.max(0, Math.min(done - 2, batch.items.length - 6));
  for (const item of batch.items.slice(start, start + 6)) {
    const state = batch.states.get(item.id) ?? 'wait';
    const t = el('div', `t ${state}`);
    t.dataset.id = item.id;
    const bitmap = photoById(item.id)?.bitmap;
    if (bitmap) {
      const canvas = document.createElement('canvas');
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      canvas.getContext('2d')?.drawImage(bitmap, 0, 0);
      t.append(canvas);
    }
    if (state === 'run') t.append(el('span', 'spin'));
    else if (state === 'ok' || state === 'saved' || state === 'fail') {
      if (state === 'saved') t.classList.add('ok');
      const st = el('span', 'st');
      st.append(exportIco(state === 'fail' ? EXPORT_ICON.x : EXPORT_ICON.check));
      t.append(st);
    }
    row.append(t);
  }
  return row;
}
