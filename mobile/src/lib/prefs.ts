// 排序方向和分组间隔记在这台手机上。没有 localStorage 时用默认值。

import { isGap, type GapMinutes, type Order } from './group';

const ORDER_KEY = 'gxr.mobile.order';
const GAP_KEY = 'gxr.mobile.gap';

function store(): Storage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export function loadOrder(): Order {
  return store()?.getItem(ORDER_KEY) === 'newest' ? 'newest' : 'oldest';
}

export function saveOrder(order: Order): void {
  store()?.setItem(ORDER_KEY, order);
}

export function loadGap(): GapMinutes {
  const value = Number(store()?.getItem(GAP_KEY));
  return isGap(value) ? value : 3;
}

export function saveGap(gap: GapMinutes): void {
  store()?.setItem(GAP_KEY, String(gap));
}

// 导出的 HDR 开关：记住上次的选择，第一次默认关（和桌面 prefs.export_hdr 一样）。
const HDR_KEY = 'gxr.mobile.export_hdr';

export function loadExportHdr(): boolean {
  return store()?.getItem(HDR_KEY) === '1';
}

export function saveExportHdr(on: boolean): void {
  try {
    store()?.setItem(HDR_KEY, on ? '1' : '0');
  } catch {
    /* 存不下（隐私模式、满了）就只管这一次。 */
  }
}
