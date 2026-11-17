// iPad 分档、格子列数、纹理上限。不读照片。

import assert from 'node:assert/strict';
import test from 'node:test';
import { groupByGap, layoutRows } from '../../mobile/src/lib/group.ts';
import { COLUMNS_MIN, PAD_MIN, TEXTURE_LIMIT, gridColumns, layoutMode } from '../../mobile/src/app/layout.ts';

test('width picks phone, pad, or columns', () => {
  assert.equal(PAD_MIN, 700);
  assert.equal(COLUMNS_MIN, 1000);
  assert.equal(layoutMode(0, 800), 'phone');
  assert.equal(layoutMode(390, 844), 'phone');
  assert.equal(layoutMode(600, 1032), 'phone');
  assert.equal(layoutMode(699, 1200), 'phone');
  assert.equal(layoutMode(700, 1000), 'pad');
  assert.equal(layoutMode(1032, 1376), 'pad');
  assert.equal(layoutMode(999, 800), 'pad');
  assert.equal(layoutMode(1000, 700), 'columns');
  assert.equal(layoutMode(1194, 834), 'columns');
  assert.equal(layoutMode(1376, 1032), 'columns');
  assert.equal(layoutMode(Number.NaN, 800), 'phone');
});

test('grid columns follow the column width', () => {
  assert.equal(gridColumns(390), 3);
  assert.equal(gridColumns(400), 3);
  assert.equal(gridColumns(600), 3);
  assert.equal(gridColumns(700), 4);
  assert.equal(gridColumns(899), 4);
  assert.equal(gridColumns(900), 5);
  assert.equal(gridColumns(1032), 5);
});

test('layoutRows keeps three columns unless asked', () => {
  const times = [0, 1, 2, 3, 4, 5, 6].map((n) => ({ id: `p${n}`, ms: n }));
  const groups = groupByGap(times, 3, 'oldest');
  const three = layoutRows(groups, 80);
  const cells3 = three.filter((row) => row.kind === 'cells');
  assert.ok(cells3.every((row) => row.ids.length <= 3));
  assert.equal(cells3[0].ids.length, 3);
  const five = layoutRows(groups, 80, 5);
  const cells5 = five.filter((row) => row.kind === 'cells');
  assert.equal(cells5[0].ids.length, 5);
  assert.equal(cells5[1].ids.length, 2);
});

test('texture limit is the device probe value', () => {
  assert.equal(TEXTURE_LIMIT, 16384);
});
