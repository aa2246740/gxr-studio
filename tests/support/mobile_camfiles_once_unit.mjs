// camfiles-once 单元测试：库目录 ID 提取、不匹配判定规则、「未校准」文案拆分与过时提示清理。

import assert from 'node:assert/strict';
import test from 'node:test';
import { registerHooks } from 'node:module';

registerHooks({
  resolve(spec, ctx, next) {
    if (spec.endsWith('.css')) return { shortCircuit: true, url: 'data:text/javascript,export default ""' };
    try {
      return next(spec, ctx);
    } catch (err) {
      if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(`${spec}.ts`, ctx);
      throw err;
    }
  },
});

test('camfiles: extractLibraryCatalogId reads GXRFX.BIN[84:89] as lowercase 10-hex', async () => {
  const { extractLibraryCatalogId, camFilesRememberedLabel } = await import('../../mobile/src/lib/camfiles.ts');
  const buf = new Uint8Array(128);
  buf.set([0x0c, 0xcc, 0x86, 0xc1, 0x9b], 84);
  assert.equal(extractLibraryCatalogId(buf.buffer), '0ccc86c19b');
  assert.equal(extractLibraryCatalogId(buf), '0ccc86c19b');
  assert.equal(extractLibraryCatalogId(new Uint8Array(88).buffer), null);
  assert.equal(extractLibraryCatalogId(null), null);
  assert.equal(camFilesRememberedLabel(266), '已记住 · 第 266 代');
  assert.equal(camFilesRememberedLabel(null), '已记住');
});

test('camfiles: describeCamFilesMismatch distinguishes library gen, catalog id, unknown future id, and allows g262/g266 same-lib', async () => {
  const { describeCamFilesMismatch } = await import('../../mobile/src/lib/camfiles.ts');
  const { parseRecipeTag } = await import('../../mobile/src/lib/recipe_tags.ts');

  const kubikTag = parseRecipeTag('GXR1 0ccc86c19b 50 68 879999999-');
  const kubikBaseTag = parseRecipeTag('GXR1 0ccc86c19b 50 01 A99999999-');
  const g176Tag = parseRecipeTag('GXR1 d25d2f0a68 50 01');
  const futureTag = parseRecipeTag('GXR1 deadbeef01 50 06 A99999999-');

  // Same-library firmware update (g262 DAT + KUBIK lib serving g266 KUBIK photo): valid, not a mismatch.
  assert.equal(describeCamFilesMismatch(kubikTag, { generation: 262, libraryCatalogId: '0ccc86c19b' }), null);
  assert.equal(describeCamFilesMismatch(kubikTag, { generation: 266, libraryCatalogId: '0ccc86c19b' }), null);
  // No tag on photo: never a mismatch.
  assert.equal(describeCamFilesMismatch(null, { generation: 158, libraryCatalogId: null }), null);

  // Cross library generation: remembered single-file g158 or g176 vs g266 KUBIK photo.
  assert.equal(describeCamFilesMismatch(kubikTag, { generation: 158, libraryCatalogId: null })?.reason, 'library_generation');
  assert.equal(describeCamFilesMismatch(kubikTag, { generation: 176, libraryCatalogId: 'd25d2f0a68' })?.reason, 'library_generation');
  assert.equal(describeCamFilesMismatch(g176Tag, { generation: 266, libraryCatalogId: '0ccc86c19b' })?.reason, 'library_generation');

  // Same library_generation (178) but different catalog_identity: non-KUBIK g185 / g250 vs KUBIK photo (both K-KINO and GRPos).
  assert.equal(describeCamFilesMismatch(kubikTag, { generation: 185, libraryCatalogId: '6a3a5a3c02' })?.reason, 'catalog_identity');
  assert.equal(describeCamFilesMismatch(kubikBaseTag, { generation: 250, libraryCatalogId: '6adf847602' })?.reason, 'catalog_identity');

  // Unknown / future catalog identity.
  assert.equal(describeCamFilesMismatch(futureTag, { generation: 266, libraryCatalogId: '0ccc86c19b' })?.reason, 'unknown_catalog');
});

test('recipe_band: splits no-calibration warning from ISO out-of-range neutral prediction and removes old copy', async () => {
  const { readFileSync } = await import('node:fs');
  const { recipeStatusSpec } = await import('../../mobile/src/app/recipe_band.ts');
  const base = {
    raw: true,
    dat: true,
    photoUnit: 'jlaunch',
    unit: 'jlaunch',
    iso: 200,
    state: { state: 'ready' },
    adopted: null,
    camError: null,
  };

  // No calibration at all -> warning tile 「未校准」
  const noCal = recipeStatusSpec({ ...base, state: { state: 'none' } }, false, false);
  assert.equal(noCal?.stateAttr, 'no-calibration');
  assert.equal(noCal?.tone, 'warn');
  assert.equal(noCal?.label, '未校准');

  // Calibrated, but photo ISO outside real-shot calibrated range (ISO 1250) -> neutral dim tile 「校准范围外（推算）」
  const isoOut = recipeStatusSpec({ ...base, iso: 1250 }, false, false);
  assert.equal(isoOut?.stateAttr, 'uncalibrated');
  assert.equal(isoOut?.tone, 'dim');
  assert.equal(isoOut?.label, '校准范围外（推算）');

  // Calibrated + datError -> still warns and preserves stateAttr='uncalibrated'
  const datErr = recipeStatusSpec(base, false, true);
  assert.equal(datErr?.stateAttr, 'uncalibrated');
  assert.equal(datErr?.tone, 'warn');

  // Misleading 「每张卡选一次就行」 must be removed from recipe_band.ts.
  const src = readFileSync(new URL('../../mobile/src/app/recipe_band.ts', import.meta.url), 'utf8');
  assert.equal(src.includes('每张卡选一次就行'), false, 'recipe_band.ts must not contain 「每张卡选一次就行」');
});
