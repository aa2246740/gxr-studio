import { fromArray } from './geom.js';

// 撤销记录：每一步开始前的整套状态（快照），加上这一步改了哪几组（owned）。
// 撤销 / 重做只还原 owned 里的组（preview.js 的 restoreSnapshot），其它字段保持现在的样子：
// 重试补写进来的值不会被撤销别的字段顺带写回旧值（W02、Y01），你自己改过的步骤也原样保留（X02）。

export function cropShape(c) {
  return c ? { preset: c.preset, rect: c.rect, angle_deg: c.angle_deg, orientation: c.orientation, frame: c.frame } : null;
}

// 撤销的单位：一步改了哪几组（frame 和 frameFrom、stamp 和 stampFrom 各算一组）
export const HISTORY_GROUPS = {
  recipe: ['recipe', 'memory'], lut: ['lut'], adjust: ['adj'], curve: ['curve'], geometry: ['geometry'],
  frame: ['frame', 'frameFrom'], stamp: ['stamp', 'stampFrom'], crop: ['crop'],
};

export function ownedGroups(before, after) {
  const a = JSON.parse(before);
  const b = JSON.parse(after);
  const owned = Object.keys(HISTORY_GROUPS).filter((g) => HISTORY_GROUPS[g].some((k) => JSON.stringify(a[k] ?? null) !== JSON.stringify(b[k] ?? null)));
  // 几何变了，服务器写几何时会顺带把框收进新画面（透视还换画布）：这一步也管框，撤销才会把原来的框写回去
  if (owned.includes('geometry') && a.crop && !owned.includes('crop')) owned.push('crop');
  return owned;
}

// 撤销 / 重做时界面上的框当场回到这一步记下的样子（框、比例、横竖、画布宽高），不等保存的回应：
// 「W × H」读数用的是框和画布两样，只回一样会错到回应回来为止。横竖变了（转 90°），预览画布的宽高也对调。
export function restoreCropLocal(view, crop) {
  const cur = view.crop;
  if (!crop || !cur) return;
  if (crop.frame && cur.frame) {
    if (view.canvas && (crop.frame.width >= crop.frame.height) !== (cur.frame.width >= cur.frame.height)) view.canvas = { w: view.canvas.h, h: view.canvas.w };
    cur.frame = crop.frame;
  }
  cur.rect = { ...crop.rect };
  cur.preset = crop.preset;
  if (crop.orientation) cur.orientation = crop.orientation;
}

export function undoAvailable(view) {
  return !!(view.undo && view.undo.length);
}

// 撤销 / 重做按钮：没有可以撤销（重做）的内容时置灰（COPY 3.10）
export function syncUndo(view) {
  const set = (btn, ok) => {
    if (!btn) return;
    if (ok) btn.removeAttribute('aria-disabled');
    else btn.setAttribute('aria-disabled', 'true');
  };
  set(view.undoBtn, undoAvailable(view));
  set(view.redoBtn, !!(view.redo && view.redo.length));
}

export function snapshot(view) {
  const ed = view.ed;
  const c = view.crop;
  return JSON.stringify({
    recipe: ed.recipe,
    memory: ed.memory,
    lut: ed.lut,
    adj: ed.adj,
    curve: ed.curve,
    geometry: ed.v2 ? ed.geometry : null,
    frame: ed.frame,
    stamp: ed.stamp,
    frameFrom: ed.frameFrom,
    stampFrom: ed.stampFrom,
    crop: cropShape(c),
  });
}

// 撤销时存进重做（或反过来）的那一份：现在的样子，管的还是同一组字段
export function withOwned(current, entry) {
  const owned = JSON.parse(entry).owned;
  return owned ? JSON.stringify({ ...JSON.parse(current), owned }) : current;
}

// 撤销落在「重读裁剪」还没回来的时候：重做记下的框是重读开始之前的（服务器写几何时收小之前的），重做写进去会被拒。
// 重读因为撤销的写入被丢掉重读时，丢掉的那份正是收小的框：把它换进「几何那一步」的重做记录
// （不一定还在最上面，后面又撤销了几步也一样）。pre 是重读开始时界面上的框；记录里的框还等于它才换，
// 用户之后改过的、别的步骤的记录都不动。
export function staleCropForRedo(view, stale, pre) {
  if (!view.redo || !stale || !stale.rect || !pre) return;
  const fitted = JSON.stringify(fromArray(stale.rect));
  for (let i = view.redo.length - 1; i >= 0; i -= 1) {
    const entry = JSON.parse(view.redo[i]);
    if (!(entry.owned || []).includes('geometry') || !entry.crop) continue;
    const rect = JSON.stringify(entry.crop.rect);
    if (rect !== pre || rect === fitted) continue;
    entry.crop = { ...entry.crop, rect: fromArray(stale.rect), frame: stale.frame };
    view.redo[i] = JSON.stringify(entry);
    return;
  }
}
