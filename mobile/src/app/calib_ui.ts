// 校准数据那张卡（起始页 01、「更多 → 校准数据」）：导入、哪些镜头和 ISO 校准过、替换、删除。

import { UNIT_LABEL, unitLine } from '../lib/calib';
import type { Book } from './book';
import { button, el } from './dom';
import type { AppApi } from './model';

function tag(text: string, kind: 'warn' | 'ok'): HTMLElement {
  const node = el('span', `tag ${kind}`, text);
  node.style.marginLeft = 'auto';
  return node;
}

/** 按现在的状态画一次。导入、替换、删除后由调用方重画。 */
function paint(card: HTMLElement, api: AppApi, book: Book): void {
  card.replaceChildren();
  card.dataset.calib = book.pack ? 'ready' : 'none';
  const hd = el('div', 'hd');
  if (!book.pack) {
    hd.append(el('b', '', '导入校准数据'), el('span', 'dim', '只需一次'), tag('未导入', 'warn'));
    card.append(
      hd,
      el('p', '', '还没导入：配方只显示中性转换，和相机里的颜色不同。'),
      el('p', 's', '在 Mac 的 GXR Studio 里导出，用隔空投送发到这台 iPhone。'),
    );
    const choose = button('btn block', book.packBusy ? '正在核对校准数据…' : '选择校准数据文件');
    choose.disabled = book.packBusy;
    choose.addEventListener('click', () => api.pickCalib());
    card.append(choose);
  } else {
    hd.append(el('b', '', '校准数据'), tag('已导入', 'ok'));
    card.append(hd, el('p', '', '已导入：配方按校准数据预测。'));
    const list = el('div', 'units');
    for (const row of book.pack.units) {
      const state = book.dat ? book.unitState(row.unit) : null;
      const line = el('p', 'unit', unitLine(row.unit));
      line.dataset.unit = row.unit;
      if (state?.state === 'invalid') {
        line.append(el('span', 'bad', ` · ${state.message}`));
        line.dataset.state = 'invalid';
      } else line.dataset.state = state?.state ?? 'listed';
      list.append(line);
    }
    const missing = (['jlaunch', 'qlaunch', 'l08firm'] as const).filter((unit) => !book.pack?.units.some((row) => row.unit === unit));
    if (missing.length) list.append(el('p', 's', `${missing.map((unit) => UNIT_LABEL[unit]).join('、')} 没有校准数据，这些镜头拍的照片只显示中性转换。`));
    card.append(list, el('p', 's', `来自「${book.pack.name}」`));
    const acts = el('div', 'acts');
    const replace = button('btn', book.packBusy ? '正在核对校准数据…' : '替换');
    replace.disabled = book.packBusy;
    replace.addEventListener('click', () => api.pickCalib());
    const remove = button('btn', '删除');
    remove.disabled = book.packBusy;
    remove.addEventListener('click', () => void book.removePack());
    acts.append(replace, remove);
    card.append(acts);
  }
  if (book.packError) {
    const line = el('p', 'bad', book.packError);
    line.dataset.state = 'calib-error';
    line.setAttribute('role', 'status');
    card.append(line);
  }
}

/** 一张会跟着校准数据变化自己重画的卡。离开页面后不再重画。 */
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

/** 「更多 → 校准数据」：同一张卡放在底部面板里。 */
export function showCalibSheet(host: HTMLElement, api: AppApi): void {
  host.querySelector('.scrim.calib-scrim')?.remove();
  host.querySelector('.ms.calib-sheet')?.remove();
  const scrim = el('div', 'scrim calib-scrim');
  const sheet = el('div', 'ms calib-sheet');
  sheet.dataset.state = 'calibration';
  const row = el('div', 'hd');
  row.append(el('h2', '', '校准数据'));
  const done = button('nb', '完成');
  const close = () => {
    scrim.remove();
    sheet.remove();
  };
  done.addEventListener('click', close);
  scrim.addEventListener('click', close);
  row.append(done);
  sheet.append(el('div', 'grab'), row, calibCard(api));
  host.append(scrim, sheet);
}
