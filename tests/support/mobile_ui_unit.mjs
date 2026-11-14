// EXIF 和分组是纯函数。用 Node 自带的测试跑，不另装依赖。

import assert from 'node:assert/strict';
import test from 'node:test';
import { captureMillis, captureTimeFromJpeg, captureTimeFromTiff, embeddedJpegRange } from '../../mobile/src/lib/exif.ts';
import { classify, groupByGap, groupCount, sortIds, visibleRange } from '../../mobile/src/lib/group.ts';

function u16(value) {
  return [value & 255, (value >> 8) & 255];
}

function u32(value) {
  return [value & 255, (value >> 8) & 255, (value >> 16) & 255, (value >> 24) & 255];
}

function entry(tag, type, count, value) {
  return [...u16(tag), ...u16(type), ...u32(count), ...u32(value)];
}

/** 最小的小端 TIFF：Exif IFD 里有 DateTimeOriginal 和 SubSecTimeOriginal。 */
function codes(text) {
  return [...text].map((char) => char.charCodeAt(0));
}

function tiffWithDate(text, sub) {
  const date = [...codes(text), 0];
  const subInline = [...codes(sub), 0, 0, 0, 0].slice(0, 4);
  const subValue = subInline[0] + subInline[1] * 256 + subInline[2] * 65536 + subInline[3] * 16777216;
  const exifAt = 8 + 2 + 12 + 4;
  const dateAt = exifAt + 2 + 24 + 4;
  const bytes = [
    0x49, 0x49, 0x2a, 0x00,
    ...u32(8),
    ...u16(1),
    ...entry(0x8769, 4, 1, exifAt),
    ...u32(0),
    ...u16(2),
    ...entry(0x9003, 2, date.length, dateAt),
    ...entry(0x9291, 2, sub.length + 1, subValue),
    ...u32(0),
    ...date,
  ];
  return Uint8Array.from(bytes);
}

function jpegWith(tiff) {
  const payload = [0x45, 0x78, 0x69, 0x66, 0x00, 0x00, ...tiff];
  const length = payload.length + 2;
  return Uint8Array.from([0xff, 0xd8, 0xff, 0xe1, (length >> 8) & 255, length & 255, ...payload, 0xff, 0xd9]);
}

test('jpeg exif date and subseconds', () => {
  const text = '2026:10:24 17:45:01';
  const bytes = jpegWith(tiffWithDate(text, '73'));
  const time = captureTimeFromJpeg(bytes);
  assert.ok(time);
  const date = new Date(time.ms);
  assert.equal(date.getFullYear(), 2026);
  assert.equal(date.getMonth(), 9);
  assert.equal(date.getDate(), 24);
  assert.equal(date.getHours(), 17);
  assert.equal(date.getMinutes(), 45);
  assert.equal(date.getSeconds(), 1);
  assert.equal(date.getMilliseconds(), 730);
});

test('tiff date without a jpeg wrapper', () => {
  const time = captureTimeFromTiff(tiffWithDate('2024:01:02 03:04:05', '7'));
  assert.equal(new Date(time.ms).getMilliseconds(), 700);
});

test('bad bytes do not throw', () => {
  const junk = Uint8Array.from({ length: 64 }, (_, i) => (i * 17) & 255);
  assert.equal(captureTimeFromJpeg(junk), null);
  assert.equal(captureTimeFromJpeg(Uint8Array.of(0xff, 0xd8, 0xff)), null);
  assert.equal(captureTimeFromTiff(junk), null);
  assert.equal(captureTimeFromTiff(new Uint8Array()), null);
  assert.equal(embeddedJpegRange(junk), null);
  assert.equal(captureMillis('2026-10-24 17:45:01', null), null);
  assert.equal(captureMillis('2026:02:31 00:00:00', null), null);
});

test('embedded jpeg range points at the preview bytes', () => {
  const offset = 8 + 2 + 24 + 4;
  const bytes = Uint8Array.from([
    0x49, 0x49, 0x2a, 0x00,
    ...u32(8),
    ...u16(2),
    ...entry(0x0201, 4, 1, offset),
    ...entry(0x0202, 4, 1, 4),
    ...u32(0),
    0xff, 0xd8, 0xff, 0xd9,
  ]);
  const range = embeddedJpegRange(bytes);
  assert.deepEqual(range, { offset, length: 4 });
  assert.equal(bytes[range.offset], 0xff);
});

test('dng preview in a reduced jpeg strip of a sub ifd', () => {
  // IFD0：160×120 未压缩缩略图，SubIFD 两个：CFA 原始数据（无损 JPEG）和 JPEG 预览。只该挑预览。
  const ifd0 = 8;
  const subs = ifd0 + 2 + 12 * 3 + 4;
  const raw = subs + 8;
  const preview = raw + 2 + 12 * 5 + 4;
  const data = preview + 2 + 12 * 5 + 4;
  const bytes = Uint8Array.from([
    0x49, 0x49, 0x2a, 0x00,
    ...u32(ifd0),
    ...u16(3),
    ...entry(0x00fe, 4, 1, 1),
    ...entry(0x0111, 4, 1, 9000),
    ...entry(0x014a, 4, 2, subs),
    ...u32(0),
    ...u32(raw),
    ...u32(preview),
    ...u16(5),
    ...entry(0x00fe, 4, 1, 0),
    ...entry(0x0103, 3, 1, 7),
    ...entry(0x0106, 3, 1, 32803),
    ...entry(0x0111, 4, 1, 100000),
    ...entry(0x0117, 4, 1, 5000000),
    ...u32(0),
    ...u16(5),
    ...entry(0x00fe, 4, 1, 1),
    ...entry(0x0103, 3, 1, 7),
    ...entry(0x0106, 3, 1, 6),
    ...entry(0x0111, 4, 1, data),
    ...entry(0x0117, 4, 1, 4),
    ...u32(0),
    0xff, 0xd8, 0xff, 0xd9,
  ]);
  assert.deepEqual(embeddedJpegRange(bytes), { offset: data, length: 4 });
  // 只有原始数据那一条时不当成预览。
  const rawOnly = bytes.slice();
  rawOnly[subs + 4] = raw & 255;
  rawOnly[subs + 5] = (raw >> 8) & 255;
  assert.equal(embeddedJpegRange(rawOnly), null);
});

test('pairing, dat and ignored files', () => {
  const files = [
    { name: 'R001.DNG', lastModified: 10 },
    { name: 'R001.JPG', lastModified: 11 },
    { name: 'notes.txt', lastModified: 1 },
    { name: 'GXRCE.DAT', lastModified: 2 },
    { name: 'R002.JPG', lastModified: 30 },
    { name: 'DSC0001.DNG', lastModified: 4 },
  ];
  const out = classify(files);
  assert.equal(out.datIndex, 3);
  assert.equal(out.ignored, 2);
  assert.equal(out.photos.length, 2);
  const first = out.photos[0];
  assert.equal(first.stem, 'R001');
  assert.equal(first.dngIndex, 0);
  assert.equal(first.jpgIndex, 1);
  assert.equal(out.photos[1].dngIndex, null);
  assert.equal(out.photos[1].jpgIndex, 4);
});

test('three minute gap and order toggle', () => {
  const base = Date.parse('2026-10-24T10:00:00');
  const items = [
    { id: 'a', ms: base },
    { id: 'b', ms: base + 2 * 60_000 },
    { id: 'c', ms: base + 6 * 60_000 },
  ];
  const oldest = groupByGap(items, 3, 'oldest');
  assert.equal(oldest.length, 2);
  assert.deepEqual(oldest[0].ids, ['a', 'b']);
  assert.deepEqual(oldest[1].ids, ['c']);
  assert.match(oldest[0].label ?? oldest[0].rangeLabel, /10:00/);
  const newest = groupByGap(items, 3, 'newest');
  assert.deepEqual(newest[0].ids, ['c']);
  assert.deepEqual(newest[1].ids, ['b', 'a']);
  assert.deepEqual(sortIds(items, 'newest').map((item) => item.id), ['c', 'b', 'a']);
  assert.equal(groupCount(items.map((item) => item.ms), 3), 2);
  assert.equal(groupCount(items.map((item) => item.ms), 10), 1);
});

test('equal capture times keep one order for grid, index and swipe', () => {
  // 连拍没有亚秒时间：三张同一毫秒。网格分组摊平后的顺序必须和编辑器用的 sortIds 一样。
  const ms = Date.parse('2026-10-24T10:00:00');
  const items = [{ id: 'r9700002', ms }, { id: 'r9700001', ms }, { id: 'r9700003', ms }];
  for (const order of ['oldest', 'newest']) {
    const grid = groupByGap(items, 3, order).flatMap((group) => group.ids);
    const editor = sortIds(items, order).map((item) => item.id);
    assert.deepEqual(grid, editor, order);
  }
  assert.deepEqual(sortIds(items, 'oldest').map((item) => item.id), ['r9700001', 'r9700002', 'r9700003']);
  assert.deepEqual(sortIds(items, 'newest').map((item) => item.id), ['r9700003', 'r9700002', 'r9700001']);
});

test('virtual window does not mount every row', () => {
  const heights = Array.from({ length: 500 }, () => 100);
  const range = visibleRange(heights, 10_000, 800, 200);
  assert.ok(range.end - range.start < 20);
  assert.ok(range.start > 50);
  assert.equal(range.top, range.start * 100);
});
