// 轻提示条：用于丢失标志回退、自动反色、删除确认和 CJK 之外的瞬时提示。

import { el } from './dom';

let timer = 0;

export function showToast(shell: HTMLElement, message: string, ms = 3200): void {
  shell.querySelectorAll('.ftoast').forEach((node) => node.remove());
  window.clearTimeout(timer);
  const node = el('div', 'ftoast', message);
  node.setAttribute('role', 'status');
  shell.append(node);
  timer = window.setTimeout(() => {
    node.remove();
  }, ms);
}
