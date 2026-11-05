// 重试保存补写成功后，把这个视图里的撤销记录对齐到补写进去的值（W02）。
// 撤销记录是整份编辑状态的快照（preview.js 的 snapshot），键和 view.ed 上的属性同名，裁剪在 crop 键下。

export function cropShape(c) {
  return c ? { preset: c.preset, rect: c.rect, angle_deg: c.angle_deg, orientation: c.orientation } : null;
}

// 重试补写进去的值和界面上原来显示的不一样：说明界面显示的是过时的值（切走又回来时读到的旧内容），
// 这个视图里的撤销记录（还没结束的一步、撤销、重做）在这个字段上也都是那份过时的值，换成补写进去的值，
// 不然撤销别的字段（比如曲线）会顺带把它写回去（W02）。和过时值不同的记录是你自己改出来的，保持不动。
// 两边一样（界面本来就显示着这个值）就什么都不改，正常的撤销照旧。
export function fixHistory(view, swaps) {
  const changed = Object.entries(swaps)
    .map(([prop, [stale, fresh]]) => [prop, JSON.stringify(stale ?? null), fresh])
    .filter(([, staleJson, fresh]) => staleJson !== JSON.stringify(fresh ?? null));
  if (!changed.length) return;
  const fix = (s) => {
    if (!s) return s;
    const o = JSON.parse(s);
    for (const [prop, staleJson, fresh] of changed) {
      if (prop in o && JSON.stringify(o[prop] ?? null) === staleJson) o[prop] = fresh;
    }
    return JSON.stringify(o);
  };
  view.pending = fix(view.pending);
  view.undo = (view.undo || []).map(fix);
  view.redo = (view.redo || []).map(fix);
}
