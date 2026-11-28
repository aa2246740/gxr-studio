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

test('gridColumns keeps phone at 3 and iPad thumbnails in 150-200 pt across widths', async () => {
  const { gridColumns } = await import('../../mobile/src/app/layout.ts');
  const { layoutRows } = await import('../../mobile/src/lib/group.ts');
  for (const w of [390, 440, 844, 956]) {
    assert.equal(gridColumns(w, false), 3);
  }
  assert.equal(gridColumns(820, true), 5);
  assert.equal(gridColumns(1024, true), 6);
  assert.equal(gridColumns(1032, true), 6);
  assert.equal(gridColumns(1180, true), 7);
  assert.equal(gridColumns(1366, true), 8);
  assert.equal(gridColumns(1376, true), 8);
  for (let w = 700; w <= 1440; w += 17) {
    const cols = gridColumns(w, true);
    const thumbW = (w - (cols - 1) * 2) / cols;
    assert.ok(thumbW >= 150 && thumbW <= 200, `width ${w}: cols=${cols} thumbW=${thumbW}`);
  }
  const rows = layoutRows([{ dayKey: '2026-11-22', dayLabel: '11月22日', rangeLabel: '10:00', startMs: 0, endMs: 0, ids: ['1', '2', '3', '4', '5', '6', '7'] }], 100, 6);
  const cellRows = rows.filter((r) => r.kind === 'cells');
  assert.equal(cellRows.length, 2);
  assert.equal(cellRows[0].ids.length, 6);
  assert.equal(cellRows[1].ids.length, 1);
});

test('cellAriaLabel formats position, kind and state without leaking private names', async () => {
  const { cellAriaLabel } = await import('../../mobile/src/app/layout.ts');
  assert.equal(cellAriaLabel(3, { dng: true, jpg: true }, false, false), '第 3 张，RAW+JPG');
  assert.equal(cellAriaLabel(3, { dng: true, jpg: true }, true, false), '第 3 张，RAW+JPG，已编辑');
  assert.equal(cellAriaLabel(3, { dng: true, jpg: true }, true, true), '第 3 张，RAW+JPG，已编辑，已选中');
  assert.equal(cellAriaLabel(1, { dng: true, jpg: false }, false, false), '第 1 张，RAW');
  assert.equal(cellAriaLabel(2, { dng: false, jpg: true }, false, true), '第 2 张，JPG，已选中');
  // Import P3-3：半格双联读屏说「半格」、按相机拼好的双联图调整、没有 HDR；真正只有 JPG 的仍是「JPG」。
  assert.equal(cellAriaLabel(4, { dng: false, jpg: true, diptych: true }, false, false), '第 4 张，半格：按相机拼好的双联图调整，没有 HDR');
  assert.equal(cellAriaLabel(4, { dng: false, jpg: true, diptych: false }, false, false), '第 4 张，JPG');
  assert.equal(cellAriaLabel(5, { dng: true, jpg: false, bad: true }, false, false), '第 5 张，RAW，读不了');
});

test('deviceName detects iPadOS via maxTouchPoints + Mac/iPad UA and keeps iPhone on phones/desktop', async () => {
  const { deviceName, isIpadDevice } = await import('../../mobile/src/lib/device.ts');
  const { hdrLine } = await import('../../mobile/src/lib/hdr_text.ts');
  const { lutFootnote } = await import('../../mobile/src/lib/lut.ts');
  const ipadDesktopUa = {
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15',
    platform: 'MacIntel',
    maxTouchPoints: 5,
  };
  const ipadLegacyUa = {
    userAgent: 'Mozilla/5.0 (iPad; CPU OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148',
    platform: 'iPad',
    maxTouchPoints: 5,
  };
  const iphoneUa = {
    userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1',
    platform: 'iPhone',
    maxTouchPoints: 5,
  };
  const macDesktop = {
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15',
    platform: 'MacIntel',
    maxTouchPoints: 0,
  };
  assert.equal(isIpadDevice(ipadDesktopUa), true);
  assert.equal(deviceName(ipadDesktopUa), 'iPad');
  assert.equal(isIpadDevice(ipadLegacyUa), true);
  assert.equal(deviceName(ipadLegacyUa), 'iPad');
  assert.equal(isIpadDevice(iphoneUa), false);
  assert.equal(deviceName(iphoneUa), 'iPhone');
  assert.equal(isIpadDevice(macDesktop), false);
  assert.equal(deviceName(macDesktop), 'iPhone');
  assert.equal(deviceName({ ...ipadDesktopUa, maxTouchPoints: 1 }), 'iPhone');
  assert.equal(hdrLine(ipadDesktopUa), '在 iPad 上看，亮处会更亮，更接近你当时看到的；别的设备照常显示。');
  assert.equal(hdrLine(iphoneUa), '在 iPhone 上看，亮处会更亮，更接近你当时看到的；别的设备照常显示。');
  assert.equal(lutFootnote(ipadDesktopUa), 'LUT 只存在这台 iPad 上，直接套在照片上，不写入相机。');
  assert.equal(lutFootnote(iphoneUa), 'LUT 只存在这台 iPhone 上，直接套在照片上，不写入相机。');
  const logoSrc = readFileSync(new URL('../../mobile/src/app/logo_sheet.ts', import.meta.url), 'utf8');
  assert.ok(logoSrc.includes("标志只保存在${thisDevice('上')}"), 'logo_sheet must use thisDevice()');
  assert.ok(!logoSrc.includes('这台手机'), 'logo_sheet must not hardcode 这台手机');
});
