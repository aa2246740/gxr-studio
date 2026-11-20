// 滑块 / 旋转尺 / 配方条拖动结束后的 250 ms 误触保护，防止刚松手时误点顶槽里的导出条按钮。

export const DRAG_GUARD_MS = 250;

let lastControlDragEnd = -Infinity;

export function markControlDragEnd(now = performance.now()): void {
  lastControlDragEnd = now;
}

export function isDragTapGuarded(now = performance.now()): boolean {
  return now - lastControlDragEnd < DRAG_GUARD_MS;
}

export function resetDragGuard(): void {
  lastControlDragEnd = -Infinity;
}
