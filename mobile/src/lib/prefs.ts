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

// 导出的 HDR 开关：记住上次的选择，第一次默认开（有 RAW 时生效）；已保存的关继续保留。
const HDR_KEY = 'gxr.mobile.export_hdr';
const SIZE_KEY = 'gxr.mobile.export_size';
const QUALITY_KEY = 'gxr.mobile.export_quality';

export type ExportSizePref = 'original' | 'large' | 'medium' | 'small';
export type ExportQualityPref = 'high' | 'medium' | 'low';

export function loadExportHdr(): boolean {
  return store()?.getItem(HDR_KEY) !== '0';
}

const hdrWatchers = new Set<() => void>();

export function watchExportHdr(fn: () => void): () => void {
  hdrWatchers.add(fn);
  return () => hdrWatchers.delete(fn);
}

export function saveExportHdr(on: boolean): void {
  try {
    store()?.setItem(HDR_KEY, on ? '1' : '0');
  } catch {
    /* 存不下（隐私模式、满了）就只管这一次。 */
  }
  for (const fn of hdrWatchers) fn();
}

export function loadExportSize(): ExportSizePref {
  const value = store()?.getItem(SIZE_KEY);
  return value === 'original' || value === 'large' || value === 'medium' || value === 'small' ? value : 'original';
}

export function saveExportSize(size: ExportSizePref): void {
  try {
    store()?.setItem(SIZE_KEY, size);
  } catch {
    /* 存不下就只管这一次。 */
  }
}

export function loadExportQuality(): ExportQualityPref {
  const value = store()?.getItem(QUALITY_KEY);
  return value === 'high' || value === 'medium' || value === 'low' ? value : 'high';
}

export function saveExportQuality(quality: ExportQualityPref): void {
  try {
    store()?.setItem(QUALITY_KEY, quality);
  } catch {
    /* 存不下就只管这一次。 */
  }
}
