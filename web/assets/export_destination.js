import { api } from './api.js';
import { h, errorBody } from './dom.js';
import { busyButton } from './busy.js';

const DESTINATION_KEY = 'gxr-studio.export-destination';

function lastDestination(fallback) {
  try { return localStorage.getItem(DESTINATION_KEY) || fallback; } catch { return fallback; }
}

export function exportDestination(fallback) {
  const path = h('p', { class: 'small num export-destination-path' });
  const status = h('p', { class: 'small dim', role: 'status' });
  const input = h('input', {
    type: 'text', value: lastDestination(fallback), class: 'input wide-input',
    'aria-label': '导出到', spellcheck: 'false', oninput: paint,
  });
  function paint() {
    path.textContent = input.value;
    input.title = input.value;
    status.replaceChildren();
    if (input.value.trim()) input.removeAttribute('aria-invalid');
  }
  paint();
  const choose = h('button', {
    class: 'btn sm', type: 'button',
    onclick: () => busyButton(choose, async () => {
      status.replaceChildren();
      input.readOnly = true;
      try {
        const selected = await api('/api/export/destination', { method: 'POST', body: { dest_dir: input.value.trim() } });
        if (!selected.cancelled && selected.path) {
          input.value = selected.path;
          paint();
        }
      } finally {
        input.readOnly = false;
      }
    }, {
      busyText: '正在选择文件夹…',
      onError: (error) => {
        status.replaceChildren(errorBody(error, '无法打开文件夹选择器。请粘贴文件夹的完整路径。'));
        input.focus();
        return false;
      },
    }),
  }, h('span', {}, '选择文件夹…'));
  return {
    el: h('div', { class: 'export-destination' }, choose, input, path, status),
    input,
    get picking() { return choose.dataset.busy === '1'; },
    remember(value) {
      try { localStorage.setItem(DESTINATION_KEY, value); } catch { /* 隐私模式下仍能导出 */ }
    },
  };
}
