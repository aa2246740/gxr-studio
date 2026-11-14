// 选文件之后的分组。不读字节：配对、排序、按拍摄间隔切开、算出该挂上屏幕的那几行。

export type Order = 'oldest' | 'newest';
export type GapMinutes = 1 | 3 | 5 | 10;

export interface Incoming {
  name: string;
  lastModified: number;
}

export interface ClassifiedPhoto {
  stem: string;
  dngIndex: number | null;
  jpgIndex: number | null;
  lastModified: number;
}

export interface Classified {
  photos: ClassifiedPhoto[];
  datIndex: number | null;
  ignored: number;
}

export interface Timed {
  id: string;
  ms: number;
}

export interface TimeGroup {
  dayKey: string;
  dayLabel: string;
  rangeLabel: string;
  startMs: number;
  endMs: number;
  ids: string[];
}

export interface VRow {
  key: string;
  kind: 'day' | 'group' | 'cells';
  height: number;
  ids: string[];
  label: string;
}

const PHOTO = /^R.+\.(dng|jpg)$/i;
const DAT = /^GXRCE\.DAT$/i;

function stemOf(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot <= 0 ? name : name.slice(0, dot);
}

/** `R*.DNG` 和 `R*.JPG` 按主文件名配对。`GXRCE.DAT` 单独留下。别的都计入忽略。 */
export function classify(files: readonly Incoming[]): Classified {
  const buckets = new Map<string, ClassifiedPhoto>();
  const order: string[] = [];
  let datIndex: number | null = null;
  let ignored = 0;
  files.forEach((file, index) => {
    if (DAT.test(file.name)) {
      if (datIndex === null) datIndex = index;
      else ignored += 1;
      return;
    }
    const match = PHOTO.exec(file.name);
    if (!match) {
      ignored += 1;
      return;
    }
    const stem = stemOf(file.name);
    const key = stem.toLowerCase();
    let photo = buckets.get(key);
    if (!photo) {
      photo = { stem, dngIndex: null, jpgIndex: null, lastModified: file.lastModified };
      buckets.set(key, photo);
      order.push(key);
    }
    const kind = match[1].toLowerCase();
    if (kind === 'dng') {
      photo.dngIndex = index;
      photo.stem = stem;
    } else photo.jpgIndex = index;
    photo.lastModified = Math.max(photo.lastModified, file.lastModified);
  });
  return { photos: order.map((key) => buckets.get(key)!), datIndex, ignored };
}

export function isGap(value: number): value is GapMinutes {
  return value === 1 || value === 3 || value === 5 || value === 10;
}

export function sortIds<T extends { id: string; ms: number }>(items: readonly T[], order: Order): T[] {
  const next = [...items].sort((a, b) => a.ms - b.ms || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  if (order === 'newest') next.reverse();
  return next;
}

export function formatClock(ms: number): string {
  const date = new Date(ms);
  const hour = String(date.getHours()).padStart(2, '0');
  const minute = String(date.getMinutes()).padStart(2, '0');
  return `${hour}:${minute}`;
}

export function formatDay(ms: number): string {
  const date = new Date(ms);
  return `${date.getMonth() + 1}月${date.getDate()}日`;
}

function dayKey(ms: number): string {
  const date = new Date(ms);
  return `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;
}

function rangeLabel(start: number, end: number): string {
  const a = formatClock(start);
  const b = formatClock(end);
  return a === b ? a : `${a}–${b}`;
}

/**
 * 按时间顺序切开。相邻两张相差严格大于间隔，或跨了本地日期，就新开一组。
 * `order` 只决定组内和组与组的展示方向，不改变谁跟谁一组。
 */
export function groupByGap(items: readonly Timed[], gapMinutes: GapMinutes, order: Order): TimeGroup[] {
  const chronological = sortIds(items, 'oldest');
  const gapMs = gapMinutes * 60_000;
  const groups: TimeGroup[] = [];
  for (const item of chronological) {
    const prev = groups[groups.length - 1];
    const prevMs = prev ? prev.endMs : null;
    const split = prevMs === null || item.ms - prevMs > gapMs || dayKey(item.ms) !== prev.dayKey;
    if (!prev || split) {
      groups.push({
        dayKey: dayKey(item.ms),
        dayLabel: formatDay(item.ms),
        rangeLabel: rangeLabel(item.ms, item.ms),
        startMs: item.ms,
        endMs: item.ms,
        ids: [item.id],
      });
      continue;
    }
    prev.endMs = item.ms;
    prev.rangeLabel = rangeLabel(prev.startMs, prev.endMs);
    prev.ids.push(item.id);
  }
  if (order === 'newest') {
    for (const group of groups) group.ids.reverse();
    groups.reverse();
  }
  return groups;
}

/** 一天一行标题，一组一行标题加若干每行 3 张的格子。 */
export function layoutRows(groups: readonly TimeGroup[], cellHeight: number): VRow[] {
  const rows: VRow[] = [];
  let lastDay = '';
  for (const group of groups) {
    if (group.dayKey !== lastDay) {
      lastDay = group.dayKey;
      rows.push({ key: `day:${group.dayKey}`, kind: 'day', height: 36, ids: [], label: group.dayLabel });
    }
    rows.push({
      key: `group:${group.ids[0] ?? group.startMs}`,
      kind: 'group',
      height: 52,
      ids: group.ids,
      label: `${group.rangeLabel} · ${group.ids.length} 张`,
    });
    for (let i = 0; i < group.ids.length; i += 3) {
      const ids = group.ids.slice(i, i + 3);
      rows.push({ key: `cells:${ids[0]}`, kind: 'cells', height: cellHeight, ids, label: '' });
    }
  }
  return rows;
}

export function visibleRange(
  heights: readonly number[],
  scrollTop: number,
  viewport: number,
  buffer: number,
): { start: number; end: number; top: number } {
  if (!heights.length || viewport <= 0) return { start: 0, end: 0, top: 0 };
  const from = Math.max(0, scrollTop - buffer);
  const to = scrollTop + viewport + buffer;
  let y = 0;
  let start = 0;
  let top = 0;
  let seen = false;
  for (let i = 0; i < heights.length; i++) {
    const next = y + heights[i];
    if (!seen && next > from) {
      start = i;
      top = y;
      seen = true;
    }
    if (seen && y >= to) return { start, end: i, top };
    y = next;
  }
  return seen ? { start, end: heights.length, top } : { start: 0, end: 0, top: 0 };
}

/** 同一次拍摄里，每种间隔会切出多少组。菜单上那一行字用这个数。 */
export function groupCount(times: readonly number[], gapMinutes: GapMinutes): number {
  const sorted = [...times].sort((a, b) => a - b);
  let count = sorted.length ? 1 : 0;
  const gapMs = gapMinutes * 60_000;
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i] - sorted[i - 1] > gapMs || dayKey(sorted[i]) !== dayKey(sorted[i - 1])) count += 1;
  }
  return count;
}
