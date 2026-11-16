// UI-M4 边框、字体、日期戳与标志去白底纯函数单元测试。Node `--experimental-strip-types` 直接跑。

import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import {
  ascentOf,
  CJK_REFUSE_MESSAGE,
  clipText,
  FONT_FILES,
  FONT_LICENCE_FILES,
  fontsNeeded,
  GXR_MARK,
  hasCjk,
  stripCjk,
} from '../../mobile/src/lib/fonts.ts';
import {
  DEFAULT_FRAME,
  DEFAULT_STAMP,
  FRAME_NAMES,
  FRAME_ORDER,
  isDateString,
  momentFromIso,
  normalizeFrame,
  normalizeStamp,
  pickLogoMode,
  sameFrameSpec,
  sameStampSpec,
  selectFrame,
  stampDateText,
  stampSummary,
  STYLE_OPTIONS,
} from '../../mobile/src/lib/frame_spec.ts';
import {
  cleanLogoName,
  cropImageData,
  hasWhiteBackground,
  isOpaqueImageData,
  keyWhiteImageData,
  LOGO_LIMITS,
  logoStats,
  sniffImageMagic,
  stemOf,
  trimBounds,
} from '../../mobile/src/lib/logo_key.ts';

test('OFL font assets and licences exist without any Ricoh reference', () => {
  const fontDir = path.resolve('src/gxr_studio/assets/fonts');
  for (const [key, filename] of Object.entries(FONT_FILES)) {
    assert.ok(!/ricoh/i.test(filename), `font ${key} must not reference ricoh`);
    const filePath = path.join(fontDir, filename);
    assert.ok(existsSync(filePath), `missing font file ${filePath}`);
    assert.ok(readFileSync(filePath).byteLength > 1024, `empty font file ${filePath}`);
  }
  for (const [key, filename] of Object.entries(FONT_LICENCE_FILES)) {
    assert.ok(!/ricoh/i.test(filename), `licence ${key} must not reference ricoh`);
    const licPath = path.join(fontDir, filename);
    assert.ok(existsSync(licPath), `missing licence file ${licPath}`);
    const text = readFileSync(licPath, 'utf8');
    assert.ok(text.includes('SIL OPEN FONT LICENSE'), `unexpected licence header in ${filename}`);
  }
  assert.equal(GXR_MARK.w, 1951);
  assert.equal(GXR_MARK.h, 1240);
  assert.ok(ascentOf('atkinson', 1000) === 950);
});

test('CJK rejection helpers block >= U+2E80 and enforce 24-char cap', () => {
  assert.equal(CJK_REFUSE_MESSAGE, '无法显示中文。请改用英文或数字。');
  assert.equal(hasCjk('GR IIIx 28mm'), false);
  assert.equal(hasCjk('2026 上海。'), true);
  assert.equal(hasCjk('東京'), true);
  assert.equal(hasCjk('「test」'), true);
  assert.equal(stripCjk('GXR 世界 2026！'), 'GXR  2026');
  assert.equal(clipText('1234567890123456789012345678', 24).length, 24);
});

test('fontsNeeded collects frame & stamp OFL fonts without cjk', () => {
  const f = selectFrame(DEFAULT_FRAME, 'leica');
  const s = { ...DEFAULT_STAMP, enabled: true, font: 'seg7' };
  const keys = fontsNeeded(f, s);
  assert.ok(keys.includes('atkinson'));
  assert.ok(keys.includes('inter'));
  assert.ok(keys.includes('inter_sb'));
  assert.ok(keys.includes('dseg'));
  assert.ok(!keys.includes('cjk'));

  const polaroidEmpty = fontsNeeded(selectFrame(DEFAULT_FRAME, 'instant'), null);
  assert.deepEqual(polaroidEmpty, []);
  const polaroidWithDate = fontsNeeded({ ...selectFrame(DEFAULT_FRAME, 'instant'), show_date: true }, null);
  assert.deepEqual(polaroidWithDate, ['patrick']);
});

test('frame_spec normalization, style selection, and stamp summaries', () => {
  assert.equal(FRAME_ORDER.length, 13);
  for (const style of FRAME_ORDER) {
    assert.ok(FRAME_NAMES[style], `missing name for ${style}`);
    assert.ok(Array.isArray(STYLE_OPTIONS[style]), `missing options for ${style}`);
  }

  const leica = selectFrame(DEFAULT_FRAME, 'leica');
  assert.equal(leica.style, 'leica');
  assert.equal(leica.show_date, true);

  const slide = selectFrame(leica, 'slide_mount');
  assert.equal(slide.color, 'cream');

  const invalid = normalizeFrame({ version: 99, style: 'unknown' });
  assert.ok(sameFrameSpec(invalid, DEFAULT_FRAME));

  const noLogo = normalizeFrame({ ...leica, logo: 'none' });
  assert.equal(noLogo.show_logo, false);
  const reshow = normalizeFrame({ ...noLogo, logo: 'ricoh', show_logo: false });
  assert.equal(reshow.logo, 'none');

  assert.equal(isDateString('2026-11-16'), true);
  assert.equal(isDateString('2026-02-30'), false);
  assert.deepEqual(momentFromIso('2026-11-16T14:05:09'), [2026, 11, 16, 14, 5]);
  assert.equal(momentFromIso('invalid'), null);

  const st = normalizeStamp({ enabled: true, date_override: '2026-02-30', corner: 'tl', color: 'red', font: 'typewriter' });
  assert.equal(st.date_override, null);
  assert.equal(st.corner, 'tl');
  assert.ok(sameStampSpec(st, { ...st }));

  assert.equal(stampDateText("'yy mm dd", '2026-11-16', '14:05', true), "'26 11 16 14:05");
  assert.equal(stampDateText('yyyy.mm.dd', '2026-11-16', '', false), '2026.11.16');
  assert.equal(stampDateText("mm dd 'yy", '2026-11-16', '', false), "11 16 '26");

  const borderSummary = stampSummary({ ...DEFAULT_STAMP, placement: 'border' }, selectFrame(DEFAULT_FRAME, 'instant'));
  assert.ok(borderSummary.startsWith('拍立得边框上：'));
  const photoSummary = stampSummary({ ...DEFAULT_STAMP, placement: 'border' }, leica);
  assert.ok(photoSummary.startsWith('印在照片上：'));
});

test('pickLogoMode switches to mono on low-contrast transparent logos only', () => {
  assert.equal(pickLogoMode({ opaque: true, lum: 250 }, 'auto', 245), 'orig');
  assert.equal(pickLogoMode({ opaque: false, lum: 250 }, 'auto', 245), 'mono');
  assert.equal(pickLogoMode({ opaque: false, lum: 15 }, 'auto', 245), 'orig');
  assert.equal(pickLogoMode({ opaque: false, lum: 250 }, 'orig', 245), 'orig');
  assert.equal(pickLogoMode({ opaque: false, lum: 15 }, 'mono', 245), 'mono');
});

test('logo_key magic sniffing, edge-connected white keying, trim, and stats', () => {
  assert.equal(sniffImageMagic(Uint8Array.of(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0)), 'PNG');
  assert.equal(sniffImageMagic(Uint8Array.of(0xff, 0xd8, 0xff, 0xe0, 0, 0, 0, 0, 0, 0, 0, 0)), 'JPEG');
  assert.equal(sniffImageMagic(Uint8Array.of(0x52, 0x49, 0x46, 0x46, 0, 0, 0, 0, 0x57, 0x45, 0x42, 0x50)), 'WEBP');
  assert.equal(sniffImageMagic(Uint8Array.of(1, 2, 3, 4)), null);

  // 9x9 RGBA: outer ring white (255), middle ring mid-dark gray (130), center 3x3 enclosed white (255).
  const w = 9;
  const h = 9;
  const data = new Uint8ClampedArray(w * h * 4);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const o = (y * w + x) * 4;
      const inFrame = x >= 2 && x <= 6 && y >= 2 && y <= 6 && !(x >= 3 && x <= 5 && y >= 3 && y <= 5);
      const v = inFrame ? 130 : 255;
      data[o] = v;
      data[o + 1] = v;
      data[o + 2] = v;
      data[o + 3] = 255;
    }
  }
  const src = { width: w, height: h, data };
  assert.equal(isOpaqueImageData(src), true);
  assert.equal(hasWhiteBackground(src), true);

  const keyed = keyWhiteImageData(src, 30);
  // Corner border white becomes transparent:
  assert.equal(keyed.data[3], 0);
  // Enclosed interior white (4, 4) stays opaque!
  assert.equal(keyed.data[(4 * w + 4) * 4 + 3], 255);

  const box = trimBounds(keyed);
  assert.deepEqual(box, [2, 2, 5, 5]);
  const cropped = cropImageData(keyed, box);
  assert.equal(cropped.width, 5);
  assert.equal(cropped.height, 5);

  const stats = logoStats(cropped);
  assert.equal(stats.opaque, false);
  assert.equal(stemOf('/tmp/my.custom.logo-file.png'), 'my.custom.logo-file');
  assert.equal(cleanLogoName('  ', 'fallback_mark.png'), 'fallback_mark');
  assert.equal(cleanLogoName('A'.repeat(40), '').length, LOGO_LIMITS.nameMax);
});
