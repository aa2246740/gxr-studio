// 27 寸大屏单元测试（Node --experimental-strip-types，不碰真实 DOM）：
// 1) layout.ts：大屏门槛 2000×1100、网格分列、缩略图盒子和配方卡长边；小版式一直是 360 / 192；
// 2) queue.ts：BigThumbBudget 按字节封顶，超了回到 360，关掉的位图不再算；宽度到发给 Worker 时才定，排队的不占账。

import test from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';

registerHooks({
  resolve(spec, ctx, next) {
    try {
      return next(spec, ctx);
    } catch (err) {
      if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(`${spec}.ts`, ctx);
      throw err;
    }
  },
});

const layout = await import('../../mobile/src/app/layout.ts');
const { BigThumbBudget, BIG_THUMB_BYTES, ThumbQueue } = await import('../../mobile/src/app/queue.ts');

test('big screen: only windows at least 2000 x 1100', () => {
  assert.equal(layout.isBigScreen(2560, 1440), true);
  assert.equal(layout.isBigScreen(2000, 1100), true);
  assert.equal(layout.isBigScreen(1999, 1440), false);
  assert.equal(layout.isBigScreen(2560, 1099), false);
  assert.equal(layout.isBigScreen(1440, 900), false);
  assert.equal(layout.isBigScreen(1366, 1024), false);
  // Node 里没有 matchMedia：不是大屏，小版式的列数不变。
  assert.equal(layout.isBigScreen(), false);
  assert.equal(layout.gridColumns(1440, true), 9);
  assert.equal(layout.BIG_SCREEN_QUERY, '(min-width: 2000px) and (min-height: 1100px)');
});

test('big screen: grid cells 200-240 px from 2000 to 3840 wide; small layouts unchanged', () => {
  for (const w of [2000, 2200, 2560, 3008, 3440, 3840]) {
    const cols = layout.gridColumns(w, true, true);
    const cell = (w - (cols - 1) * 2) / cols;
    assert.ok(cell >= 200 && cell <= 240, `${w}: ${cols} cols, ${cell.toFixed(1)} px`);
  }
  for (const [w, want] of [[1440, 9], [1280, 8], [1366, 8], [1180, 7], [1024, 6], [2560, 16]]) assert.equal(layout.gridColumns(w, true, false), want);
  assert.equal(layout.gridColumns(390, false, true), 3);
});

test('big screen: thumbnail box covers the cell at DPR (max 2x); small stays 360 wide only', () => {
  assert.deepEqual(layout.gridThumbBox(1440, 2, false), { width: 360, height: 0 });
  assert.deepEqual(layout.gridThumbBox(2560, 2, false), { width: 360, height: 0 });
  const box = layout.gridThumbBox(2560, 2, true);
  const cols = layout.gridColumns(2560, true, true);
  const cell = (2560 - (cols - 1) * 2) / cols;
  assert.ok(box.width >= cell * 2 && box.width <= cell * 2 + 2, JSON.stringify(box));
  assert.ok(box.height >= (cell * 2) / 3 * 2 + 4 && box.height <= box.width, JSON.stringify(box));
  assert.deepEqual(layout.gridThumbBox(2560, 3, true), box, 'DPR capped at 2');
  assert.equal(layout.gridThumbBox(2560, 1, true).width, 360, 'never below 360');
  assert.equal(layout.cardBakeEdge(2, false), 192);
  assert.equal(layout.cardBakeEdge(2, true), 272);
  assert.equal(layout.cardBakeEdge(1, true), 192);
});

function bitmap(width, height) {
  return { width, height, close() { this.width = 0; this.height = 0; } };
}

test('BigThumbBudget: capped by bytes, falls back to 360, frees closed bitmaps', () => {
  assert.equal(BIG_THUMB_BYTES, 128 << 20);
  const one = 470 * 313 * 4;
  const budget = new BigThumbBudget(one * 3 + 4 * 470 * 705 * 4);
  assert.equal(budget.take(360), 360, 'small widths never touch the budget');
  const held = [];
  for (let i = 0; i < 1000; i++) {
    const w = budget.take(470);
    const bmp = bitmap(w, Math.round((w * 2) / 3));
    budget.settle(w, bmp);
    held.push(bmp);
  }
  const stats = budget.stats();
  assert.ok(stats.bytes <= stats.limit, JSON.stringify(stats));
  assert.equal(stats.reserved, 0);
  const big = held.filter((b) => b.width > 360).length;
  assert.ok(big >= 3 && big < 1000, `big ${big}`);
  for (const b of held) if (b.width > 360) b.close();
  assert.equal(budget.bytes(), 0, 'closed bitmaps no longer count');
  assert.equal(budget.take(470), 470, 'room again after closing');
  budget.settle(470, null);
  assert.equal(budget.stats().reserved, 0, 'a failed read releases its reservation');
});

test('ThumbQueue: a 300-photo import fills the budget, not the queue reservations', async () => {
  const sent = [];
  class FakeWorker {
    constructor() {
      this.onmessage = null;
      this.onerror = null;
    }
    postMessage(msg) {
      sent.push({ width: msg.width, height: msg.height });
      const bmp = bitmap(msg.width, Math.round((msg.width * 2) / 3));
      setTimeout(() => this.onmessage({ data: { kind: 'thumb', name: 'x', id: msg.id, ok: true, captureMs: null, bitmap: bmp, bad: false, meta: {}, frame: null } }), 0);
    }
    terminate() {}
  }
  const saved = { Worker: globalThis.Worker, window: globalThis.window, matchMedia: globalThis.matchMedia };
  Object.assign(globalThis, { Worker: FakeWorker, window: { innerWidth: 2560, devicePixelRatio: 2 }, matchMedia: () => ({ matches: true }) });
  try {
    const queue = new ThumbQueue(2);
    const file = { name: 'R0000001.JPG' };
    const replies = await Promise.all(Array.from({ length: 300 }, () => queue.run(file, 'jpg')));
    const big = replies.filter((r) => r.ok && r.bitmap.width > 360).length;
    const stats = queue.budget.stats();
    const each = 462 * 308 * 4;
    assert.ok(stats.bytes <= BIG_THUMB_BYTES, JSON.stringify(stats));
    assert.ok(big >= Math.floor(BIG_THUMB_BYTES / each) - 6, `only ${big} of 300 decoded big (${JSON.stringify(stats)})`);
    assert.ok(big < 300, 'the cap stops big decodes');
    assert.equal(stats.reserved, 0);
    assert.ok(sent.filter((m) => m.width === 360).every((m) => m.height === 0), 'fallbacks are plain 360 width-only reads');
    assert.ok(sent.filter((m) => m.width > 360).every((m) => m.height > 0), 'big reads carry the cell height');
    // 显式宽度（编辑器要的大图）不进账、照原样发。
    const still = await queue.run(file, 'jpg', 1024, true);
    assert.equal(still.bitmap.width, 1024);
    assert.equal(queue.budget.stats().count, big);
    queue.stop();
  } finally {
    Object.assign(globalThis, saved);
  }
});
