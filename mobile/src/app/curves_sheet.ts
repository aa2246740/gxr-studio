// 曲线深层工作表：从「调整 → 曲线 ›」或「更多 → 曲线」打开。
// 挂在 `.screen.editor` 内，保持 `.editor svg.curve` 可达且拖动期间曲线盒子不位移（phone-curve-drag）。

import type { CurvesView } from './curves_view';
import { button, el } from './dom';
import { registerSheet } from './sheet_lifecycle';

export function openCurvesSheet(
  shell: HTMLElement,
  host: HTMLElement,
  curves: CurvesView,
  onClose?: () => void,
): () => void {
  const scrim = el('div', 'scrim curves-scrim');
  const sheet = el('div', 'ms curves-sheet');
  sheet.setAttribute('role', 'dialog');
  sheet.setAttribute('aria-label', '曲线');

  let unregister: () => void = () => undefined;
  let closed = false;
  const panel = curves.panelEl();
  const close = () => {
    if (closed) return;
    closed = true;
    curves.cancelDrag();
    unregister();
    panel.hidden = true;
    scrim.remove();
    sheet.remove();
    onClose?.();
  };
  unregister = registerSheet(shell, close);
  scrim.addEventListener('click', close);

  const hd = el('div', 'sheet-hd');
  hd.append(el('h2', '', '曲线'));
  const done = button('nb bold', '完成');
  done.dataset.act = 'close';
  done.addEventListener('click', close);
  hd.append(done);

  panel.hidden = false;
  curves.refresh();
  sheet.append(hd, panel);
  host.append(scrim, sheet);
  return close;
}
