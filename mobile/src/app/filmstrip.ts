// iPad 底部单行胶片条（方案 A）：沿用网格分组规则（单张自成一组不显示组标题，紧跟上组时留细分隔），点选切照片。

import { groupByGap, type TimeGroup } from '../lib/group';
import { button, el } from './dom';
import { photoMs, type AppApi, type Photo } from './model';

export interface FilmstripGroupEntry {
  kind: 'header' | 'cell';
  group: TimeGroup;
  photoId?: string;
  soloSep?: boolean;
}

/** 把照片列表按网格相同规则展开为胶片条项：多张组前放组头，单张组不放组头，紧跟上组时标 soloSep。 */
export function buildFilmstripEntries(groups: readonly TimeGroup[]): FilmstripGroupEntry[] {
  const out: FilmstripGroupEntry[] = [];
  let hadPrevCell = false;
  for (const group of groups) {
    if (group.ids.length > 1) {
      out.push({ kind: 'header', group });
      for (const id of group.ids) {
        out.push({ kind: 'cell', group, photoId: id });
        hadPrevCell = true;
      }
    } else if (group.ids.length === 1) {
      out.push({ kind: 'cell', group, photoId: group.ids[0], soloSep: hadPrevCell });
      hadPrevCell = true;
    }
  }
  return out;
}

export class FilmstripView {
  private readonly api: AppApi;
  private readonly root: HTMLElement;
  private currentId: string;
  private cells = new Map<string, { btn: HTMLButtonElement; canvas: HTMLCanvasElement | null; drawn: ImageBitmap | null }>();
  private scrollRaf = 0;

  constructor(api: AppApi, currentId: string) {
    this.api = api;
    this.currentId = currentId;
    this.root = el('div', 'film ipad-filmstrip');
    this.root.dataset.filmstrip = '1';
    this.root.setAttribute('role', 'region');
    this.root.setAttribute('aria-label', '照片胶片条');
    this.render();
  }

  el(): HTMLElement {
    return this.root;
  }

  setCurrent(photoId: string): void {
    this.currentId = photoId;
    for (const [id, entry] of this.cells) {
      const active = id === photoId;
      entry.btn.classList.toggle('cur', active);
      if (active) entry.btn.setAttribute('aria-current', 'true');
      else entry.btn.removeAttribute('aria-current');
    }
    this.scheduleCenter();
  }

  refresh(): void {
    this.render();
  }

  destroy(): void {
    if (this.scrollRaf) {
      cancelAnimationFrame(this.scrollRaf);
      this.scrollRaf = 0;
    }
    for (const entry of this.cells.values()) {
      if (entry.canvas) entry.canvas.width = entry.canvas.height = 0;
    }
    this.cells.clear();
    this.root.replaceChildren();
  }

  private render(): void {
    const photos = this.api.photos();
    const groups = groupByGap(
      photos.map((photo) => ({ id: photo.id, ms: photoMs(photo) })),
      this.api.gap(),
      this.api.order(),
    );
    const entries = buildFilmstripEntries(groups);
    const indexById = new Map<string, number>();
    photos.forEach((p, idx) => indexById.set(p.id, idx + 1));

    for (const entry of this.cells.values()) {
      if (entry.canvas) entry.canvas.width = entry.canvas.height = 0;
    }
    this.cells.clear();
    this.root.replaceChildren();

    let firstHeader = true;
    for (const item of entries) {
      if (item.kind === 'header') {
        const hd = el('div', firstHeader ? 'fgh first' : 'fgh');
        firstHeader = false;
        hd.dataset.groupCount = String(item.group.ids.length);
        hd.append(
          el('span', 'faint lbl1 num', item.group.rangeLabel),
          el('span', 'small dim num', `${item.group.ids.length} 张`),
        );
        this.root.append(hd);
        continue;
      }
      firstHeader = false;
      const id = item.photoId;
      if (!id) continue;
      const photo = this.api.photo(id);
      if (!photo) continue;
      const active = id === this.currentId;
      const cls = `cell${active ? ' cur' : ''}${item.soloSep ? ' solo-sep' : ''}`;
      const btn = button(cls, '');
      btn.dataset.photoId = id;
      const pos = indexById.get(id);
      btn.setAttribute('aria-label', pos ? `第 ${pos} 张` : '照片');
      if (active) btn.setAttribute('aria-current', 'true');
      const canvas = this.drawThumb(btn, photo);
      btn.addEventListener('click', () => {
        if (id !== this.currentId) this.api.open(id);
      });
      this.cells.set(id, { btn, canvas, drawn: photo.bitmap });
      this.root.append(btn);
    }
    this.scheduleCenter();
  }

  private drawThumb(btn: HTMLElement, photo: Photo): HTMLCanvasElement | null {
    if (!photo.bitmap) {
      btn.append(el('span', 'raw', photo.dng ? 'RAW' : 'JPG'));
      return null;
    }
    const cv = document.createElement('canvas');
    cv.width = photo.bitmap.width;
    cv.height = photo.bitmap.height;
    cv.getContext('2d')?.drawImage(photo.bitmap, 0, 0);
    btn.append(cv);
    return cv;
  }

  private scheduleCenter(): void {
    if (this.scrollRaf) cancelAnimationFrame(this.scrollRaf);
    this.scrollRaf = requestAnimationFrame(() => {
      this.scrollRaf = 0;
      const active = this.cells.get(this.currentId)?.btn;
      if (!active || !this.root.isConnected || this.root.clientWidth <= 0) return;
      const sr = this.root.getBoundingClientRect();
      const cr = active.getBoundingClientRect();
      const target = this.root.scrollLeft + (cr.left - sr.left) + cr.width / 2 - sr.width / 2;
      const max = Math.max(0, this.root.scrollWidth - this.root.clientWidth);
      this.root.scrollLeft = Math.max(0, Math.min(max, target));
    });
  }
}
