// 校准数据那张卡（「更多 → 校准数据」）：现在用的是默认校准还是自己的、哪些镜头和 ISO 校准过、换成自己的、恢复默认。
// 起始页不再放这张卡：没导入时用内置的默认校准（lib/default_calib.ts），第一步就是选照片和相机配方文件。

import { UNIT_LABEL, unitLine, type UnitPack } from '../lib/calib';
import { camFilesRememberedLabel } from '../lib/camfiles';
import { DEFAULT_LABEL, DEFAULT_NOTE } from '../lib/default_calib';
import { deviceName } from '../lib/device';
import type { Book } from './book';
import { button, el } from './dom';
import type { AppApi } from './model';

function tag(text: string, kind: 'warn' | 'ok'): HTMLElement {
  const node = el('span', `tag ${kind}`, text);
  node.style.marginLeft = 'auto';
  return node;
}

/** 包里每个单元一行（选了相机配方文件后带上核对结果），再加一行没校准的镜头。 */
function unitList(book: Book, units: UnitPack[]): HTMLElement {
  const list = el('div', 'units');
  for (const row of units) {
    const state = book.dat ? book.unitState(row.unit) : null;
    const line = el('p', 'unit', unitLine(row.unit));
    line.dataset.unit = row.unit;
    if (state?.state === 'invalid') {
      line.append(el('span', 'bad', ` · ${state.message}`));
      line.dataset.state = 'invalid';
    } else line.dataset.state = state?.state ?? 'listed';
    list.append(line);
  }
  const missing = (['jlaunch', 'qlaunch', 'l08firm'] as const).filter((unit) => !units.some((row) => row.unit === unit));
  if (missing.length) list.append(el('p', 's', `${missing.map((unit) => UNIT_LABEL[unit]).join('、')} 没有校准数据，这些镜头拍的照片只显示中性转换。`));
  return list;
}

/** 「换成自己的校准…」：就是原来的导入。 */
function importButton(api: AppApi, book: Book, label: string, block: boolean): HTMLButtonElement {
  const choose = button(block ? 'btn block' : 'btn', book.packBusy ? '正在核对校准数据…' : label);
  choose.dataset.act = 'calib-import';
  choose.disabled = book.packBusy;
  choose.addEventListener('click', () => api.pickCalib());
  return choose;
}

/** 按现在的状态画一次。导入、换成自己的、恢复默认、默认校准加载完后由调用方重画。 */
function paint(card: HTMLElement, api: AppApi, book: Book): void {
  card.replaceChildren();
  const pack = book.pack;
  card.dataset.calib = pack ? pack.kind : book.loadingDefault ? 'loading' : 'none';
  const hd = el('div', 'hd');
  if (pack?.kind === 'user') {
    hd.append(el('b', '', '校准数据'), tag('已导入', 'ok'));
    card.append(hd, el('p', '', '已导入：配方按校准数据预测。'), unitList(book, pack.units), el('p', 's', `来自「${pack.name}」`));
    const acts = el('div', 'acts');
    // 删掉自己导入的包就回到默认校准。
    const restore = button('btn', '恢复默认校准');
    restore.dataset.act = 'calib-restore-default';
    restore.disabled = book.packBusy;
    restore.addEventListener('click', () => void book.removePack());
    acts.append(importButton(api, book, '换成自己的校准…', false), restore);
    card.append(acts);
  } else if (pack) {
    hd.append(el('b', '', DEFAULT_LABEL));
    card.append(hd, el('p', 's', DEFAULT_NOTE), unitList(book, pack.units), importButton(api, book, '换成自己的校准…', true));
  } else if (book.loadingDefault) {
    hd.append(el('b', '', '校准数据'));
    card.append(hd, el('p', 'dim', '正在加载默认校准…'), importButton(api, book, '换成自己的校准…', true));
  } else {
    hd.append(el('b', '', '校准数据'), tag('未校准', 'warn'));
    const why = el('p', '', `${book.defaultError ?? ''}配方只显示中性转换，和相机里的颜色不同。`);
    why.dataset.state = 'default-failed';
    card.append(
      hd,
      why,
      el('p', 's', `也可以用自己的校准：在 Mac 的 GXR Studio 里导出，用隔空投送发到这台 ${deviceName()}。`),
      importButton(api, book, '选择校准数据文件', true),
    );
  }
  if (book.packError) {
    const line = el('p', 'bad', book.packError);
    line.dataset.state = 'calib-error';
    line.setAttribute('role', 'status');
    card.append(line);
  }
}

/** 一张会跟着校准数据变化自己重画的卡。离开页面后不再重画。 */
/** 网格「更多」里「校准数据」那一行的小字（从 grid.ts 原样移来）。 */
export function calibDetail(api: AppApi): string {
  const book = api.book();
  const pack = book.pack;
  if (pack?.kind === 'default') return DEFAULT_LABEL;
  if (pack) return `已导入：${pack.units.map((row) => UNIT_LABEL[row.unit]).join('、')}`;
  return book.loadingDefault ? '正在加载默认校准…' : '未校准';
}

export function calibCard(api: AppApi): HTMLElement {
  const book = api.book();
  const card = el('div', 'card calib');
  paint(card, api, book);
  const off = book.onChange(() => {
    if (!card.isConnected) {
      off();
      return;
    }
    paint(card, api, book);
  });
  return card;
}

function paintCamFiles(card: HTMLElement, api: AppApi, book: Book): void {
  card.replaceChildren();
  const dat = book.dat;
  card.dataset.camfiles = dat ? 'remembered' : 'none';
  const hd = el('div', 'hd');
  if (dat) {
    hd.append(el('b', '', '相机配方文件'), tag(camFilesRememberedLabel(dat.generation), 'ok'));
    const note = el('p', 's', `只存在这台 ${deviceName()} 的浏览器里，不会上传。换新固件（配方库变了）时再换一份。`);
    const acts = el('div', 'acts');
    const replace = button('btn', book.datBusy ? '正在读取…' : '换一份…');
    replace.dataset.act = 'camfiles-replace';
    replace.disabled = book.datBusy;
    replace.addEventListener('click', () => api.pickDat());
    const forget = button('btn danger', '忘掉');
    forget.dataset.act = 'camfiles-forget';
    forget.disabled = book.datBusy;
    forget.addEventListener('click', () => {
      void book.forgetDat().catch(() => undefined);
    });
    acts.append(replace, forget);
    card.append(hd, note, acts);
  } else {
    const emptyTag = el('span', 'tag neutral-tag', '未记住');
    emptyTag.style.marginLeft = 'auto';
    hd.append(el('b', '', '相机配方文件'), emptyTag);
    const note = el('p', 's', `存储卡最外层的 GXRCE.DAT 和 GXRFX.BIN。选一次就会留在这台 ${deviceName()}，以后只选照片。`);
    const pick = button('btn block', book.datBusy ? '正在读取…' : '选择相机配方文件…');
    pick.dataset.act = 'camfiles-pick';
    pick.disabled = book.datBusy;
    pick.addEventListener('click', () => api.pickDat());
    card.append(hd, note, pick);
  }
  if (book.datError) {
    const line = el('p', 'bad', book.datError);
    line.dataset.state = 'camfiles-error';
    line.setAttribute('role', 'status');
    card.append(line);
  }
}

/** 一张会跟着相机配方文件变化自动重画的卡（「更多 → 校准与配方文件」）。 */
export function camFilesCard(api: AppApi): HTMLElement {
  const book = api.book();
  const card = el('div', 'card camfiles');
  paintCamFiles(card, api, book);
  const off = book.onChange(() => {
    if (!card.isConnected) {
      off();
      return;
    }
    paintCamFiles(card, api, book);
  });
  return card;
}

/** 「更多 → 校准数据 / 校准与配方文件」：相机配方文件卡与校准数据卡并列。 */
export function showCalibSheet(host: HTMLElement, api: AppApi): void {
  host.querySelector('.scrim.calib-scrim')?.remove();
  host.querySelector('.ms.calib-sheet')?.remove();
  const scrim = el('div', 'scrim calib-scrim');
  const sheet = el('div', 'ms calib-sheet');
  sheet.dataset.state = 'calibration';
  const row = el('div', 'hd');
  row.append(el('h2', '', '校准与配方文件'));
  const done = button('nb', '完成');
  const close = () => {
    scrim.remove();
    sheet.remove();
  };
  done.addEventListener('click', close);
  scrim.addEventListener('click', close);
  row.append(done);
  sheet.append(el('div', 'grab'), row, camFilesCard(api), calibCard(api));
  host.append(scrim, sheet);
}
