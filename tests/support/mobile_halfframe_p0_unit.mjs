import assert from 'node:assert/strict';
import test from 'node:test';

import { DRAFT_HEIGHT, DRAFT_WIDTH, FULL_HEIGHT, FULL_WIDTH, draftLayoutForBitmap, isDiptychRgba } from '../../mobile/src/lib/halfframe.ts';

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

test('draftLayoutForBitmap chooses col/row from decoded bitmap dimensions', () => {
  assert.deepEqual(draftLayoutForBitmap(DRAFT_WIDTH, DRAFT_HEIGHT), {
    draftWidth: DRAFT_WIDTH,
    draftHeight: DRAFT_HEIGHT,
    axis: 'col',
  });
  assert.deepEqual(draftLayoutForBitmap(FULL_WIDTH, FULL_HEIGHT), {
    draftWidth: DRAFT_WIDTH,
    draftHeight: DRAFT_HEIGHT,
    axis: 'col',
  });
  assert.deepEqual(draftLayoutForBitmap(DRAFT_HEIGHT, DRAFT_WIDTH), {
    draftWidth: DRAFT_HEIGHT,
    draftHeight: DRAFT_WIDTH,
    axis: 'row',
  });
  assert.deepEqual(draftLayoutForBitmap(FULL_HEIGHT, FULL_WIDTH), {
    draftWidth: DRAFT_HEIGHT,
    draftHeight: DRAFT_WIDTH,
    axis: 'row',
  });
  assert.equal(draftLayoutForBitmap(DRAFT_WIDTH, 198), null);
});

test('isDiptychRgba detects g250 and legacy48 seams in landscape and portrait and rejects counterexamples', () => {
  const g250 = makeRgba(DRAFT_WIDTH, DRAFT_HEIGHT);
  blackCols(g250, DRAFT_WIDTH, DRAFT_HEIGHT, 2088, 2199);
  assert.equal(isDiptychRgba(g250, DRAFT_WIDTH, DRAFT_HEIGHT, 'col'), true);
  assert.equal(isDiptychRgba(g250, DRAFT_WIDTH, DRAFT_HEIGHT), true);

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
  assert.equal(isDiptychRgba(portraitDiptych, DRAFT_HEIGHT, DRAFT_WIDTH), true);

  const portraitLegacy48 = makeRgba(DRAFT_HEIGHT, DRAFT_WIDTH);
  blackRows(portraitLegacy48, DRAFT_HEIGHT, 2120, 2167);
  assert.equal(isDiptychRgba(portraitLegacy48, DRAFT_HEIGHT, DRAFT_WIDTH, 'row'), true);

  const portraitShifted = makeRgba(DRAFT_HEIGHT, DRAFT_WIDTH);
  blackRows(portraitShifted, DRAFT_HEIGHT, 1200, 1311);
  assert.equal(isDiptychRgba(portraitShifted, DRAFT_HEIGHT, DRAFT_WIDTH, 'row'), false);

  const portraitWide = makeRgba(DRAFT_HEIGHT, DRAFT_WIDTH);
  blackRows(portraitWide, DRAFT_HEIGHT, 2040, 2248);
  assert.equal(isDiptychRgba(portraitWide, DRAFT_HEIGHT, DRAFT_WIDTH, 'row'), false);

  const portraitUniform = makeRgba(DRAFT_HEIGHT, DRAFT_WIDTH);
  assert.equal(isDiptychRgba(portraitUniform, DRAFT_HEIGHT, DRAFT_WIDTH, 'row'), false);
});

test('isDiptychRgba accepts g250 seam adjoining a dark scene on one side while rejecting legacy48 and dark negatives', () => {
  // MOUNT boundary analogue: dark curtain right of g250 seam (2088..2239 black), dim detail on right half
  const darkRight = makeRgba(DRAFT_WIDTH, DRAFT_HEIGHT, 140);
  blackCols(darkRight, DRAFT_WIDTH, DRAFT_HEIGHT, 2088, 2239);
  for (let y = 0; y < DRAFT_HEIGHT; y += 1) {
    for (let x = Math.floor(2240 / 8); x < DRAFT_WIDTH; x += 1) {
      const off = (y * DRAFT_WIDTH + x) * 4;
      darkRight[off] = 40;
      darkRight[off + 1] = 40;
      darkRight[off + 2] = 40;
    }
  }
  assert.equal(isDiptychRgba(darkRight, DRAFT_WIDTH, DRAFT_HEIGHT, 'col'), true);

  // Symmetric dark scene left of g250 seam in portrait orientation
  const darkLeftPortrait = makeRgba(DRAFT_HEIGHT, DRAFT_WIDTH, 140);
  blackRows(darkLeftPortrait, DRAFT_HEIGHT, 2040, 2199);
  for (let y = 0; y < Math.floor(2040 / 8); y += 1) {
    for (let x = 0; x < DRAFT_HEIGHT; x += 1) {
      const off = (y * DRAFT_HEIGHT + x) * 4;
      darkLeftPortrait[off] = 40;
      darkLeftPortrait[off + 1] = 40;
      darkLeftPortrait[off + 2] = 40;
    }
  }
  assert.equal(isDiptychRgba(darkLeftPortrait, DRAFT_HEIGHT, DRAFT_WIDTH, 'row'), true);

  // legacy48 keeps its strict two-sided rule
  const legacyDarkRight = makeRgba(DRAFT_WIDTH, DRAFT_HEIGHT, 140);
  blackCols(legacyDarkRight, DRAFT_WIDTH, DRAFT_HEIGHT, 2120, 2207);
  assert.equal(isDiptychRgba(legacyDarkRight, DRAFT_WIDTH, DRAFT_HEIGHT, 'col'), false);

  // Dark negative controls: all-black and half-black must remain false
  const allBlack = makeRgba(DRAFT_WIDTH, DRAFT_HEIGHT, 0);
  assert.equal(isDiptychRgba(allBlack, DRAFT_WIDTH, DRAFT_HEIGHT, 'col'), false);

  const halfBlackRight = makeRgba(DRAFT_WIDTH, DRAFT_HEIGHT, 140);
  blackCols(halfBlackRight, DRAFT_WIDTH, DRAFT_HEIGHT, 2088, 4287);
  assert.equal(isDiptychRgba(halfBlackRight, DRAFT_WIDTH, DRAFT_HEIGHT, 'col'), false);
});
