// 配方与 LUT 共用的 128pt 单带辅件：带首「配方 | LUT」切换键、配方筛选工作表、状态说明与 06d 缺文件弹层。

import { crossed, uncalibratedReason, type Unit } from '../lib/calib';
import { DEFAULT_LABEL, DEFAULT_NOTE } from '../lib/default_calib';
import { deviceName } from '../lib/device';
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
import { markControlDragEnd } from './drag_guard';
import { button, el, icon } from './dom';
import { registerSheet } from './sheet_lifecycle';

const CHECK = 'M5 12.5l4.5 4.5L19 7.5';
const WARN = 'M12 4l9 16H3zM12 10v4M12 17h.01';
const INFO = 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18M12 11v5M12 8h.01';
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

export function shelfCount(list: Shelves, which: Shelf): number {
  if (which === 'builtin') return list.builtin.length;
  if (which === 'looks') return list.looks.length;
  if (which === 'mine') return list.users.length;
  return list.builtin.length + list.looks.length + list.users.length;
}

/** 固定在配方横条左侧的 52×56pt 筛选卡（导出条占住顶槽时也随时可达）。 */
export function makeRecipeFilterTile(which: Shelf, count: number, onClick: () => void): HTMLButtonElement {
  const tile = button('rc filter-tile', '');
  tile.dataset.act = 'recipe-filter';
  tile.dataset.shelf = which;
  tile.setAttribute('aria-label', `筛选配方：${shelfShortLabel(which)} ${count}`);
  const box = el('div', 'th filter-th');
  box.append(el('span', 'flt-top', shelfShortLabel(which)), el('span', 'flt-cnt num', `▾ ${count}`));
  tile.append(box, el('span', 'nm', '筛选'));
  tile.addEventListener('click', onClick);
  return tile;
}

export interface RecipeStatusSpec {
  label: string;
  tone: 'warn' | 'dim';
  stateAttr?: 'no-calibration' | 'uncalibrated';
  cameraRecipeError?: string;
  cameraRecipeNote?: 'levels-unknown';
  action: 'jpg-info' | 'ask-dat' | 'status-sheet';
}

export function recipeStatusSpec(
  scene: RecipeStatusScene & { raw: boolean; dat: boolean },
  diptych: boolean,
  hasDatError: boolean,
): RecipeStatusSpec | null {
  if (!scene.raw) {
    return { label: diptych ? '半格双联' : '配方需要 RAW', tone: 'warn', action: 'jpg-info' };
  }
  if (scene.camError) {
    return { label: '拍摄配方', tone: 'warn', cameraRecipeError: scene.camError.code, action: 'status-sheet' };
  }
  if (!scene.dat) {
    return { label: '选配方文件', tone: 'warn', action: 'ask-dat' };
  }
  const calibrated = scene.state.state === 'ready';
  const off = calibrated && crossed(scene.photoUnit, scene.unit, scene.iso);
  if (scene.adopted?.levelsUnknown) {
    return { label: '档位未标', tone: 'dim', cameraRecipeNote: 'levels-unknown', action: 'status-sheet' };
  }
  if (!calibrated) {
    return { label: '未校准', tone: 'warn', stateAttr: 'no-calibration', action: 'status-sheet' };
  }
  if (hasDatError) {
    return { label: '配方文件异常', tone: 'warn', stateAttr: 'uncalibrated', action: 'status-sheet' };
  }
  if (off) {
    return { label: '校准范围外（推算）', tone: 'dim', stateAttr: 'uncalibrated', action: 'status-sheet' };
  }
  return null;
}

/** 固定在配方横条左侧的 52×70pt 状态瓷砖（导出条占住顶槽时也随时可达）。 */
export function makeRecipeStatusTile(spec: RecipeStatusSpec, onClick: () => void): HTMLButtonElement {
  const tile = button(`rc filter-tile status-tile ${spec.tone}`, '');
  tile.dataset.act = 'recipe-status';
  if (spec.stateAttr) tile.dataset.state = spec.stateAttr;
  if (spec.cameraRecipeError) tile.dataset.cameraRecipeError = spec.cameraRecipeError;
  if (spec.cameraRecipeNote) tile.dataset.cameraRecipeNote = spec.cameraRecipeNote;
  tile.setAttribute('aria-label', `配方状态：${spec.label}`);
  const box = el('div', 'th filter-th');
  box.append(icon(spec.tone === 'warn' ? WARN : INFO), el('span', 'status-lab', spec.label));
  tile.append(box);
  tile.addEventListener('click', onClick);
  return tile;
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

export function buildStatusBlock(scene: RecipeStatusScene, pickCalib: () => void, book: Book): HTMLElement {
  const state = scene.state;
  const off = state.state === 'ready' && crossed(scene.photoUnit, scene.unit, scene.iso);
  if (state.state === 'ready') {
    const line = el('div', 'hdr-note');
    line.dataset.state = off ? 'uncalibrated' : 'calibrated';
    if (off) {
      const why = el('span', 'why');
      why.append(el('b', '', '校准范围外（推算）：'), document.createTextNode(uncalibratedReason(scene.photoUnit, scene.unit, scene.iso)));
      line.append(why);
    } else if (book.pack?.kind === 'default') {
      // 内置的默认校准：写明来自作者的样机，想更准可以换成自己的。
      const ok = el('span', 'ok');
      ok.append(icon(CHECK));
      const text = el('span');
      text.append(el('b', '', DEFAULT_LABEL), el('br'), el('small', 'faint', DEFAULT_NOTE));
      line.dataset.calib = 'default';
      line.append(ok, text);
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
  if (state.state === 'none' && book.defaultError) {
    // 默认校准下载不下来或核对没通过：写明原因。
    banner.dataset.reason = 'default';
    text.append(el('b', '', '未校准：'), document.createTextNode(`${book.defaultError}现在所有配方都只显示中性转换，和相机里的颜色不同。导出也一样。`));
  } else if (state.state === 'none') {
    text.append(el('b', '', '还没导入校准数据。'), document.createTextNode('现在所有配方都只显示中性转换，和相机里的颜色不同。导出也一样。'));
  } else {
    text.append(el('b', '', state.message), document.createTextNode(' 这张照片的配方只显示中性转换，和相机里的颜色不同。'));
  }
  banner.append(mark, text);
  const bring = button('btn primary block', '导入校准数据');
  bring.prepend(icon(PLUS));
  bring.addEventListener('click', pickCalib);
  box.append(banner, bring, el('p', 'faint small', `在 Mac 的 GXR Studio 里导出校准数据，用隔空投送发到这台 ${deviceName()}，再点上面的按钮。只需一次。`));
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
  if (scene.camError?.code === 'camera_recipe_catalog_missing' && scene.state.state === 'checking') {
    nodes.push(datFileBox('换上拍摄这张照片所用存储卡上的相机配方文件后即可生成预测。', pickDat));
  } else {
    nodes.push(buildStatusBlock(scene, pickCalib, book));
  }
  if (book.datNote) nodes.push(el('p', 'faint small', book.datNote));
  openRecipeInfoSheet(shell, '配方状态', nodes);
}

function askLeadNodes(datError: string | null): HTMLElement[] {
  return datError
    ? [el('p', 'lead', datError)]
    : [
        el('p', 'lead', '配方要用存储卡最外层的相机配方文件（GXRCE.DAT 和 GXRFX.BIN）。'),
        el('p', 'lead', '选一次就会记住，以后只选照片就行。'),
      ];
}

export function openAskDatSheet(shell: HTMLElement, book: Book, pickDat: () => void, hasJpg = false): AskSheetHandle {
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
  const later = button('btn lg ghost block', hasJpg ? '先不用，继续看相机 JPG' : '先不用，只看中性转换');
  later.dataset.act = 'ask-later';
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
  onScrub?: (text: string | null) => void,
): HTMLElement {
  const levels = choices(control, generation);
  const current = effective(control, savedLevels);
  const label = KNOB_NAMES[control.name] ?? control.label;
  const row = el('div', 'krow');
  row.dataset.knob = control.name;
  const value = el('span', 'val num', levelText(control, current));
  const input = document.createElement('input');
  input.type = 'range';
  input.min = '0';
  input.max = String(Math.max(0, levels.length - 1));
  input.step = '1';
  input.value = String(nearestIndex(levels, current));
  input.setAttribute('aria-label', label);
  const paint = () => {
    const level = levels[Number(input.value)] ?? control.default;
    const text = levelText(control, level);
    value.textContent = text;
    const span = Math.max(1, levels.length - 1);
    const zero = nearestIndex(levels, control.name === 'amount' ? levels[0] : 0);
    const at = Number(input.value);
    input.style.setProperty('--a', `${(Math.min(at, zero) / span) * 100}%`);
    input.style.setProperty('--b', `${(Math.max(at, zero) / span) * 100}%`);
    return text;
  };
  paint();
  let dragging = false;
  input.addEventListener('pointerdown', () => {
    dragging = true;
    onScrub?.(`${label} ${value.textContent ?? ''}`);
  });
  input.addEventListener('input', () => {
    const text = paint();
    onScrub?.(`${label} ${text}`);
    if (!dragging) onScrub?.(null);
  });
  const end = () => {
    if (!dragging) return;
    dragging = false;
    markControlDragEnd();
    onScrub?.(null);
  };
  input.addEventListener('pointerup', end);
  input.addEventListener('pointercancel', end);
  input.addEventListener('blur', () => {
    if (!dragging) onScrub?.(null);
  });
  input.addEventListener('change', () => {
    if (!dragging) onScrub?.(null);
    const level = levels[Number(input.value)];
    if (level !== undefined) onLevel(level);
  });
  const track = el('div', 'slider');
  track.append(input);
  row.append(el('span', 'lab', label), track, value);
  return row;
}

export function centerStripCard(strip: HTMLElement, cardId: string): boolean {
  if (!strip.isConnected || strip.clientWidth <= 0) return false;
  const active = strip.querySelector<HTMLElement>('.rc.on');
  if (!active || active.dataset.card !== cardId) return false;
  const sr = strip.getBoundingClientRect();
  const cr = active.getBoundingClientRect();
  let targetLeft = strip.scrollLeft + (cr.left - sr.left) + cr.width / 2 - sr.width / 2;
  const usable = (el: Element | null): HTMLElement | null =>
    el instanceof HTMLElement && el.classList.contains('rc') && !el.classList.contains('lockd') ? el : null;
  const neighbor = usable(active.nextElementSibling) ?? usable(active.previousElementSibling);
  if (neighbor) {
    const nr = neighbor.getBoundingClientRect();
    const pairLeft = Math.min(cr.left, nr.left);
    const pairWidth = Math.max(cr.right, nr.right) - pairLeft;
    const gap = Math.max(0, pairWidth - cr.width - nr.width);
    // 390pt 竖屏下横条可视宽约 231pt，只把选中卡放正中会把左右邻卡各裁切一截；
    // 让「选中卡 + 一张邻卡」靠边缘完整落入视口，另一侧邻卡中心也保留在视口内，兼顾完整邻卡点击与三卡中心可达。
    if (pairWidth <= sr.width && (sr.width - cr.width) / 2 < nr.width + gap) {
      const edgeInset = Math.max(2, Math.min(6, Math.floor(sr.width - pairWidth - gap - cr.width / 2 - 2)));
      targetLeft =
        nr.left >= cr.left
          ? strip.scrollLeft + (nr.right - sr.right) + edgeInset
          : strip.scrollLeft + (nr.left - sr.left) - edgeInset;
    }
  }
  const maxScroll = Math.max(0, strip.scrollWidth - strip.clientWidth);
  strip.scrollLeft = Math.max(0, Math.min(maxScroll, targetLeft));
  return true;
}

export function buildIpadRecipeMeta(
  scene: RecipeStatusScene & { raw: boolean; dat: boolean },
  targetName: string,
  targetTag: string,
  diptych: boolean,
  datError: string | null,
  onStatusClick: (action: RecipeStatusSpec['action']) => void,
  fallbackLabel = '中性转换',
): HTMLElement {
  const wrap = el('div', 'ipad-recipe-meta');
  const head = el('div', 'rmeta');
  if (!scene.raw) {
    head.append(el('b', 'rname dim', diptych ? '半格双联' : '相机 JPG'));
  } else if (!scene.dat) {
    head.append(el('b', 'rname dim', fallbackLabel));
  } else {
    head.append(el('b', 'rname', targetName));
    if (targetTag) head.append(el('span', 'tag neutral-tag', targetTag));
  }
  wrap.append(head);

  const spec = recipeStatusSpec(scene, diptych, Boolean(datError));
  if (spec) {
    const btn = button(`ipad-status-btn ${spec.tone}`, spec.label);
    btn.dataset.act = 'recipe-status';
    if (spec.stateAttr) btn.dataset.state = spec.stateAttr;
    if (spec.cameraRecipeError) btn.dataset.cameraRecipeError = spec.cameraRecipeError;
    if (spec.cameraRecipeNote) btn.dataset.cameraRecipeNote = spec.cameraRecipeNote;
    btn.prepend(icon(spec.tone === 'warn' ? WARN : INFO));
    btn.addEventListener('click', () => onStatusClick(spec.action));
    wrap.append(btn);
  }
  return wrap;
}

const bitmapIds = new WeakMap<ImageBitmap, number>();
let nextBitmapId = 1;
let nextLutMenuSeq = 1;

export interface LutPinMenuHandle {
  trigger: HTMLButtonElement;
  popover: HTMLElement;
  sync(selected: boolean, badgeText: string | null, spaceLabel: string, warn: boolean): void;
  close(fromPointer?: boolean, restoreFocus?: boolean): void;
  destroy(): void;
}

/** 手机竖屏 LUT 左侧合并瓷砖（44×70pt）与原生顶层弹出菜单（导入 LUT + 色彩空间）。 */
export function createLutPinMenu(onImport: () => void, onSpace: () => void): LutPinMenuHandle {
  const seq = nextLutMenuSeq++;
  const triggerId = `lut-pin-trigger-${seq}`;
  const menuId = `lut-pin-menu-${seq}`;

  const hasInvokers = 'commandForElement' in HTMLButtonElement.prototype;
  const trigger = button('rc filter-tile lut-menu-tile', '');
  trigger.id = triggerId;
  trigger.dataset.act = 'lut-menu';
  trigger.setAttribute('aria-haspopup', 'menu');
  trigger.setAttribute('aria-expanded', 'false');
  if (hasInvokers) {
    trigger.setAttribute('commandfor', menuId);
    trigger.setAttribute('command', 'toggle-popover');
  }
  const th = el('div', 'th filter-th');
  const label = el('span', 'nm', '导入');
  trigger.append(th, label);

  const popover = el('div', 'lut-menu-pop');
  popover.id = menuId;
  popover.setAttribute('popover', 'auto');
  const list = el('div', 'lut-menu-list');
  list.setAttribute('focusgroup', 'menu nomemory');
  list.setAttribute('aria-labelledby', triggerId);

  const importBtn = button('lut-menu-item', '');
  importBtn.dataset.act = 'lut-menu-import';
  importBtn.append(icon(PLUS), el('span', 'lut-menu-lab', '导入 LUT'));

  const spaceBtn = button('lut-menu-item', '');
  spaceBtn.dataset.act = 'lut-menu-space';
  const spaceVal = el('span', 'lut-menu-val', '未选 LUT');
  spaceBtn.append(el('span', 'lut-menu-lab', '色彩空间'), spaceVal);

  list.append(importBtn, spaceBtn);
  popover.append(list);

  let enterOnOpen = false;
  let pointerOpen = false;
  let openAtPointerDown = false;

  const isOpen = (): boolean => {
    try {
      return popover.matches(':popover-open');
    } catch {
      return false;
    }
  };
  const enabledItems = (): HTMLButtonElement[] => [importBtn, spaceBtn].filter((btn) => !btn.disabled);
  const focusItem = (index: number): void => {
    const target = enabledItems()[index];
    if (target) target.focus({ preventScroll: true });
  };
  const close = (fromPointer = false, restoreFocus = false): void => {
    if (!isOpen()) return;
    if (restoreFocus) {
      if (fromPointer) trigger.focus({ focusVisible: false } as FocusOptions);
      else trigger.focus();
    }
    const pop = popover as HTMLElement & { hidePopover?: () => void };
    pop.hidePopover?.();
  };
  const positionFallback = (): void => {
    if (typeof CSS !== 'undefined' && typeof CSS.supports === 'function' && CSS.supports('position-area', 'block-end')) {
      return;
    }
    const trig = trigger.getBoundingClientRect();
    const pop = popover.getBoundingClientRect();
    const gap = 8;
    const left = Math.max(8, Math.min(window.innerWidth - pop.width - 8, trig.left + 4));
    const above = trig.top - pop.height - gap;
    const top = above >= 8 ? above : Math.min(window.innerHeight - pop.height - 8, trig.bottom + gap);
    popover.style.left = `${Math.round(left)}px`;
    popover.style.top = `${Math.round(Math.max(8, top))}px`;
  };

  importBtn.addEventListener('click', (ev) => {
    close(ev.detail !== 0, true);
    onImport();
  });
  spaceBtn.addEventListener('click', (ev) => {
    close(ev.detail !== 0, true);
    onSpace();
  });

  trigger.addEventListener('pointerdown', (ev) => {
    if (ev.pointerType === 'mouse') ev.preventDefault();
    pointerOpen = true;
    openAtPointerDown = isOpen();
  });
  trigger.addEventListener('pointercancel', () => {
    pointerOpen = false;
    openAtPointerDown = false;
  });
  trigger.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      openAtPointerDown = false;
      pointerOpen = false;
      enterOnOpen = true;
      if (!isOpen()) trigger.click();
      else focusItem(0);
    } else if (ev.key === 'Enter' || ev.key === ' ') {
      openAtPointerDown = false;
      pointerOpen = false;
      enterOnOpen = true;
    }
  });
  if (!hasInvokers) {
    trigger.addEventListener('click', (ev) => {
      ev.preventDefault();
      // 只有真点击（detail ≠ 0）才看按下时的状态：按下后拖走再由脚本或读屏 click() 时，不能把它当成「再点一次关掉」。
      const wasOpen = ev.detail !== 0 && openAtPointerDown;
      openAtPointerDown = false;
      if (wasOpen || isOpen()) {
        if (isOpen()) {
          close(ev.detail !== 0, true);
        } else if (ev.detail !== 0) {
          trigger.focus({ focusVisible: false } as FocusOptions);
        } else {
          trigger.focus();
        }
      } else {
        enterOnOpen = ev.detail === 0 && !pointerOpen;
        const pop = popover as HTMLElement & { showPopover?: (opts?: { source?: HTMLElement }) => void };
        pop.showPopover?.({ source: trigger });
      }
    });
  }

  popover.addEventListener('toggle', (ev) => {
    const open = (ev as ToggleEvent).newState === 'open';
    trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      positionFallback();
      if (enterOnOpen) {
        enterOnOpen = false;
        pointerOpen = false;
        focusItem(0);
      } else if (pointerOpen) {
        pointerOpen = false;
        trigger.focus({ focusVisible: false } as FocusOptions);
      }
    } else {
      enterOnOpen = false;
      pointerOpen = false;
    }
  });

  const nativeFocusGroup = 'focusGroup' in HTMLElement.prototype;
  if (!nativeFocusGroup) {
    list.setAttribute('role', 'menu');
    importBtn.setAttribute('role', 'menuitem');
    spaceBtn.setAttribute('role', 'menuitem');
  }
  list.addEventListener('keydown', (ev) => {
    if (ev.key === 'Tab') {
      close(false, false);
      return;
    }
    const items = enabledItems();
    if (!items.length) return;
    if (ev.key === 'Home') {
      ev.preventDefault();
      items[0].focus({ preventScroll: true });
    } else if (ev.key === 'End') {
      ev.preventDefault();
      items[items.length - 1].focus({ preventScroll: true });
    } else if (!nativeFocusGroup && (ev.key === 'ArrowDown' || ev.key === 'ArrowUp')) {
      ev.preventDefault();
      const cur = items.indexOf(document.activeElement as HTMLButtonElement);
      const step = ev.key === 'ArrowUp' ? -1 : 1;
      items[cur < 0 ? 0 : (cur + step + items.length) % items.length].focus({ preventScroll: true });
    }
  });

  const onDocFocusIn = (ev: FocusEvent) => {
    if (!isOpen()) return;
    const target = ev.target;
    if (target instanceof Node && (popover.contains(target) || trigger.contains(target))) return;
    close(false, false);
  };
  document.addEventListener('focusin', onDocFocusIn);

  return {
    trigger,
    popover,
    sync(selected, badgeText, spaceLabel, warn) {
      trigger.classList.toggle('has-lut', selected);
      trigger.classList.toggle('warn', selected && warn);
      if (selected) {
        th.replaceChildren(el('span', 'lut-menu-badge', badgeText ?? '选空间'));
        label.textContent = '管理';
        trigger.setAttribute('aria-label', `LUT 管理：${spaceLabel}`);
      } else {
        th.replaceChildren(icon(PLUS));
        label.textContent = '导入';
        trigger.setAttribute('aria-label', '导入 LUT 与色彩空间');
      }
      spaceBtn.disabled = !selected;
      spaceBtn.classList.toggle('warn', selected && warn);
      spaceVal.textContent = selected ? spaceLabel : '未选 LUT';
    },
    close,
    destroy() {
      close(false, false);
      document.removeEventListener('focusin', onDocFocusIn);
    },
  };
}

export function bitmapId(bitmap: ImageBitmap): number {
  let id = bitmapIds.get(bitmap);
  if (!id) {
    id = nextBitmapId++;
    bitmapIds.set(bitmap, id);
  }
  return id;
}
