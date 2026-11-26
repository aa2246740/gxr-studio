// 网格里的一格：角标、读不了/卡/只有 RAW、编辑过的铅笔、读屏名称和缩略图。从 grid.ts 原样搬出来（单文件行数）。

import { groupsPresent } from '../lib/paste';
import { el, icon } from './dom';
import { cellAriaLabel } from './layout';
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
    if (photo.jpg && !photo.dng) bl.append(el('span', 'bdg', 'JPG'));
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
