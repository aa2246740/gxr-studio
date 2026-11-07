// 图库里的粘贴调整：把复制的调整粘贴到选中的照片。分批发给服务器，边粘贴边显示进度；
// 粘贴完刷新缩略图，并给一个「撤销」：把这一批每张照片还原成粘贴之前，粘贴之后又改过的照片不动，并说明。

import { state, emit } from './state.js';
import { h, button, toast, toastError, setFraction } from './dom.js';
import { flushPhotos } from './saves.js';
import { clipboard, copyKeys, pasteKeys, pasteMessage, postPaste, postUndo, undoMessage, newBatchId, groupName } from './paste_core.js';

export const CHUNK = 10; // 每次请求粘贴几张：进度按这个走，一次请求也不会拖太久
let running = false;

// 选择栏上的按钮。没有复制过时点它会说怎么复制
export function pasteSelectionButton(n) {
  const clip = clipboard();
  const title = clip ? `粘贴 ${clip.from.name} 的${clip.groups.map(groupName).join('、')}（${pasteKeys()}）` : `先在编辑器里复制一张照片的调整（${copyKeys()}）`;
  const b = button(`粘贴调整到 ${n} 张`, { ico: 'clipboard-paste', title, onclick: pasteToSelection });
  if (!clip) b.setAttribute('aria-disabled', 'true');
  return b;
}

function progressToast() {
  const text = h('span', {}, '');
  const bar = h('div', { class: 'progress' }, h('i'));
  const close = toast(h('div', {}, text, bar), 'info', { timeout: 0 });
  return {
    set(done, total) {
      text.textContent = `正在粘贴调整… ${done} / ${total}`;
      setFraction(bar, total ? done / total : 0);
    },
    close,
  };
}

async function undoBatch(batchId) {
  try {
    const res = await postUndo(batchId);
    toast(undoMessage(res), res.counts.restored ? 'ok' : 'info');
  } catch (e) {
    toastError(e, '无法撤销。请重试。');
  }
  emit('library-changed');
}

// 粘贴到选中的照片。返回是否处理了这个请求（快捷键用）
export async function pasteToSelection() {
  const clip = clipboard();
  if (!clip) {
    toast(`先在编辑器里复制一张照片的调整：按 ${copyKeys()}，或点「复制调整」。`, 'info');
    return true;
  }
  if (running) return true;
  const ids = [...state.selection].filter((id) => id !== clip.from.id);
  if (!ids.length) {
    toast(state.selection.size ? '选中的只有复制来源这一张。请再选几张要粘贴的照片。' : '先选择要粘贴的照片：点照片左上角的圈，或按住 ⇧ 连续选择。', 'info');
    return true;
  }
  running = true;
  const progress = progressToast();
  progress.set(0, ids.length);
  const batchId = newBatchId();
  const results = [];
  try {
    await flushPhotos(ids); // 刚改过的照片先等它们存完，免得粘贴盖在没写完的修改上
    for (let i = 0; i < ids.length; i += CHUNK) {
      const part = ids.slice(i, i + CHUNK);
      const res = await postPaste({ source: clip.source, groups: clip.groups, targets: part.map((id) => ({ id })), batchId });
      results.push(...res.results);
      progress.set(results.length, ids.length);
    }
  } catch (e) {
    progress.close();
    running = false;
    emit('library-changed');
    const done = results.filter((r) => r.applied.length).length;
    toastError(e, '粘贴没有完成。', done ? { action: { label: '撤销已粘贴的', run: () => undoBatch(batchId) } } : {});
    return true;
  }
  progress.close();
  running = false;
  emit('library-changed'); // 重新取照片列表：缩略图地址带着新的版本号
  const used = results.some((r) => r.applied.length);
  toast(pasteMessage(results), used ? 'ok' : 'info', used ? { action: { label: '撤销', run: () => undoBatch(batchId) }, timeout: 15000 } : {});
  return true;
}
