export function el<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function button(className: string, text: string): HTMLButtonElement {
  const node = el('button', className, text);
  node.type = 'button';
  return node;
}

const NS = 'http://www.w3.org/2000/svg';

export function icon(d: string): SVGSVGElement {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'ico');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(NS, 'path');
  path.setAttribute('d', d);
  svg.append(path);
  return svg;
}

export const ICON = {
  back: 'M15 5l-7 7 7 7',
  help: 'M12 18h.01M9.1 9a3 3 0 1 1 5.8 1c0 2-3 2-3 4',
  lock: 'M8 11V8a4 4 0 0 1 8 0v3M6 11h12v9H6z',
} as const;
