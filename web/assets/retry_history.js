// 重试保存补写成功后，把这个视图里的撤销记录对齐到补写进去的值（W02、X02）。
// 撤销记录是整份编辑状态的快照（preview.js 的 snapshot），键和 view.ed 上的属性同名（memory 不在快照里），裁剪在 crop 键下。

export function cropShape(c) {
  return c ? { preset: c.preset, rect: c.rect, angle_deg: c.angle_deg, orientation: c.orientation } : null;
}

const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

// group：一个字段对应的属性 → [界面上原来的值, 补写进去的值]，例如 { frame: [...], frameFrom: [...] }。
// 只有这个字段在这个视图的撤销记录里（撤销、还没结束的一步、重做）从来没变过，才说明那是切回来时读到的旧值、
// 不是你改出来的：这时把这些记录里的它换成补写进去的值，撤销别的字段就不会顺带把旧值写回去（W02）。
// 只要有一条记录的值不一样，这个字段就是你在这里改过的，记录保持原样（X02：值碰巧相同也不算旧值）。
// 没有正在进行的一步时，界面上的值也要和记录一致；有的话界面上是拖动中的草稿，不算。
export function fixHistory(view, group) {
  const parsed = [...(view.undo || []), view.pending, ...(view.redo || [])].filter(Boolean).map((s) => JSON.parse(s));
  if (!parsed.length) return;
  const props = Object.keys(group).filter((prop) => prop in parsed[0]);
  if (!props.length) return;
  for (const prop of props) {
    const [shown, fresh] = group[prop];
    const base = parsed[0][prop];
    if (!parsed.every((o) => same(o[prop], base))) return;
    if (!view.pending && !same(shown, base)) return;
    if (same(base, fresh)) group[prop] = null;
  }
  const todo = props.filter((prop) => group[prop]);
  if (!todo.length) return;
  const fix = (s) => {
    if (!s) return s;
    const o = JSON.parse(s);
    for (const prop of todo) o[prop] = group[prop][1];
    return JSON.stringify(o);
  };
  view.pending = fix(view.pending);
  view.undo = (view.undo || []).map(fix);
  view.redo = (view.redo || []).map(fix);
}
