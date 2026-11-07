// 编辑器里的复制 / 粘贴调整：顶栏按钮、复制选项（勾选哪几组）、粘贴到当前这张。
// 粘贴到当前这张走这张照片的保存队列（saves.js 的 enqueue）：排在已经排着的写入后面，带队列里最新的 rev，
// 不会和正在写的保存抢。服务器的回应一次性装进界面，作为撤销记录里的一步。

import { on } from './state.js';
import { h, button, openModal, toast, toastError } from './dom.js';
import { saveRecord, enqueue, saveStatus } from './saves.js';
import { applyRetried, endChange, undoChange } from './preview.js';
import { snapshot } from './undo_entries.js';
import {
  PASTE_GROUPS, chosenGroups, rememberGroups, groupsPresent, copyFromView, copyMessage, clipboard,
  copyKeys, pasteKeys, pasteMessage, postPaste, newBatchId, groupName,
} from './paste_core.js';

// 服务器写了哪些组 → 界面上对应的编辑字段（preview.js 的 applyRetried 认的名字）
const EDIT_FIELDS = { recipe: ['recipe', 'lut'], adjust: ['adjust', 'curve'], geometry: ['geometry'], frame: ['frame'], stamp: ['stamp'] };

// 这张照片有写不进去的修改（失败记录还在）：再粘贴一次，之后「重试」会把那份旧内容写回来盖掉粘贴
const UNSAVED_TEXT = '这张照片有修改没有保存。先点「重试」或「放弃修改」，再粘贴。';

const buttons = new Set(); // 顶栏上的粘贴按钮（可能不止一个视图留下过，断开的在刷新时清掉）

function currentIsShowing(view) {
  const m = /^#\/photo\/([^/?]+)/.exec(location.hash);
  return !!m && decodeURIComponent(m[1]) === view.photo.id && !view.disposed && !!view.ed;
}

export function copyEdits(view) {
  if (!view || !currentIsShowing(view)) return false;
  const result = copyFromView(view);
  toast(copyMessage(result), result.copied.length ? 'ok' : 'info', result.copied.length ? { action: { label: '更改选项', run: () => openCopyOptions(view) } } : {});
  return true;
}

// 复制选项：勾选要复制哪几组（记在本机，下次还是这样）。这张照片里没有设置的组标出来
export function openCopyOptions(view) {
  if (!view || !view.ed) return;
  const present = new Set(groupsPresent(view));
  const chosen = new Set(chosenGroups());
  const boxes = PASTE_GROUPS.map(([id, name, hint]) => {
    const box = h('input', { type: 'checkbox', checked: chosen.has(id), dataset: { group: id } });
    const row = h('label', { class: 'paste-opt' }, box,
      h('span', {}, h('b', {}, name), h('small', { class: 'dim' }, present.has(id) ? hint : `${hint}（这张没有设置）`)));
    return { id, box, row };
  });
  const copy = () => {
    const ids = boxes.filter((b) => b.box.checked).map((b) => b.id);
    rememberGroups(ids);
    close();
    if (!ids.length) toast('没有勾选任何项目，没有复制。', 'info');
    else copyEdits(view);
  };
  const body = h('div', { class: 'paste-opts' }, h('p', { class: 'small dim' }, '粘贴时只改勾选的项目，别的保持不变。'), ...boxes.map((b) => b.row));
  const foot = [button('取消', { kind: 'ghost', onclick: () => close() }), button('复制', { kind: 'primary', onclick: copy })];
  const close = openModal('复制调整', body, { foot, initialFocus: boxes[0].box });
}

// 粘贴到当前这张：排进保存队列，成功后一次性装进界面，撤销一步还原
export async function pasteEdits(view) {
  if (!view || !currentIsShowing(view)) return false;
  const clip = clipboard();
  if (!clip) {
    toast(`先复制一张照片的调整：在编辑器里按 ${copyKeys()}，或点「复制调整」。`, 'info');
    return true;
  }
  if (clip.from.id === view.photo.id) {
    toast('这张照片就是复制来源，不用粘贴。', 'info');
    return true;
  }
  const id = view.photo.id;
  const rec = saveRecord(id);
  if (saveStatus(id) === 'failed') {
    toast(UNSAVED_TEXT, 'error');
    return true;
  }
  let item = null;
  let failure = null;
  let refused = false;
  const written = await enqueue(rec, async () => {
    if (rec.failed) { // 排在它前面的保存刚失败了：不粘贴
      refused = true;
      throw new Error(UNSAVED_TEXT);
    }
    try {
      const res = await postPaste({ source: clip.source, groups: clip.groups, targets: [{ id, expected_rev: rec.rev }], batchId: newBatchId(), includeState: true });
      [item] = res.results;
      if (item.rev !== undefined) rec.rev = item.rev;
    } catch (e) {
      failure = e;
      throw e;
    }
  });
  if (!written) {
    if (refused) toast(UNSAVED_TEXT, 'error');
    else toastError(failure, '无法粘贴。请重试。');
    return true;
  }
  if (item.status === 'error') {
    toast(item.message, 'error');
    return true;
  }
  if (!currentIsShowing(view) || view.ed === null) {
    toast(`${view.photo.name}：${pasteMessage([item])}`, item.applied.length ? 'ok' : 'info'); // 粘贴过程中已经切走了：只说结果
    return true;
  }
  if (item.applied.length) applyToView(view, item);
  const undo = item.applied.length ? { action: { label: '撤销', run: () => undoChange(view) } } : {};
  toast(pasteMessage([item]), item.applied.length ? 'ok' : 'info', undo);
  return true;
}

// 把服务器写完的内容一次装进界面，记成撤销里的一步。
// 「这一步开始前」的快照要在装进去之前拍，装完再交给 endChange：applyRetried 会把进行中的一步的快照也一起改成新内容
// （那是给重试补写用的），先开始一步再装的话，这一步前后一样，撤销里就没有这一步了。
function applyToView(view, item) {
  const fields = item.applied.flatMap((g) => EDIT_FIELDS[g] || []);
  const before = snapshot(view);
  if (fields.length) applyRetried(view, { kind: 'edit', response: item.edit, fields, rev: item.rev });
  if (item.crop && (item.applied.includes('crop') || item.applied.includes('geometry'))) {
    applyRetried(view, { kind: 'crop', response: item.crop, fields: ['crop'], rev: item.rev }); // 几何会让服务器顺带收框
  }
  if (!view.pending) view.pending = before; // 正在拖滑块时这一步并进去，不抢它的撤销
  endChange(view);
  view.redrawSide?.();
  view.onChange?.();
}

// 顶栏的三个按钮：复制调整、复制选项、粘贴调整
export function editorCopyPaste(view) {
  const copy = button('复制调整', { kind: 'ghost', ico: 'copy', title: `复制这张照片的调整（${copyKeys()}）`, onclick: () => copyEdits(view) });
  const options = button('', { kind: 'icon', ico: 'chevron-down', title: '选择复制哪些内容', onclick: () => openCopyOptions(view) });
  const paste = button('粘贴调整', { kind: 'ghost', ico: 'clipboard-paste', onclick: () => pasteEdits(view) });
  paste.dataset.paste = '1';
  buttons.add(paste);
  syncPasteButtons();
  return [copy, options, paste];
}

function syncPasteButtons() {
  const clip = clipboard();
  for (const b of buttons) {
    if (!b.isConnected && b.dataset.seen) {
      buttons.delete(b);
      continue;
    }
    b.dataset.seen = '1';
    b.title = clip ? `粘贴到这张照片（${pasteKeys()}）。来自 ${clip.from.name}：${clip.groups.map(groupName).join('、')}` : `先复制一张照片的调整（${copyKeys()}）`;
    if (clip) b.removeAttribute('aria-disabled');
    else b.setAttribute('aria-disabled', 'true');
  }
}
on('paste-clipboard', syncPasteButtons);
