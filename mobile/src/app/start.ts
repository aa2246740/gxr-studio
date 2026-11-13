import type { AppApi } from './model';
import { button, el, icon, ICON } from './dom';

function hero(): SVGSVGElement {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 440 180');
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML =
    '<rect x="250" y="16" width="110" height="148" rx="22" fill="#202020" stroke="#6e6e6e" stroke-width="3"/>' +
    '<rect x="258" y="24" width="94" height="132" rx="16" fill="#161616"/>' +
    '<path d="M70 28h52l18 18v78a6 6 0 0 1-6 6H70a6 6 0 0 1-6-6V34a6 6 0 0 1 6-6z" fill="#2b2b2b" stroke="#b3b3b3" stroke-width="3"/>' +
    '<path d="M168 90h48M202 78l14 12-14 12" fill="none" stroke="#6aa6ff" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>' +
    '<text x="96" y="92" text-anchor="middle" fill="#ececec" font-size="16" font-family="-apple-system,sans-serif" font-weight="700">GXR</text>';
  return svg;
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
  const picture = el('div', 'hero');
  picture.append(hero());
  const heading = el('h1', '', '选照片，换配方，存到相册');
  const lead = el('p', 'lead', '用读卡器把存储卡插到 iPhone，再点下面的按钮，在「文件」里选照片。');
  const priv = el('p', 'priv', '照片只在这次使用中保留，不会上传，也不会离开这台 iPhone。');
  priv.prepend(icon(ICON.lock));

  const calib = el('div', 'card calib');
  const hd = el('div', 'hd');
  hd.append(el('b', '', '导入校准数据'), el('span', 'dim', '只需一次'), el('span', 'tag', '未导入'));
  const hdTag = hd.querySelector('.tag');
  if (hdTag) hdTag.setAttribute('style', 'margin-left:auto');
  calib.append(
    hd,
    el('p', '', '还没导入：配方只显示中性转换，和相机里的颜色不同。'),
    el('p', 's', '在 Mac 的 GXR Studio 里导出，用隔空投送发到这台 iPhone。'),
  );
  const choose = button('btn block', '选择校准数据文件');
  choose.disabled = true;
  calib.append(choose);

  const cta = el('div', 'cta');
  const pick = button('btn primary lg block', '从存储卡选照片');
  pick.addEventListener('click', () => api.addPhotos());
  cta.append(pick);
  const help = el('div', 'help');
  const helpBtn = button('lnk', '怎么把卡插到 iPhone');
  helpBtn.prepend(icon(ICON.help));
  helpBtn.addEventListener('click', () => api.showHelp());
  help.append(helpBtn);

  body.append(picture, heading, lead, priv, calib, cta, help);
  screen.append(nav, body);
  host.append(screen);
}

const STEPS: Array<[string, string]> = [
  ['把存储卡放进读卡器', '用带 USB-C 接口的读卡器。卡面朝向读卡器上标的方向。'],
  ['把读卡器插到 iPhone 底部', '插好后不用等，不会有任何弹窗。'],
  ['回到这里，点「从存储卡选照片」', '在「文件」里找到存储卡，打开 DCIM 文件夹，选要用的照片。'],
  ['需要配方时，再选相机配方文件', '文件名是 GXRCE.DAT，在存储卡最外层，和 DCIM 并排。选照片时一起选上也行；没选的话，之后会提示你补选。'],
];

export function renderHelp(host: HTMLElement): HTMLElement {
  const scrim = el('div', 'scrim');
  const sheet = el('div', 'ms');
  const row = el('div', 'hd');
  row.append(el('h2', '', '怎么把卡插到 iPhone'));
  const done = button('nb', '完成');
  const close = () => {
    scrim.remove();
    sheet.remove();
  };
  done.addEventListener('click', close);
  scrim.addEventListener('click', close);
  row.append(done);
  sheet.append(el('div', 'grab'), row);
  STEPS.forEach(([title, detail], index) => {
    const line = el('div', 'lrow');
    line.append(el('span', 'step', String(index + 1)));
    const text = el('div');
    text.append(el('b', '', title), el('small', '', detail));
    line.append(text);
    sheet.append(line);
  });
  sheet.append(
    el(
      'p',
      'faint',
      '拔卡前，先确认照片已经存到相册。除了在胶片柜里点「写入存储卡」，GXR Studio 不会改动存储卡上的文件。',
    ),
  );
  host.append(scrim, sheet);
  return sheet;
}
