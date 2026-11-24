// 相机配方文件不匹配时的单次提示表与状态判定；从 recipe_view.ts / recipe_band.ts 拆出以控制单文件行数。

import { camFilesMismatchMessage } from '../lib/camfiles';
import { datKeyOf, type CameraRecipeError, type Choice } from '../lib/recipes';
import type { Book } from './book';
import { button, el, icon } from './dom';
import type { AskSheetHandle } from './recipe_band';

const FILE = 'M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7zm0 0v5h5';

let promptedDatKey: string | null = null;

/** 当前照片未显式选配方且拍摄标签与记住的相机配方文件不匹配。 */
export function isCatalogMismatch(camError: CameraRecipeError | null, choice: Choice | null): boolean {
  return Boolean(!choice && camError?.code === 'camera_recipe_catalog_missing');
}

/** 同一份不匹配的相机配方文件在一次会话里只主动提示一次，避免连续切图反复弹窗；换文件或忘掉后重置。 */
export function shouldPromptMismatch(dat: object | null | undefined): boolean {
  const key = datKeyOf(dat);
  if (!key) {
    promptedDatKey = null;
    return false;
  }
  if (promptedDatKey === key) return false;
  promptedDatKey = key;
  return true;
}

export function resetMismatchPrompt(): void {
  promptedDatKey = null;
}

function mismatchLeadNodes(book: Book, hasJpg: boolean): HTMLElement[] {
  if (book.datError) return [el('p', 'lead', book.datError)];
  return [el('p', 'lead', camFilesMismatchMessage(book.dat?.generation ?? null, hasJpg))];
}

export function openMismatchDatSheet(
  shell: HTMLElement,
  book: Book,
  hasJpg: boolean,
  pickDat: () => void,
): AskSheetHandle {
  shell.querySelector('.ask-scrim')?.remove();
  shell.querySelector('.ask-sheet')?.remove();

  const initialDat = book.dat;
  const scrim = el('div', 'scrim ask-scrim');
  const sheet = el('div', 'ms ask-sheet');
  sheet.dataset.state = 'camfiles-mismatch';
  const head = el('div', 'hd');
  head.append(el('h2', '', '相机配方文件不匹配'));
  const path = el('div', 'path');
  const fic = el('span', 'fic');
  fic.append(icon(FILE));
  path.append(
    fic,
    el('span', '', '存储卡'),
    el('span', 'faint', '›'),
    el('b', 'num', 'GXRCE.DAT'),
    el('span', 'faint small', '（在最外层，和 DCIM 并排）'),
    el('b', 'num', 'GXRFX.BIN（新卡才有）'),
  );
  const close = () => {
    scrim.remove();
    sheet.remove();
  };
  const pick = button('btn primary lg block', '选择这张卡的 GXRCE.DAT 和 GXRFX.BIN');
  pick.dataset.act = 'mismatch-pick';
  pick.prepend(icon(FILE));
  pick.addEventListener('click', () => {
    close();
    pickDat();
  });
  const later = button('btn lg ghost block', '先不用');
  later.dataset.act = 'mismatch-later';
  later.addEventListener('click', close);
  scrim.addEventListener('click', close);
  const leads = el('div', 'ask-leads');
  leads.append(...mismatchLeadNodes(book, hasJpg));
  sheet.append(el('div', 'grab'), head, leads, path, pick, later);
  shell.append(scrim, sheet);

  return {
    close,
    sync(nextBook: Book) {
      if (!leads.isConnected) return;
      if (nextBook.dat !== initialDat && !nextBook.datError) {
        close();
        return;
      }
      leads.replaceChildren(...mismatchLeadNodes(nextBook, hasJpg));
    },
  };
}
