// 复制 / 粘贴调整：剪贴板、要复制哪几组、请求服务器，以及把结果说成白话。编辑器和图库共用。
// 剪贴板只在这一页的内存里（复制时的样子，之后原来那张再改也不影响）；勾选的组记在本机浏览器里。
// 服务器逐张判断能不能用（`POST /api/edits/paste`）：能用的用，不能用的跳过并说明原因。

import { api } from './api.js';
import { emit } from './state.js';
import { modKey } from './dom.js';
import { adjustBody, curveBody, recipeBody } from './preview.js';
import { frameBody } from './finish.js';
import { toArray } from './geom.js';

// [id, 名称, 说明]。前两组默认勾上；几何不含旋转和翻转（那是照片自己的方向）
export const PASTE_GROUPS = [
  ['recipe', '配方 / 风格', '配方、LUT'],
  ['adjust', '调整', '曝光、色温、对比度、曲线等'],
  ['geometry', '几何', '拉直、透视、畸变（不含旋转和翻转）'],
  ['crop', '裁剪', '比例和裁剪框'],
  ['frame', '边框', '边框样式、成品比例'],
  ['stamp', '时间戳', '日期或文字戳'],
];
const DEFAULT_GROUPS = ['recipe', 'adjust'];
const GROUPS_KEY = 'gxr-studio.paste-groups';
export const groupName = (id) => (PASTE_GROUPS.find((g) => g[0] === id) || [id, id])[1];

export function chosenGroups() {
  try {
    const saved = JSON.parse(localStorage.getItem(GROUPS_KEY));
    if (Array.isArray(saved)) return PASTE_GROUPS.map((g) => g[0]).filter((id) => saved.includes(id));
  } catch { /* 没存过或读不出来：用默认 */ }
  return [...DEFAULT_GROUPS];
}

export function rememberGroups(ids) {
  try { localStorage.setItem(GROUPS_KEY, JSON.stringify(ids)); } catch { /* 隐私模式写不进去就算了 */ }
}

export const copyKeys = () => (modKey() === '⌘' ? '⇧⌘C' : 'Ctrl+Shift+C');
export const pasteKeys = () => (modKey() === '⌘' ? '⇧⌘V' : 'Ctrl+Shift+V');

// ---- 剪贴板 -------------------------------------------------------------------

let clip = null; // { source, groups, from: { id, name } }

export function clipboard() {
  return clip;
}

export function setClipboard(next) {
  clip = next;
  emit('paste-clipboard', clip);
}

// 裁剪算「设置过」：比例不是「原图」，或者框没有占满整张。3:2 的机身文件上选 3:2，框几乎占满，但比例预设仍是要复制的内容
const cropIsSet = (c) => c.preset !== 'original' || c.rect.w < 0.999 || c.rect.h < 0.999 || c.rect.x > 0.001 || c.rect.y > 0.001;

// 这张照片在编辑器里现在的样子，按组装成服务器要的来源。没有设置过的组返回 null（不复制：粘贴一个空组会把目标的清掉）
function groupSource(view, id) {
  const { ed, crop } = view;
  switch (id) {
    case 'recipe':
      return ed.recipe || ed.lut ? { recipe: recipeBody(view, { memory: true }), lut: ed.lut || null } : null;
    case 'adjust':
      return Object.values(ed.adj).some((x) => Number(x) !== 0) || ed.curve ? { adjust: adjustBody(ed), curve: curveBody(ed) } : null;
    case 'geometry': {
      if (!ed.v2 || !ed.geometry) return null;
      const { straighten = 0, vertical = 0, horizontal = 0, distortion = 0 } = ed.geometry;
      return straighten || vertical || horizontal || distortion ? { geometry: { straighten, vertical, horizontal, distortion } } : null;
    }
    case 'crop':
      return crop && crop.rect && cropIsSet(crop) ? { crop: { preset: crop.preset, rect: toArray(crop.rect) } } : null;
    case 'frame':
      return ed.v2 && ed.frameFrom === 'photo' && ed.frame.style !== 'none' ? { frame: frameBody(ed.frame) } : null;
    case 'stamp':
      return ed.v2 && ed.stampFrom === 'photo' && ed.stamp.enabled ? { stamp: ed.stamp } : null;
    default:
      return null;
  }
}

// 这张照片里每一组有没有东西可以复制（复制选项里据此标出「这张没有设置」）
export function groupsPresent(view) {
  return PASTE_GROUPS.map((g) => g[0]).filter((id) => groupSource(view, id));
}

// 复制：把勾选的、这张照片里确实有设置的组存进剪贴板。返回 { copied, empty }（没有东西可复制时 copied 为空，剪贴板不变）
export function copyFromView(view, groups = chosenGroups()) {
  const source = {};
  const copied = [];
  const empty = [];
  for (const id of groups) {
    const part = groupSource(view, id);
    if (part) {
      Object.assign(source, part);
      copied.push(id);
    } else empty.push(id);
  }
  if (copied.length) setClipboard({ source, groups: copied, from: { id: view.photo.id, name: view.photo.name } });
  return { copied, empty };
}

export function copyMessage({ copied, empty }) {
  if (!copied.length) return '这张照片在勾选的项目里没有修改，没有复制。';
  const names = copied.map(groupName).join('、');
  return empty.length ? `已复制：${names}。${empty.map(groupName).join('、')}这张没有设置，没有复制。` : `已复制：${names}`;
}

// ---- 请求 -----------------------------------------------------------------------

export const newBatchId = () => `p${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;

export function postPaste({ source, groups, targets, batchId, includeState = false }) {
  return api('/api/edits/paste', {
    method: 'POST',
    body: { source, groups, targets, batch_id: batchId, include_state: includeState },
  });
}

export function postUndo(batchId) {
  return api('/api/edits/paste/undo', { method: 'POST', body: { batch_id: batchId } });
}

// ---- 把结果说成白话 ---------------------------------------------------------------

// 跳过的原因 → 一句话。who 是「2 张」或（只有一张时）「这张」；part 是配方组里被跳过的那一半（配方或 LUT）
const SKIP_LINES = {
  unit_mismatch: (who) => `${who}镜头单元不同，没有套用配方`,
  needs_raw: (who, s) => `${who}只有 JPG，没有套用${s.part === 'lut' ? ' LUT' : '配方'}`,
  lut_missing: (who) => `${who}找不到这个 LUT，没有套用 LUT`,
  recipe_rejected: (who) => `${who}用不了这个配方，没有套用配方`,
  not_copied: (who, s) => `${who}没有套用${groupName(s.group)}（复制时没带上）`,
};
const ERROR_LINES = {
  rev_conflict: (who) => `${who}在别处改过，没有粘贴`,
  photo_not_found: (who) => `${who}找不到，没有粘贴`,
  sidecar_too_new: (who) => `${who}的编辑来自更新的版本，没有粘贴`,
  sidecar_unreadable: (who) => `${who}的编辑记录读不了，没有粘贴`,
};

// 结果说明：已套用到几张，再逐类说明没套用的原因。单张照片时说得更直接。
export function pasteMessage(results) {
  const used = results.filter((r) => r.applied.length);
  const single = results.length === 1;
  const reasons = new Map(); // 同一类原因合并计数
  const note = (key, line, info) => {
    const hit = reasons.get(key) || { line, info, n: 0 };
    hit.n += 1;
    reasons.set(key, hit);
  };
  for (const r of results) {
    for (const s of r.skipped) {
      note(`${s.reason}|${s.part || ''}|${s.group}`, SKIP_LINES[s.reason] || ((who) => `${who}没有套用${groupName(s.group)}：${s.message}`), s);
    }
    if (r.status === 'error') note(`error|${r.code}`, ERROR_LINES[r.code] || ((who) => `${who}没有粘贴`), r);
  }
  const head = single
    ? (used.length ? `已套用：${used[0].applied.map(groupName).join('、')}` : '没有套用')
    : (used.length ? `已套用到 ${used.length} 张` : '没有套用到任何一张');
  const tail = [...reasons.values()].map(({ line, info, n }) => line(single ? '这张' : `${n} 张`, info));
  return [head, ...tail].join('；');
}

export function undoMessage(res) {
  const { restored = 0, changed = 0, error = 0 } = res.counts || {};
  const parts = [restored ? `已撤销 ${restored} 张` : '没有撤销任何一张'];
  if (changed) parts.push(`${changed} 张粘贴之后又改过，没有撤销`);
  if (error) parts.push(`${error} 张没有撤销成功`);
  return parts.join('；');
}
