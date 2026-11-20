// 配方与 LUT 共用的 128pt 单带辅件：带首「配方 | LUT」切换键、配方筛选工作表、状态说明与 06d 缺文件弹层。

import { crossed, uncalibratedReason, type Unit } from '../lib/calib';
import {
  CAMERA_RECIPE_LEVELS_UNKNOWN,
  choices,
  effective,
  formatCameraRecipeEditorError,
  KNOB_NAMES,
  levelText,
  nearestIndex,
} from '../lib/recipes';
import type { AdoptedSelection, CameraRecipeError, Control, Shelf, Shelves } from '../lib/recipes';
import type { Book, UnitState } from './book';
import { button, el, icon } from './dom';
import { registerSheet } from './sheet_lifecycle';

const CHECK = 'M5 12.5l4.5 4.5L19 7.5';
const WARN = 'M12 4l9 16H3zM12 10v4M12 17h.01';
const PLUS = 'M12 5v14M5 12h14';
const FILE = 'M7 3h7l4 4v14H7zM14 3v4h4';

export interface RecipeStatusScene {
  photoUnit: Unit | null;
  unit: Unit;
  iso: number | null;
  state: UnitState;
  adopted: AdoptedSelection | null;
  camError: CameraRecipeError | null;
}

export interface AskSheetHandle {
  close(): void;
  sync(book: Book): void;
}

export function makeRecipeLutSwitch(
  active: 'recipe' | 'lut',
  lutActive: boolean,
  onSwitch: (mode: 'recipe' | 'lut') => void,
): HTMLElement {
  const wrap = el('div', 'mode-switch');
  wrap.setAttribute('role', 'tablist');
  wrap.setAttribute('aria-label', '配方与 LUT');

  const rBtn = button(active === 'recipe' ? 'mode-btn on' : 'mode-btn', '配方');
  rBtn.dataset.mode = 'recipe';
  rBtn.setAttribute('role', 'tab');
  rBtn.setAttribute('aria-selected', active === 'recipe' ? 'true' : 'false');
  rBtn.addEventListener('click', () => {
    if (active !== 'recipe') onSwitch('recipe');
  });

  const lBtn = button(active === 'lut' ? 'mode-btn on' : 'mode-btn', 'LUT');
  lBtn.dataset.mode = 'lut';
  lBtn.setAttribute('role', 'tab');
  lBtn.setAttribute('aria-selected', active === 'lut' ? 'true' : 'false');
  if (lutActive) lBtn.classList.add('dot');
  lBtn.addEventListener('click', () => {
    if (active !== 'lut') onSwitch('lut');
  });

  wrap.append(rBtn, lBtn);
  return wrap;
}

export function shelfShortLabel(which: Shelf): string {
  if (which === 'builtin') return '内置';
  if (which === 'looks') return 'Looks';
  if (which === 'mine') return '我的';
  return '全部';
}

export function openRecipeFilterSheet(
  shell: HTMLElement,
  list: Shelves,
  current: Shelf,
  onPick: (which: Shelf) => void,
): () => void {
  const scrim = el('div', 'scrim');
  const sheet = el('div', 'ms filter-sheet');
  sheet.setAttribute('role', 'dialog');
  sheet.setAttribute('aria-label', '配方筛选');

  let unregister: () => void = () => undefined;
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    unregister();
    scrim.remove();
    sheet.remove();
  };
  unregister = registerSheet(shell, close);
  scrim.addEventListener('click', close);

  const hd = el('div', 'sheet-hd');
  hd.append(el('h2', '', '配方筛选'));
  const done = button('nb', '关闭');
  done.dataset.act = 'close';
  done.addEventListener('click', close);
  hd.append(done);

  const all = list.builtin.length + list.looks.length + list.users.length;
  const items: Array<[Shelf, string, number]> = [
    ['all', '全部', all],
    ['builtin', '内置', list.builtin.length],
  ];
  if (list.looks.length) items.push(['looks', 'Looks', list.looks.length]);
  items.push(['mine', '我的配方', list.users.length]);

  const body = el('div', 'filter-list');
  for (const [which, title, count] of items) {
    const row = button(which === current ? 'lrow filter-item on' : 'lrow filter-item', '');
    row.dataset.filter = which;
    row.dataset.shelf = which;
    const tx = el('div', 'grow');
    tx.append(el('b', '', `${title} ${count}`));
    row.append(tx);
    if (which === current) {
      const ck = el('span', 'gchk');
      ck.append(icon(CHECK));
      row.append(ck);
    }
    row.addEventListener('click', () => {
      close();
      onPick(which);
    });
    body.append(row);
  }

  sheet.append(hd, body);
  shell.append(scrim, sheet);
  return close;
}

export function openRecipeInfoSheet(
  shell: HTMLElement,
  title: string,
  nodes: HTMLElement[],
): () => void {
  const scrim = el('div', 'scrim');
  const sheet = el('div', 'ms recipe-info-sheet');
  sheet.setAttribute('role', 'dialog');
  sheet.setAttribute('aria-label', title);

  let unregister: () => void = () => undefined;
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    unregister();
    scrim.remove();
    sheet.remove();
  };
  unregister = registerSheet(shell, close);
  scrim.addEventListener('click', close);

  const hd = el('div', 'sheet-hd');
  hd.append(el('h2', '', title));
  const done = button('nb', '关闭');
  done.dataset.act = 'close';
  done.addEventListener('click', close);
  hd.append(done);

  const body = el('div', 'recipe-info-body');
  body.append(...nodes);
  sheet.append(hd, body);
  shell.append(scrim, sheet);
  return close;
}

export function openJpgInfoSheet(shell: HTMLElement, diptych: boolean): void {
  const note = el('div', 'bn grey');
  const text = el('div', 'tx');
  if (diptych) {
    text.append(el('b', '', '这张是半格双联，配方效果要等配对功能。'));
  } else {
    text.append(el('b', '', '配方需要 RAW 文件'), el('div', 'dim', '拍摄时将画质设为 RAW+JPEG'));
  }
  note.append(text);
  openRecipeInfoSheet(shell, '配方需要 RAW 文件', [
    note,
    el('p', 'dim', '这张只有相机 JPG。下面这些照常能用，导出的是相机 JPG 加上这些改动：调整、裁剪、边框、曲线。'),
    el('p', 'faint', 'HDR 需要 RAW 文件。这张会存普通版本。'),
  ]);
}

export function datFileBox(message: string, pickDat: () => void): HTMLElement {
  const box = el('div');
  box.append(el('p', 'dim', message));
  const pick = button('btn primary block', '在「文件」里选 GXRCE.DAT 和 GXRFX.BIN');
  pick.prepend(icon(FILE));
  pick.addEventListener('click', pickDat);
  box.append(pick);
  return box;
}

export function buildStatusBlock(scene: RecipeStatusScene, pickCalib: () => void): HTMLElement {
  const state = scene.state;
  const off = state.state === 'ready' && crossed(scene.photoUnit, scene.unit, scene.iso);
  if (state.state === 'ready') {
    const line = el('div', 'hdr-note');
    line.dataset.state = off ? 'uncalibrated' : 'calibrated';
    if (off) {
      const why = el('span', 'why');
      why.append(el('b', '', '未校准：'), document.createTextNode(uncalibratedReason(scene.photoUnit, scene.unit, scene.iso)));
      line.append(why);
    } else {
      const ok = el('span', 'ok');
      ok.append(icon(CHECK));
      line.append(ok, el('span', '', '已校准：颜色按校准数据预测。'));
    }
    return line;
  }
  if (state.state === 'checking') return el('p', 'dim', '正在核对校准数据…');
  const box = el('div');
  const banner = el('div', 'bn');
  banner.dataset.state = 'no-calibration';
  const mark = el('span', 'warn');
  mark.append(icon(WARN));
  const text = el('div', 'tx');
  if (state.state === 'none') {
    text.append(el('b', '', '还没导入校准数据。'), document.createTextNode('现在所有配方都只显示中性转换，和相机里的颜色不同。导出也一样。'));
  } else {
    text.append(el('b', '', state.message), document.createTextNode(' 这张照片的配方只显示中性转换，和相机里的颜色不同。'));
  }
  banner.append(mark, text);
  const bring = button('btn primary block', '导入校准数据');
  bring.prepend(icon(PLUS));
  bring.addEventListener('click', pickCalib);
  box.append(banner, bring, el('p', 'faint small', '在 Mac 的 GXR Studio 里导出校准数据，用隔空投送发到这台 iPhone，再点上面的按钮。只需一次。'));
  return box;
}

export function openRecipeStatusSheet(
  shell: HTMLElement,
  book: Book,
  scene: RecipeStatusScene,
  pickDat: () => void,
  pickCalib: () => void,
): void {
  const nodes: HTMLElement[] = [];
  if (book.datError) nodes.push(datFileBox(book.datError, pickDat));
  if (scene.camError) {
    const box = el('div', 'bn');
    box.dataset.cameraRecipeError = scene.camError.code;
    const mark = el('span', 'warn');
    mark.append(icon(WARN));
    box.append(mark, el('div', 'tx', formatCameraRecipeEditorError(scene.camError.message)));
    nodes.push(box);
  } else if (scene.adopted?.levelsUnknown) {
    const note = el('p', 'faint small', CAMERA_RECIPE_LEVELS_UNKNOWN);
    note.dataset.cameraRecipeNote = 'levels-unknown';
    nodes.push(note);
  }
  nodes.push(buildStatusBlock(scene, pickCalib));
  if (book.datNote) nodes.push(el('p', 'faint small', book.datNote));
  openRecipeInfoSheet(shell, '配方状态', nodes);
}

function askLeadNodes(datError: string | null): HTMLElement[] {
  return datError
    ? [el('p', 'lead', datError)]
    : [
        el('p', 'lead', '配方要用存储卡上的相机配方文件，这次没有和照片一起选上。'),
        el('p', 'lead', '每张卡选一次就行。没选之前，照片只显示中性转换。'),
      ];
}

export function openAskDatSheet(shell: HTMLElement, book: Book, pickDat: () => void): AskSheetHandle {
  shell.querySelector('.ask-scrim')?.remove();
  shell.querySelector('.ask-sheet')?.remove();

  const scrim = el('div', 'scrim ask-scrim');
  const sheet = el('div', 'ms ask-sheet');
  sheet.dataset.state = 'no-recipe-file';
  const head = el('div', 'hd');
  head.append(el('h2', '', '还差相机配方文件'));
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
  const pick = button('btn primary lg block', '在「文件」里选 GXRCE.DAT 和 GXRFX.BIN');
  pick.prepend(icon(FILE));
  pick.addEventListener('click', () => {
    close();
    pickDat();
  });
  const later = button('btn lg ghost block', '先不用，只看中性转换');
  later.addEventListener('click', close);
  scrim.addEventListener('click', close);
  const leads = el('div', 'ask-leads');
  leads.append(...askLeadNodes(book.datError));
  sheet.append(el('div', 'grab'), head, leads, path, pick, later);
  shell.append(scrim, sheet);

  return {
    close,
    sync(nextBook: Book) {
      if (!leads.isConnected) return;
      if (nextBook.dat && !nextBook.datError) {
        close();
        return;
      }
      leads.replaceChildren(...askLeadNodes(nextBook.datError));
    },
  };
}

export function buildKnobRow(
  control: Control,
  generation: number | null,
  savedLevels: Record<string, number> | undefined,
  onLevel: (level: number) => void,
): HTMLElement {
  const levels = choices(control, generation);
  const current = effective(control, savedLevels);
  const row = el('div', 'krow');
  row.dataset.knob = control.name;
  const value = el('span', 'val num', levelText(control, current));
  const input = document.createElement('input');
  input.type = 'range';
  input.min = '0';
  input.max = String(Math.max(0, levels.length - 1));
  input.step = '1';
  input.value = String(nearestIndex(levels, current));
  input.setAttribute('aria-label', KNOB_NAMES[control.name] ?? control.label);
  const paint = () => {
    const level = levels[Number(input.value)] ?? control.default;
    value.textContent = levelText(control, level);
    const span = Math.max(1, levels.length - 1);
    const zero = nearestIndex(levels, control.name === 'amount' ? levels[0] : 0);
    const at = Number(input.value);
    input.style.setProperty('--a', `${(Math.min(at, zero) / span) * 100}%`);
    input.style.setProperty('--b', `${(Math.max(at, zero) / span) * 100}%`);
  };
  paint();
  input.addEventListener('input', paint);
  input.addEventListener('change', () => {
    const level = levels[Number(input.value)];
    if (level !== undefined) onLevel(level);
  });
  const track = el('div', 'slider');
  track.append(input);
  row.append(el('span', 'lab', KNOB_NAMES[control.name] ?? control.label), track, value);
  return row;
}

export function centerStripCard(strip: HTMLElement, cardId: string): boolean {
  if (!strip.isConnected || strip.clientWidth <= 0) return false;
  const active = strip.querySelector<HTMLElement>('.rc.on');
  if (!active || active.dataset.card !== cardId) return false;
  const sr = strip.getBoundingClientRect();
  const cr = active.getBoundingClientRect();
  const targetLeft = strip.scrollLeft + (cr.left - sr.left) + cr.width / 2 - sr.width / 2;
  const maxScroll = Math.max(0, strip.scrollWidth - strip.clientWidth);
  strip.scrollLeft = Math.max(0, Math.min(maxScroll, targetLeft));
  return true;
}

const bitmapIds = new WeakMap<ImageBitmap, number>();
let nextBitmapId = 1;

export function bitmapId(bitmap: ImageBitmap): number {
  let id = bitmapIds.get(bitmap);
  if (!id) {
    id = nextBitmapId++;
    bitmapIds.set(bitmap, id);
  }
  return id;
}
