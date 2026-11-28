import type { AppApi } from './model';
import { camFilesRememberedLabel } from '../lib/camfiles';
import { cardHelpTitle, deviceName, isComputerDevice, saveWord, thisDevice } from '../lib/device';
import { FONT_LICENCE_LABELS, FONT_LICENCES } from '../lib/font_assets';
import { button, el, icon, ICON } from './dom';
import { FOLDER_PICK_LABEL, FOLDER_PICK_NOTE, pickFolder } from './file_drop';
import './folder_pick.css';

// 电脑上不画手机：同样的线条画一台笔记本（屏幕加底座），卡和箭头不变。
const PHONE_SHAPE =
  '<rect x="250" y="16" width="110" height="148" rx="22" fill="#202020" stroke="#6e6e6e" stroke-width="3"/>' +
  '<rect x="258" y="24" width="94" height="132" rx="16" fill="#161616"/>';
const LAPTOP_SHAPE =
  '<rect x="244" y="34" width="152" height="100" rx="10" fill="#202020" stroke="#6e6e6e" stroke-width="3"/>' +
  '<rect x="253" y="43" width="134" height="82" rx="4" fill="#161616"/>' +
  '<path d="M228 140h184l-8 12a6 6 0 0 1-5 3H241a6 6 0 0 1-5-3z" fill="#202020" stroke="#6e6e6e" stroke-width="3" stroke-linejoin="round"/>';

function hero(): SVGSVGElement {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 440 180');
  svg.setAttribute('aria-hidden', 'true');
  if (isComputerDevice()) svg.dataset.device = 'computer';
  svg.innerHTML =
    (isComputerDevice() ? LAPTOP_SHAPE : PHONE_SHAPE) +
    '<path d="M70 28h52l18 18v78a6 6 0 0 1-6 6H70a6 6 0 0 1-6-6V34a6 6 0 0 1 6-6z" fill="#2b2b2b" stroke="#b3b3b3" stroke-width="3"/>' +
    '<path d="M168 90h48M202 78l14 12-14 12" fill="none" stroke="#6aa6ff" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>' +
    '<text x="96" y="92" text-anchor="middle" fill="#ececec" font-size="16" font-family="-apple-system,sans-serif" font-weight="700">GXR</text>';
  return svg;
}

/** 电脑上「选照片」旁边加「选文件夹」，下面一行小字说浏览器的「上传」只是本机读取。手机和 iPad 原样只有「选照片」。 */
function withFolder(pick: HTMLButtonElement, computer: boolean): HTMLElement[] {
  if (!computer) return [pick];
  const row = el('div', 'pick-row');
  const folder = button('btn lg', FOLDER_PICK_LABEL);
  folder.dataset.act = 'pick-folder';
  folder.addEventListener('click', () => pickFolder());
  row.append(pick, folder);
  const note = el('p', 'folder-note', FOLDER_PICK_NOTE);
  note.dataset.state = 'folder-note';
  return [row, note];
}

function paintStartBody(screen: HTMLElement, body: HTMLElement, api: AppApi): void {
  const dev = deviceName();
  const computer = isComputerDevice();
  const book = api.book();
  const dat = book.dat;
  screen.dataset.camfiles = dat ? 'remembered' : 'none';

  body.replaceChildren();
  const picture = el('div', 'hero');
  picture.append(hero());
  const heading = el('h1', '', `选照片，换配方，${saveWord()}`);
  const lead = el(
    'p',
    'lead',
    dat
      ? computer
        ? '把存储卡接到这台电脑，在弹出的窗口里选照片。'
        : `用读卡器把存储卡插到 ${dev}，在「文件」里选照片。`
      : '先选一次相机配方文件（存储卡最外层的 GXRCE.DAT 和 GXRFX.BIN），以后每次只选照片。',
  );
  const priv = el(
    'p',
    'priv',
    dat
      ? `照片只在这次使用中保留，不会上传，也不会离开${thisDevice('。')}`
      : `照片和相机配方文件只留在${thisDevice('，不会上传。')}`,
  );
  priv.prepend(icon(ICON.lock));

  const cta = el('div', 'cta start-cta');
  if (dat) {
    const status = el('p', 'start-status', `相机配方文件${camFilesRememberedLabel(dat.generation)}`);
    status.dataset.state = 'camfiles-remembered';
    const pick = button('btn primary lg block', '从存储卡选照片');
    pick.dataset.act = 'pick-photos';
    pick.addEventListener('click', () => api.addPhotos());
    cta.append(...withFolder(pick, computer));
    body.append(picture, heading, lead, status, priv, cta);
  } else {
    const pickFiles = button('btn primary lg block', book.datBusy ? '正在读取相机配方文件…' : '选相机配方文件');
    pickFiles.dataset.act = 'pick-camfiles';
    pickFiles.disabled = book.datBusy;
    pickFiles.addEventListener('click', () => api.pickDat());
    const pickPhotos = button('btn lg block', '选照片');
    pickPhotos.dataset.act = 'pick-photos';
    pickPhotos.addEventListener('click', () => api.addPhotos());
    cta.append(pickFiles, ...withFolder(pickPhotos, computer));
    body.append(picture, heading, lead, priv);
    if (book.datError) {
      const err = el('p', 'bad small start-err', book.datError);
      err.dataset.state = 'start-dat-error';
      err.setAttribute('role', 'status');
      body.append(err);
    }
    body.append(cta);
  }

  const help = el('div', 'help');
  const helpBtn = button('lnk', cardHelpTitle());
  helpBtn.prepend(icon(ICON.help));
  helpBtn.addEventListener('click', () => api.showHelp());
  help.append(helpBtn);
  body.append(help);
}

export function renderStart(host: HTMLElement, api: AppApi): void {
  host.replaceChildren();
  const screen = el('section', 'screen');
  const nav = el('header', 'nav');
  const title = el('div', 't');
  const brand = el('b', 'wm');
  brand.append(el('span', 'mark'), document.createTextNode('GXR Studio'));
  title.append(brand);
  nav.append(el('span', 'nb'), title, el('span', 'nb'));

  const body = el('div', 'start');
  screen.append(nav, body);
  host.append(screen);
  paintStartBody(screen, body, api);

  const book = api.book();
  const off = book.onChange(() => {
    if (!screen.isConnected) {
      off();
      return;
    }
    paintStartBody(screen, body, api);
  });
}

// 电脑上的三步：插卡、在弹出的窗口里选照片、第一次再选配方文件。没有第二行小字。
const COMPUTER_STEPS: Array<[string, string]> = [
  ['把存储卡插进电脑的 SD 卡槽，或用读卡器接上', ''],
  ['点选照片的按钮（「选照片」或「从存储卡选照片」），在弹出的窗口里打开存储卡的 DCIM 文件夹，选好 RAW 和 JPG（可以 ⌘A / Ctrl+A 全选）', ''],
  ['第一次用时，再选存储卡最外层的 GXRCE.DAT 和 GXRFX.BIN', ''],
];

function helpSteps(dev: string): Array<[string, string]> {
  if (isComputerDevice()) return COMPUTER_STEPS;
  return [
    ['把存储卡放进读卡器', '用带 USB-C 接口的读卡器。卡面朝向读卡器上标的方向。'],
    [`把读卡器插到 ${dev} 底部`, '插好后不用等，不会有任何弹窗。'],
    ['首次使用，先选相机配方文件', '在「文件」里找到存储卡最外层的 GXRCE.DAT 和 GXRFX.BIN（和 DCIM 并排）。选一次就会记住，和照片一起选也行。'],
    ['再选照片', '打开 DCIM 文件夹选要用的照片；以后每次只需选照片。没选配方文件也能先看相机 JPG。'],
  ];
}

export function renderHelp(host: HTMLElement): HTMLElement {
  const dev = deviceName();
  const scrim = el('div', 'scrim');
  const sheet = el('div', 'ms');
  const row = el('div', 'hd');
  row.append(el('h2', '', cardHelpTitle()));
  const done = button('nb', '完成');
  const close = () => {
    scrim.remove();
    sheet.remove();
  };
  done.addEventListener('click', close);
  scrim.addEventListener('click', close);
  row.append(done);
  sheet.append(el('div', 'grab'), row);
  helpSteps(dev).forEach(([title, detail], index) => {
    const line = el('div', 'lrow');
    line.append(el('span', 'step', String(index + 1)));
    const text = el('div');
    text.append(el('b', '', title));
    if (detail) text.append(el('small', '', detail));
    line.append(text);
    sheet.append(line);
  });
  sheet.append(
    el(
      'p',
      'faint',
      `拔卡前，先确认照片已经${isComputerDevice() ? '下载好' : '存到相册'}。除了在胶片柜里点「写入存储卡」，GXR Studio 不会改动存储卡上的文件。`,
    ),
  );
  const licences = el('p', 'faint licence-links');
  licences.append(document.createTextNode('字体许可：'));
  FONT_LICENCE_LABELS.forEach(([key, label], index) => {
    if (index > 0) licences.append(document.createTextNode(' · '));
    const link = document.createElement('a');
    link.href = FONT_LICENCES[key];
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = label;
    licences.append(link);
  });
  sheet.append(licences);
  host.append(scrim, sheet);
  return sheet;
}
