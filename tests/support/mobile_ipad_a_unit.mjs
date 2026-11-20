// ipad-a 单元测试（Node --experimental-strip-types，不碰真实 DOM）：
// 1) layout.ts：7 种标准视口与 700pt 窄分屏边界判定、13"/11" 尺寸判定、iPad 直方图默认开与记住关；
// 2) filmstrip.ts：按时间段分组（1 张独立组不加标题，跟在另一组后加分隔线）；
// 3) export_options.ts 与 more_panel.ts：批准文案与「边框」首位顺序。

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
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

function installMemoryStorage(seed = {}) {
  const map = new Map(Object.entries(seed));
  const storage = {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    clear: () => map.clear(),
  };
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage });
  return map;
}

test('layout: classifyLayout covers all 7 viewports and 700pt Split View boundary', async () => {
  const { classifyLayout, isIpadLayout, ipadSizeOf } = await import('../../mobile/src/app/layout.ts');
  assert.equal(classifyLayout(1366, 1024), 'ipad-landscape');
  assert.equal(classifyLayout(1024, 1366), 'ipad-portrait');
  assert.equal(classifyLayout(1180, 820), 'ipad-landscape');
  assert.equal(classifyLayout(844, 390), 'phone-landscape');
  assert.equal(classifyLayout(956, 440), 'phone-landscape');
  assert.equal(classifyLayout(390, 844), 'phone-portrait');
  assert.equal(classifyLayout(440, 956), 'phone-portrait');

  // iPad narrow Split View / Slide Over (< 700pt short edge) falls back to phone layout
  assert.equal(classifyLayout(699, 1024), 'phone-portrait');
  assert.equal(classifyLayout(700, 1024), 'ipad-portrait');
  assert.equal(classifyLayout(1024, 699), 'phone-landscape');
  assert.equal(classifyLayout(1024, 700), 'ipad-landscape');

  assert.equal(isIpadLayout('ipad-landscape'), true);
  assert.equal(isIpadLayout('ipad-portrait'), true);
  assert.equal(isIpadLayout('phone-landscape'), false);
  assert.equal(isIpadLayout('phone-portrait'), false);

  assert.equal(ipadSizeOf(1366, 1024), '13');
  assert.equal(ipadSizeOf(1024, 1366), '13');
  assert.equal(ipadSizeOf(1180, 820), '11');
});

test('layout: iPad histogram defaults ON and remembers OFF', async () => {
  const map = installMemoryStorage();
  const { loadIpadHist, saveIpadHist } = await import('../../mobile/src/app/layout.ts');
  assert.equal(loadIpadHist(), true);

  saveIpadHist(false);
  assert.equal(map.get('gxr.mobile.ipad_hist'), '0');
  assert.equal(loadIpadHist(), false);

  saveIpadHist(true);
  assert.equal(map.get('gxr.mobile.ipad_hist'), '1');
  assert.equal(loadIpadHist(), true);

  installMemoryStorage({ 'gxr.mobile.ipad_hist': '0' });
  assert.equal(loadIpadHist(), false);
});

test('filmstrip: 1-photo solo group omits header and adds separator after a previous group', async () => {
  const { buildFilmstripEntries } = await import('../../mobile/src/app/filmstrip.ts');
  const groups = [
    { key: 'g1', label: '3月15日 · 上午', ids: ['a'] },
    { key: 'g2', label: '3月15日 · 下午', ids: ['b', 'c'] },
    { key: 'g3', label: '3月16日 · 早上', ids: ['d'] },
  ];
  const entries = buildFilmstripEntries(groups);
  assert.deepEqual(
    entries.map((e) => ({ kind: e.kind, photoId: e.photoId ?? null, soloSep: Boolean(e.soloSep) })),
    [
      { kind: 'cell', photoId: 'a', soloSep: false },
      { kind: 'header', photoId: null, soloSep: false },
      { kind: 'cell', photoId: 'b', soloSep: false },
      { kind: 'cell', photoId: 'c', soloSep: false },
      { kind: 'cell', photoId: 'd', soloSep: true },
    ],
  );
});

test('approved copy and inline More order (边框 first)', () => {
  const exportSrc = readFileSync(new URL('../../mobile/src/app/export_options.ts', import.meta.url), 'utf8');
  assert.ok(exportSrc.includes('「存到相册」每次都按这里的设置做。'));
  assert.ok(exportSrc.includes("'好'"));

  const moreSrc = readFileSync(new URL('../../mobile/src/app/more_panel.ts', import.meta.url), 'utf8');
  const idxFrame = moreSrc.indexOf("key: 'frame'");
  const idxStamp = moreSrc.indexOf("key: 'stamp'");
  const idxCurves = moreSrc.indexOf("key: 'curves'");
  assert.ok(idxFrame > 0 && idxFrame < idxStamp && idxStamp < idxCurves, '边框 must be first in inline MorePanel');
});
