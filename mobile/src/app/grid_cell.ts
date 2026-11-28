// 网格里的一格：角标、读不了/卡/只有 RAW、编辑过的铅笔、读屏名称和缩略图。从 grid.ts 原样搬出来（单文件行数）。

import { groupsPresent } from '../lib/paste';
import { isComputer, saveWord } from '../lib/save_mode';
import { el, icon } from './dom';
import { DIPTYCH_BADGE, DIPTYCH_NOTE, cellAriaLabel } from './layout';
import { unread, type Photo } from './model';
import { PASTE_ICON } from './paste_ui';

const bitmapIds = new WeakMap<ImageBitmap, number>();
let nextBitmapId = 1;

function gridBitmapId(bitmap: ImageBitmap): number {
  let id = bitmapIds.get(bitmap);
  if (!id) {
    id = nextBitmapId++;
    bitmapIds.set(bitmap, id);
  }
  return id;
}

/**
 * 电脑上网格只占一个 Tab 位（roving tabindex）：`stop`（上次有焦点的那张）这一格 tabIndex 0，它不在画出来的这几行里就是
 * 画出来的第一格；别的格子 -1，下一个 Tab 就出网格。方向键、Enter、空格照旧。手机和 iPad 不变（每格都能 Tab 到）。
 */
export function rovingTabs(cells: ReadonlyMap<string, HTMLButtonElement>, stop: string | null): void {
  if (!isComputer()) return;
  const at = stop !== null && cells.has(stop) ? stop : cells.keys().next().value;
  for (const [id, cell] of cells) cell.tabIndex = id === at ? 0 : -1;
}

export function fillCell(cell: HTMLButtonElement, photo: Photo, selected: boolean, index: number | undefined): void {
  cell.classList.toggle('skel', photo.status === 'pending');
  cell.classList.toggle('err', photo.status === 'bad');
  cell.classList.toggle('gone', unread(photo));
  cell.classList.toggle('on', selected);
  const raw = cell.querySelector('.raw');
  const bl = cell.querySelector('.bl');
  if (raw) {
    raw.textContent = photo.status === 'bad' ? '读不了' : unread(photo) ? '卡' : !photo.bitmap && photo.status === 'ready' && !photo.jpg ? '只有 RAW' : '';
  }
  if (bl) {
    bl.replaceChildren();
    if (photo.diptych) {
      const badge = el('span', 'bdg', DIPTYCH_BADGE);
      badge.title = DIPTYCH_NOTE;
      badge.setAttribute('aria-label', DIPTYCH_NOTE);
      bl.append(badge);
    } else if (photo.jpg && !photo.dng) bl.append(el('span', 'bdg', 'JPG'));
  }
  // 04e：带着编辑（配方/LUT、调整/曲线、拉直、裁剪、边框、时间戳任一组）的照片右下角有铅笔，按现在的编辑状态挂上或拿掉。
  const edited = groupsPresent(photo).length > 0;
  const mark = cell.querySelector('.edt');
  if (edited && !mark) {
    const pencil = el('span', 'edt');
    pencil.setAttribute('aria-hidden', 'true');
    pencil.append(icon(PASTE_ICON.pencil));
    cell.append(pencil);
  } else if (!edited) mark?.remove();
  cell.setAttribute(
    'aria-label',
    cellAriaLabel(
      index,
      {
        dng: photo.dng !== null,
        jpg: photo.jpg !== null,
        diptych: photo.diptych,
        bad: photo.status === 'bad',
        unread: unread(photo),
      },
      edited,
      selected,
    ),
  );
  const bmp = photo.bitmap;
  if (!bmp || bmp.width <= 0 || bmp.height <= 0) return;
  let canvas = cell.querySelector('canvas');
  if (!canvas) {
    canvas = document.createElement('canvas');
    cell.prepend(canvas);
  }
  const drawnKey = `${photo.id}:${gridBitmapId(bmp)}`;
  if (canvas.dataset.drawn === drawnKey) return;
  canvas.width = bmp.width;
  canvas.height = bmp.height;
  canvas.getContext('2d')?.drawImage(bmp, 0, 0);
  canvas.dataset.drawn = drawnKey;
}

/** 拔卡提示的两行数字（从 grid.ts 原样搬出来，单文件行数）。 */
export function cardText(sheet: HTMLElement, ready: number, missing: number): void {
  const first = sheet.querySelector('p.ready');
  const second = sheet.querySelector('p.missing');
  if (first) first.textContent = `已经读进来的 ${ready} 张照片不受影响，可以接着换配方、${saveWord()}。`;
  if (second) second.textContent = `还有 ${missing} 张没读完。把卡插回去，再点「重新选照片」，选同样的照片；做过的设置会保留。`;
}
