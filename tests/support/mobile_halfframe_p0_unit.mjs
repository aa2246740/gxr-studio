import assert from 'node:assert/strict';
import test from 'node:test';

import { DRAFT_HEIGHT, DRAFT_WIDTH, isDiptychRgba } from '../../mobile/src/lib/halfframe.ts';

function makeRgba(width, height, fill = 128) {
  const buf = new Uint8ClampedArray(width * height * 4);
  for (let i = 0; i < buf.length; i += 4) {
    buf[i] = fill;
    buf[i + 1] = fill;
    buf[i + 2] = fill;
    buf[i + 3] = 255;
  }
  return buf;
}

function blackCols(buf, width, height, fullStart, fullEnd) {
  const x0 = Math.floor(fullStart / 8);
  const x1 = Math.floor(fullEnd / 8);
  for (let y = 0; y < height; y += 1) {
    for (let x = x0; x <= x1; x += 1) {
      const off = (y * width + x) * 4;
      buf[off] = 0;
      buf[off + 1] = 0;
      buf[off + 2] = 0;
    }
  }
}

function blackRows(buf, width, fullStart, fullEnd) {
  const y0 = Math.floor(fullStart / 8);
  const y1 = Math.floor(fullEnd / 8);
  for (let y = y0; y <= y1; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const off = (y * width + x) * 4;
      buf[off] = 0;
      buf[off + 1] = 0;
      buf[off + 2] = 0;
    }
  }
}

test('isDiptychRgba detects g250 and legacy48 seams and rejects counterexamples', () => {
  const g250 = makeRgba(DRAFT_WIDTH, DRAFT_HEIGHT);
  blackCols(g250, DRAFT_WIDTH, DRAFT_HEIGHT, 2088, 2199);
  assert.equal(isDiptychRgba(g250, DRAFT_WIDTH, DRAFT_HEIGHT, 'col'), true);

  const legacy48 = makeRgba(DRAFT_WIDTH, DRAFT_HEIGHT);
  blackCols(legacy48, DRAFT_WIDTH, DRAFT_HEIGHT, 2120, 2167);
  assert.equal(isDiptychRgba(legacy48, DRAFT_WIDTH, DRAFT_HEIGHT, 'col'), true);

  const shifted = makeRgba(DRAFT_WIDTH, DRAFT_HEIGHT);
  blackCols(shifted, DRAFT_WIDTH, DRAFT_HEIGHT, 1200, 1311);
  assert.equal(isDiptychRgba(shifted, DRAFT_WIDTH, DRAFT_HEIGHT, 'col'), false);

  const wideBand = makeRgba(DRAFT_WIDTH, DRAFT_HEIGHT);
  blackCols(wideBand, DRAFT_WIDTH, DRAFT_HEIGHT, 2040, 2248);
  assert.equal(isDiptychRgba(wideBand, DRAFT_WIDTH, DRAFT_HEIGHT, 'col'), false);

  const uniform = makeRgba(DRAFT_WIDTH, DRAFT_HEIGHT);
  assert.equal(isDiptychRgba(uniform, DRAFT_WIDTH, DRAFT_HEIGHT, 'col'), false);

  const xpanHeight = Math.floor(1584 / 8);
  const xpan = makeRgba(DRAFT_WIDTH, xpanHeight);
  blackCols(xpan, DRAFT_WIDTH, xpanHeight, 2088, 2199);
  assert.equal(isDiptychRgba(xpan, DRAFT_WIDTH, xpanHeight, 'col'), false);

  const portraitDiptych = makeRgba(DRAFT_HEIGHT, DRAFT_WIDTH);
  blackRows(portraitDiptych, DRAFT_HEIGHT, 2088, 2199);
  assert.equal(isDiptychRgba(portraitDiptych, DRAFT_HEIGHT, DRAFT_WIDTH, 'row'), true);

  const portraitWide = makeRgba(DRAFT_HEIGHT, DRAFT_WIDTH);
  blackRows(portraitWide, DRAFT_HEIGHT, 2040, 2248);
  assert.equal(isDiptychRgba(portraitWide, DRAFT_HEIGHT, DRAFT_WIDTH, 'row'), false);
});
