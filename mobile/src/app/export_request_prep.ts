// 导出请求准备与缩略图条：拆出 ExportFlow 的纯装配逻辑，避免单文件过长。

import { unitOfLens } from '../lib/calib';
import { mergeMeta } from '../lib/exif';
import { buildRequest, usedLogoId, type RequestPhoto } from '../lib/export_request';
import { normalizedHalfframeMode, type HalfframeRole } from '../lib/halfframe';
import { isDisplaySpace } from '../lib/lut';
import { adoptedSelectionOf, cameraRecipeErrorOf, photoWithAdopted, recipeUnitOf, shelf, shelves, type Card } from '../lib/recipes';
import type { Book } from './book';
import { el } from './dom';
import { batchPhotoId, type BatchItem, type ExportBatch } from './export_batch';
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
  id: string;
  stem: string;
  version: number;
  diptych: boolean;
  dng: File | null;
  jpg: File | null;
  reqPhoto: RequestPhoto;
  cards: Card[];
  explicitChoice: boolean;
  recipeOff: boolean;
  note?: string;
}

/**
 * 照桌面 halfframe_export.py:300-316 构造单格请求：保留调色（adjust / curve / display-space lut），
 * 关闭裁剪、拉直、边框、时间戳、配方与 HDR，文件名按 `_1` / `_2` 区分并在 Worker 里追加 `_half` 。
 */
export function singleHalfRequestPhoto(reqPhoto: RequestPhoto, role: HalfframeRole): RequestPhoto {
  const suffix = role === 'first' ? '_1' : '_2';
  const keepLut = Boolean(reqPhoto.lut && isDisplaySpace(reqPhoto.lutInputSpace));
  return {
    ...reqPhoto,
    stem: `${reqPhoto.stem}${suffix}`,
    dngName: null,
    choice: null,
    lut: keepLut ? reqPhoto.lut : null,
    lutInputSpace: keepLut ? reqPhoto.lutInputSpace : null,
    levels: new Map(),
    memory: new Map(),
    frame: null,
    geometry: undefined,
    crop: null,
    border: null,
    stamp: null,
    logoInfo: null,
  };
}

/** 将一批快照按半格导出模式展开为 1（双联一张/普通）、2（两张单格）或 3（都要）条 BatchItem。 */
export function buildBatchItems(
  snapshots: readonly ExportSnapshot[],
  options: ExportOptions,
  build: string,
  binding?: string,
): BatchItem[] {
  const hfMode = normalizedHalfframeMode(options.halfframe);
  const items: BatchItem[] = [];
  for (const s of snapshots) {
    if (!s.diptych || hfMode === 'diptych') {
      items.push({
        id: s.id,
        stem: s.stem,
        version: s.version,
        dng: s.dng,
        jpg: s.jpg,
        req: buildRequest(s.reqPhoto, s.cards, options, build, binding),
        ...(s.note ? { cameraRecipeNote: s.note } : {}),
      });
      continue;
    }
    if (hfMode === 'both') {
      items.push({
        id: s.id,
        photoId: s.id,
        stem: s.stem,
        version: s.version,
        dng: s.dng,
        jpg: s.jpg,
        req: buildRequest(s.reqPhoto, s.cards, options, build, binding),
        ...(s.note ? { cameraRecipeNote: s.note } : {}),
      });
    }
    const singleOpts = { size: options.size, quality: options.quality, hdr: false };
    for (const [role, idx] of [
      ['first', '1'],
      ['second', '2'],
    ] as const) {
      const halfPhoto = singleHalfRequestPhoto(s.reqPhoto, role);
      items.push({
        id: `${s.id}#half:${idx}`,
        photoId: s.id,
        halfframeRole: role,
        stem: halfPhoto.stem,
        version: s.version,
        dng: null,
        jpg: s.jpg,
        req: buildRequest(halfPhoto, [], singleOpts, build, binding),
      });
    }
  }
  return items;
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
  const byPhoto = new Map<string, BatchItem[]>();
  for (const item of items) {
    const pid = batchPhotoId(item);
    const list = byPhoto.get(pid);
    if (list) list.push(item);
    else byPhoto.set(pid, [item]);
  }
  photos.forEach((photo, idx) => {
    const snap = snapshots[idx];
    let changed = false;
    const logoId = hasLogos ? usedLogoId(snap.reqPhoto.border) : null;
    if (logoId) {
      snap.reqPhoto.logoInfo = logoStore.info(logoId);
      changed = true;
    }
    const targetItems = byPhoto.get(photo.id) ?? [];
    if (needAdopt && !snap.explicitChoice && !snap.recipeOff) {
      const adopted = adoptedSelectionOf(photo, dat);
      if (adopted) {
        snap.reqPhoto.choice = { ...adopted.choice };
        snap.reqPhoto.levels = new Map([[adopted.choice.id, { ...adopted.levels }]]);
        if (dat) snap.cards = shelf(shelves(dat.listing, recipeUnitOf(null, adopted, unitOfLens(snap.reqPhoto.lens))), 'all');
        changed = true;
      }
      const note = cameraRecipeErrorOf(photo, dat)?.message;
      if (note) {
        for (const item of targetItems) {
          if (!item.halfframeRole) item.cameraRecipeNote = note;
        }
      }
    }
    if (changed) {
      for (const item of targetItems) {
        if (!item.halfframeRole) item.req = buildRequest(snap.reqPhoto, snap.cards, options, build, binding);
      }
    }
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
    const bitmap = photoById(batchPhotoId(item))?.bitmap;
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
