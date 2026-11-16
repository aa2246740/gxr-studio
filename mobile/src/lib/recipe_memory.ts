// 换配方时的 memory：照桌面 `look_cards.js` 的 recipeTarget。cleanLevels 用导出请求那一份（桌面 preview.js 的 cleanLevels）。

import { cleanLevels } from './export_request';
import type { Card } from './recipes';

export type Memory = Map<string, Record<string, number>>;

/**
 * 换配方时把切走的那个（没选配方时是原厂 04，档位为空）的档位记进 memory，新配方取回 memory 里上次的档位。
 * 已经是它了就是 same，什么都不改。memory 的键是卡的 id，和桌面的 recipeKey 一一对应（导出时改写）。
 * 输入的 memory 不改，返回一份新的。
 */
export function recipeSwitch(
  memory: ReadonlyMap<string, Record<string, number>>,
  current: { id: string; card: Card | null; levels: Record<string, number> | undefined; chosen: boolean },
  next: Card,
): { same: true } | { same: false; memory: Memory; levels: Record<string, number> } {
  if (current.chosen && current.id === next.id) return { same: true };
  const out: Memory = new Map();
  for (const [key, value] of memory) out.set(key, { ...value });
  out.set(current.id, current.card ? cleanLevels(current.card, current.levels) : {});
  return { same: false, memory: out, levels: cleanLevels(next, out.get(next.id) ?? {}) };
}
