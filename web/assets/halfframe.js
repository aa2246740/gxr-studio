// 半格（g250）桌面界面的纯逻辑与小组件：图库折叠、角标、配对选择、「取半格」裁剪、黑缝排除、导出选项。
// 不在模块加载时访问 document，方便 Node 单元测试直接导入。

const FULL_WIDTH = 4288;
const FULL_HEIGHT = 2848;
const HALF_X0_JPG = 1100;
const HALF_X0_RAW = 1099;
const HALF_W = 2088;
const SEAM_X0 = 2088;
const SEAM_X1 = 2200; // 不含，宽 112 像素（2088..2199）
const SIZE_EDGES = { original: null, large: 3000, medium: 2048, small: 1600 };

export const HALFFRAME_MODES = [
  ['diptych', '双联一张'],
  ['singles', '两张单格'],
  ['both', '都要'],
];

export const PAIR_EXPORT_NOTE = '双联和相机拼的一样：第 1 张在左（竖拿时在上），中间黑缝 112 像素。';
export const HALF_CROP_ITEM = { id: 'halfframe', label: '取半格' };

export function isPairFirst(p) {
  return !!(p && p.pair && p.pair.role === 'first');
}

export function isPairSecond(p) {
  return !!(p && p.pair && p.pair.role === 'second');
}

export function isUnpairedHalfFirst(p) {
  return !!(p && p.half_first && !p.pair);
}

export function isUnpairedDiptych(p) {
  return !!(p && p.diptych && !p.pair);
}

export function badgeKind(p) {
  if (isPairSecond(p)) return 'pair';
  if (isUnpairedHalfFirst(p)) return 'half_first';
  if (isUnpairedDiptych(p)) return 'diptych';
  return null;
}

const BADGE_SPECS = {
  pair: { cls: 'badge hf', text: '半格', title: '半格一对' },
  half_first: { cls: 'badge hf lone', text: '半格·未成对', title: '半格第 1 张，未配对' },
  diptych: { cls: 'badge hf lone', text: '双联', title: '未配对的双联照片' },
};

export function halfframeBadge(h, icon, p) {
  const kind = badgeKind(p);
  if (!kind) return null;
  const spec = BADGE_SPECS[kind];
  return h('span', { class: spec.cls, title: spec.title }, icon('columns-2'), spec.text);
}

export function missingPartnerIds(photos, partners = new Map()) {
  const known = new Set();
  for (const p of photos || []) if (p && p.id) known.add(p.id);
  const missing = [];
  const seen = new Set();
  for (const p of photos || []) {
    const pid = p && p.pair && p.pair.partner_id;
    if (!pid || known.has(pid) || partners.has(pid) || seen.has(pid)) continue;
    seen.add(pid);
    missing.push(pid);
  }
  return missing;
}

export function partnerOf(photo, photos = [], partners = new Map()) {
  const pid = photo && photo.pair && photo.pair.partner_id;
  if (!pid) return null;
  if (partners && partners.has(pid)) return partners.get(pid);
  return (photos || []).find((p) => p && p.id === pid) || null;
}

function shotById(photos, partners) {
  const map = new Map();
  if (partners) {
    for (const [k, v] of partners.entries()) if (v) map.set(k, v);
  }
  for (const p of photos || []) if (p && p.id) map.set(p.id, p);
  return map;
}

function matchesDate(p, activeDate) {
  return activeDate == null || p.date === activeDate;
}

// 图库折叠：配对的第 1 张收起，每对只显示第 2 张（按第 2 张的拍摄日期与时间排）。
// 跨页拆开时，如果第 1 张在本页、第 2 张在 partners 里，只有第 2 张也属于当前日期筛选（或全部照片）才补进来。
export function foldPhotos(photos, { partners = new Map(), halfframeOnly = false, activeDate = null } = {}) {
  const list = photos || [];
  const byId = shotById(list, partners);
  const inList = new Set(list.filter((x) => x && x.id).map((x) => x.id));
  const seen = new Set();
  const out = [];
  let promoted = false;
  for (let i = 0; i < list.length; i++) {
    const p = list[i];
    if (!p || !p.id) continue;
    if (isPairFirst(p)) {
      const pid = p.pair.partner_id;
      const p2 = pid ? byId.get(pid) : null;
      if (p2 && matchesDate(p2, activeDate) && !seen.has(p2.id) && !inList.has(p2.id)) {
        seen.add(p2.id);
        out.push({ p: p2, idx: i });
        promoted = true;
      }
      continue;
    }
    if (seen.has(p.id)) continue;
    seen.add(p.id);
    out.push({ p, idx: i });
  }
  if (promoted) {
    out.sort((a, b) => {
      const ta = a.p.captured_at || '';
      const tb = b.p.captured_at || '';
      if (ta !== tb) return ta < tb ? 1 : -1;
      return a.idx - b.idx;
    });
  }
  const items = out.map((x) => x.p);
  return halfframeOnly ? items.filter((p) => isPairSecond(p)) : items;
}

export function pairCount(photos, { partners = new Map(), activeDate = null } = {}) {
  const list = photos || [];
  const byId = shotById(list, partners);
  const seconds = new Set();
  for (const p of list) {
    if (isPairSecond(p) && matchesDate(p, activeDate)) {
      seconds.add(p.id);
    } else if (isPairFirst(p) && p.pair.partner_id) {
      const p2 = byId.get(p.pair.partner_id);
      if (p2 && matchesDate(p2, activeDate)) seconds.add(p2.id);
    }
  }
  return seconds.size;
}

export function pairMemberIds(photo) {
  if (isPairSecond(photo) && photo.pair.partner_id) return [photo.id, photo.pair.partner_id];
  return photo && photo.id ? [photo.id] : [];
}

export function isPhotoSelected(selection, photo) {
  if (!selection || !photo) return false;
  return selection.has(photo.id);
}

export function togglePairSelection(selection, photo, force = undefined) {
  if (!selection || !photo) return selection;
  const ids = pairMemberIds(photo);
  const on = force === undefined ? !isPhotoSelected(selection, photo) : !!force;
  for (const id of ids) {
    if (on) selection.add(id);
    else selection.delete(id);
  }
  return selection;
}

export function expandSelectionIds(ids, photos = [], partners = new Map()) {
  const byId = shotById(photos, partners);
  const out = new Set();
  for (const id of ids || []) {
    const p = byId.get(id);
    if (p) {
      for (const mid of pairMemberIds(p)) out.add(mid);
    } else if (id) {
      out.add(id);
    }
  }
  return out;
}

// 批量粘贴 / 统计项数时，把同一对里的第 1 张和第 2 张合并成 1 项（保留第 2 张）。
export function dedupePairIds(ids, photos = [], partners = new Map()) {
  const byId = shotById(photos, partners);
  const set = new Set(ids || []);
  const out = [];
  const seen = new Set();
  for (const id of ids || []) {
    if (!id || seen.has(id)) continue;
    const p = byId.get(id);
    if (p && isPairFirst(p) && p.pair.partner_id && set.has(p.pair.partner_id)) continue;
    seen.add(id);
    out.push(id);
  }
  return out;
}

export function selectionStats(ids, photos = [], partners = new Map()) {
  const byId = shotById(photos, partners);
  const targets = dedupePairIds(ids, photos, partners);
  let pairs = 0;
  for (const id of targets) {
    if (isPairSecond(byId.get(id))) pairs += 1;
  }
  return { targets, items: targets.length, pairs };
}

// 统一的选区量词：纯半格用「N 对半格」，混合用「N 项」，纯普通用「N 张」。
export function selectionNoun(stats) {
  if (typeof stats === 'number') return `${stats} 张`;
  const items = Number(stats?.items || 0);
  const pairs = Number(stats?.pairs || 0);
  if (pairs > 0 && items === pairs) return `${pairs} 对半格`;
  if (pairs > 0) return `${items} 项`;
  return `${items} 张`;
}

export function isUntouchedPairRecipe(view) {
  return !!(view && isPairSecond(view.photo) && view.ed && (view.ed.recipeFromCamera || !view.ed.recipe));
}

export function isSinglePairView(view) {
  return !!(view && isPairSecond(view.photo) && (view.pairView === 'first' || view.pairView === 'second'));
}

export function shortRecipeLabel(tag) {
  if (!tag) return '未标记';
  if (tag.name) return tag.name;
  const pos = tag.position != null ? String(tag.position).padStart(2, '0') : '';
  return [tag.unit_label || tag.unit, pos].filter(Boolean).join(' ') || '未标记';
}

export function usesRawRenderBase(photo, ed = null) {
  if (!photo || !photo.has_raw) return false;
  if (ed && ed.label === 'camera-jpg') return false;
  if (ed && (ed.label === 'prediction' || ed.label === 'neutral')) return true;
  if (photo.has_jpg === false) return true;
  return !!(ed && ed.recipe);
}

export function halfFirstCropRect(w, h, { raw = false, orientation = 1 } = {}) {
  const x0Sensor = raw ? HALF_X0_RAW : HALF_X0_JPG;
  const ori = Number(orientation || 1);
  const x0 = ori === 3 || ori === 8 ? FULL_WIDTH - x0Sensor - HALF_W : x0Sensor;
  const ratioX = x0 / FULL_WIDTH;
  const ratioW = HALF_W / FULL_WIDTH;
  if ((h || 0) > (w || 0)) return { x: 0, y: ratioX, w: 1, h: ratioW };
  return { x: ratioX, y: 0, w: ratioW, h: 1 };
}

export function isHalfFirstCropRect(rect, w, h, opts = {}) {
  if (!rect) return false;
  const exp = halfFirstCropRect(w, h, opts);
  const eps = 0.004;
  return (
    Math.abs(rect.x - exp.x) <= eps
    && Math.abs(rect.y - exp.y) <= eps
    && Math.abs(rect.w - exp.w) <= eps
    && Math.abs(rect.h - exp.h) <= eps
  );
}

// 计算双联黑缝在当前位图上的像素排除矩形 [x0, y0, x1, y1]（不含 x1/y1）。
// 单张视图或非双联图返回 null；裁剪过时换算到裁剪后的坐标。
export function seamExclusionRect(photo, width, height, cropRect = null, pairView = 'diptych') {
  if (!photo || !photo.diptych || pairView === 'first' || pairView === 'second' || !width || !height) return null;
  const s0 = SEAM_X0 / FULL_WIDTH;
  const s1 = SEAM_X1 / FULL_WIDTH;
  const portrait = (photo.height || height) > (photo.width || width);
  const pad = 1;
  if (!portrait) {
    let a = s0;
    let b = s1;
    if (cropRect && cropRect.w > 0) {
      a = (a - cropRect.x) / cropRect.w;
      b = (b - cropRect.x) / cropRect.w;
    }
    if (b <= 0 || a >= 1) return null;
    const x0 = Math.max(0, Math.floor(Math.max(0, a) * width) - pad);
    const x1 = Math.min(width, Math.ceil(Math.min(1, b) * width) + pad);
    return x1 > x0 ? [x0, 0, x1, height] : null;
  }
  let a = s0;
  let b = s1;
  if (cropRect && cropRect.h > 0) {
    a = (a - cropRect.y) / cropRect.h;
    b = (b - cropRect.y) / cropRect.h;
  }
  if (b <= 0 || a >= 1) return null;
  const y0 = Math.max(0, Math.floor(Math.max(0, a) * height) - pad);
  const y1 = Math.min(height, Math.ceil(Math.min(1, b) * height) + pad);
  return y1 > y0 ? [0, y0, width, y1] : null;
}

function fitEdge(w, h, edge) {
  if (!edge || Math.max(w, h) <= edge) return [w, h];
  const k = edge / Math.max(w, h);
  return [Math.max(1, Math.round(w * k)), Math.max(1, Math.round(h * k))];
}

export function singleHalfSize(photo, sizeKey = 'original') {
  const portrait = (photo && photo.height && photo.width) ? photo.height > photo.width : false;
  const base = portrait ? [FULL_HEIGHT, HALF_W] : [HALF_W, FULL_HEIGHT];
  return fitEdge(base[0], base[1], SIZE_EDGES[sizeKey] || null);
}

export function pairExportSizeText(photo, diptychSize, sizeKey, mode) {
  const dip = diptychSize || (photo && photo.width && photo.height ? fitEdge(photo.width, photo.height, SIZE_EDGES[sizeKey] || null) : fitEdge(FULL_WIDTH, FULL_HEIGHT, SIZE_EDGES[sizeKey] || null));
  const single = singleHalfSize(photo, sizeKey);
  if (mode === 'singles') return `${single[0]} × ${single[1]} 像素 · 2 个文件`;
  if (mode === 'both') return `${dip[0]} × ${dip[1]} + ${single[0]} × ${single[1]} 像素 · 3 个文件`;
  return `${dip[0]} × ${dip[1]} 像素 · 1 个文件`;
}

export function halfframeBatchModeWord(mode) {
  if (mode === 'singles') return '按两张单格导出';
  if (mode === 'both') return '按双联和单格导出';
  return '按双联导出';
}

export function batchExportHeadline(progress, photos = [], halfframe = 'diptych') {
  const results = Array.isArray(progress?.results) ? progress.results : [];
  const ok = results.filter((r) => !r.error);
  if (!ok.length) return '已导出 0 张';
  const fileTotal = ok.reduce((n, r) => n + (Array.isArray(r.files) && r.files.length ? r.files.length : 1), 0);
  const declaredPairs = Number(progress?.pairs || 0);
  if (declaredPairs <= 0) return `已导出 ${ok.length} 张`;
  const byId = shotById(photos, null);
  let okPairs = 0;
  let knownOk = 0;
  for (const r of ok) {
    if (Array.isArray(r.files) && r.files.length > 1) {
      okPairs += 1;
      knownOk += 1;
      continue;
    }
    const p = r.id ? byId.get(r.id) : null;
    if (p) {
      knownOk += 1;
      if (isPairSecond(p)) okPairs += 1;
    }
  }
  let badPairs = 0;
  for (const r of results) {
    if (!r.error) continue;
    const p = r.id ? byId.get(r.id) : null;
    if (p && isPairSecond(p)) badPairs += 1;
  }
  const exportedPairs = knownOk === ok.length
    ? okPairs
    : Math.max(0, Math.min(declaredPairs - badPairs, ok.length));
  return exportedPairs > 0
    ? `导出 ${fileTotal} 张，其中 ${exportedPairs} 对半格${halfframeBatchModeWord(halfframe)}`
    : `已导出 ${ok.length} 张`;
}

// 增量同步按日期分组的网格：保留已有且仍在可见列表里的 .cell 节点（不重新解码缩略图），
// 只移除被折进半格对的第 1 张并按顺序插入新增节点。
export function patchDayGrid(wrap, vis, { h, createCell, syncCell, fmtDate, countOn, unit }) {
  const groups = [];
  const byDate = new Map();
  const desiredIds = new Set();
  for (const p of vis) {
    desiredIds.add(p.id);
    const dateKey = String(p.date);
    let g = byDate.get(dateKey);
    if (!g) {
      g = { dateKey, date: p.date, items: [] };
      byDate.set(dateKey, g);
      groups.push(g);
    }
    g.items.push(p);
  }
  const existingGroups = new Map();
  const existingCells = new Map();
  for (const child of [...wrap.children]) {
    if (!child.classList.contains('day') || !byDate.has(child.dataset.date)) {
      child.remove();
      continue;
    }
    existingGroups.set(child.dataset.date, child);
    const cellsEl = child.lastElementChild;
    if (!cellsEl) continue;
    for (const el of [...cellsEl.children]) {
      const id = el.dataset?.id;
      if (!id || !desiredIds.has(id)) el.remove();
      else existingCells.set(id, el);
    }
  }
  for (let gi = 0; gi < groups.length; gi += 1) {
    const g = groups[gi];
    let group = existingGroups.get(g.dateKey);
    const countLabel = `${countOn(g.date)} ${unit}`;
    if (!group) {
      const hid = `day-${g.date || 'unknown'}`;
      group = h('div', { class: 'day', role: 'group', 'aria-labelledby': hid },
        h('h3', { class: 'num', id: hid }, g.date ? fmtDate(g.date) : '未知日期', h('span', {}, countLabel)),
        h('div', { class: 'cells' }));
      group.dataset.date = g.dateKey;
    } else {
      const span = group.querySelector('h3 > span');
      if (span && span.textContent !== countLabel) span.textContent = countLabel;
    }
    if (wrap.children[gi] !== group) wrap.insertBefore(group, wrap.children[gi] || null);
    const cellsEl = group.lastElementChild;
    for (let ci = 0; ci < g.items.length; ci += 1) {
      const p = g.items[ci];
      let el = existingCells.get(p.id);
      if (el) syncCell?.(el, p);
      else el = createCell(p);
      if (cellsEl.children[ci] !== el) cellsEl.insertBefore(el, cellsEl.children[ci] || null);
    }
  }
}
