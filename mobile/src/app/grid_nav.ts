// 网格的键盘和鼠标：方向键在格子之间走、Shift 点选一段。只按网格自己排好的行算（列数来自版式），不碰 DOM。

import type { VRow } from '../lib/group';

function cellRows(rows: VRow[]): VRow[] {
  return rows.filter((row) => row.kind === 'cells' && row.ids.length > 0);
}

/** 网格上从左到右、从上到下的顺序（跨组）。 */
export function gridOrder(rows: VRow[]): string[] {
  return cellRows(rows).flatMap((row) => row.ids);
}

/** 上次点的那张到这一张（含两头），按网格顺序；有一头不在网格里就是 null。 */
export function rangeBetween(order: string[], from: string, to: string): string[] | null {
  const a = order.indexOf(from);
  const b = order.indexOf(to);
  if (a < 0 || b < 0) return null;
  return order.slice(Math.min(a, b), Math.max(a, b) + 1);
}

/** 方向键、Home、End 落到哪一张；不是这几个键就是 null，走到头就留在原地。上下按同一列走，下一行短了就落在它的最后一张。 */
export function arrowTarget(rows: VRow[], id: string, key: string): string | null {
  const lines = cellRows(rows);
  const order = lines.flatMap((row) => row.ids);
  const at = order.indexOf(id);
  if (at < 0) return null;
  if (key === 'ArrowLeft') return order[Math.max(0, at - 1)];
  if (key === 'ArrowRight') return order[Math.min(order.length - 1, at + 1)];
  if (key === 'Home') return order[0];
  if (key === 'End') return order[order.length - 1];
  if (key !== 'ArrowUp' && key !== 'ArrowDown') return null;
  const line = lines.findIndex((row) => row.ids.includes(id));
  const col = lines[line].ids.indexOf(id);
  const next = lines[line + (key === 'ArrowDown' ? 1 : -1)];
  return next ? next.ids[Math.min(col, next.ids.length - 1)] : id;
}

/** 这一张所在那一行的上沿和高度（网格内容坐标）。 */
export function rowBox(rows: VRow[], id: string): { top: number; height: number } | null {
  let top = 0;
  for (const row of rows) {
    if (row.kind === 'cells' && row.ids.includes(id)) return { top, height: row.height };
    top += row.height;
  }
  return null;
}
