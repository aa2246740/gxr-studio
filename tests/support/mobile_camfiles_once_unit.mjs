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

test('book_camfiles: corrupt stored record is tombstoned on restore so next launch skips readDat', async () => {
  const { CamFilesController } = await import('../../mobile/src/app/book_camfiles.ts');
  let stored = {
    dat: new Uint8Array(64).buffer,
    datName: 'GXRCE.DAT',
    library: null,
    generation: 266,
    libraryHash: null,
    catalogId: null,
    savedAt: 1000,
  };
  let readDatCalls = 0;
  let deleteCalls = [];
  const io = {
    async readFile() { return null; },
    async readDat() {
      readDatCalls += 1;
      throw new Error('corrupt dat');
    },
    async loadCamFiles() {
      return stored && stored.dat ? stored : null;
    },
    async deleteCamFiles(at) {
      deleteCalls.push(at);
      if (at > stored.savedAt) {
        stored = { ...stored, dat: null, datName: '', library: null, savedAt: at };
        return true;
      }
      return false;
    },
    async camFilesWatermark() {
      return stored.savedAt;
    },
  };
  const host = { onBump() {}, onChanged() {} };

  const c1 = new CamFilesController(io, host);
  await c1.restore();
  assert.equal(c1.dat, null);
  assert.equal(readDatCalls, 1);
  assert.equal(deleteCalls.length, 1, 'corrupt restore must tombstone IndexedDB row');
  assert.ok(deleteCalls[0] > 1000);
  assert.equal(stored.dat, null);

  // Next launch: row is tombstoned, so readDat is never called again.
  const c2 = new CamFilesController(io, host);
  await c2.restore();
  assert.equal(c2.dat, null);
  assert.equal(readDatCalls, 1, 'next launch must not re-parse tombstoned corrupt bytes');
});

test('book: catalog mismatch makes usableDatFor/shelvesFor null and unitStateFor non-ready for that photo', async () => {
  const { Book } = await import('../../mobile/src/app/book.ts');
  const { datKeyOf } = await import('../../mobile/src/lib/recipes.ts');
  const book = new Book({
    readFile: async () => null,
    readDat: async () => { throw new Error('unused'); },
    importCalib: async () => { throw new Error('unused'); },
    restoreCalib: async () => null,
    bake: async () => { throw new Error('unused'); },
    resolve: async () => { throw new Error('unused'); },
    calibCheck: async () => ({ model_iso: 200, ctx: 'c', ring: 'r' }),
    deletePack: async () => true,
    watermark: async () => 0,
  });
  const listing = {
    units: [
      {
        id: 'jlaunch',
        recipes: [{ slot: 4, name: '原厂标准', kind: 'builtin', number: '04', controls: [{ name: 'amount', label: '强度', default: 100, levels: [0, 100] }] }],
        looks: [],
        users: [],
      },
    ],
  };
  book.dat = {
    name: 'GXRCE.DAT',
    read: { bytes: new ArrayBuffer(16), entries: new Uint8Array(24 * 552), summary: '{}', listing: '{}', library: null },
    generation: 158,
    listing,
    library: null,
    libraryHash: null,
    libraryCatalogId: null,
    savedAt: 1,
  };
  const key = datKeyOf(book.dat);
  const mismatchedPhoto = {
    dng: {},
    diptych: false,
    choice: null,
    recipeOff: false,
    jpgMeta: { copyright: 'GXR1 0ccc86c19b 50 68 879999999-' },
    dngMeta: null,
    cameraRecipe: { datKey: key, copyright: 'GXR1 0ccc86c19b 50 68 879999999-', version: 1, result: { status: 'error', error: { code: 'camera_recipe_catalog_missing', message: '请先导入拍摄时使用的相机配方文件。' } } },
  };
  const matchingPhoto = {
    dng: {},
    diptych: false,
    choice: null,
    recipeOff: false,
    jpgMeta: { copyright: null },
    dngMeta: null,
    cameraRecipe: { datKey: key, copyright: null, version: 1, result: { status: 'none' } },
  };
  assert.equal(book.usableDatFor(mismatchedPhoto), null);
  assert.equal(book.shelvesFor(mismatchedPhoto, 'jlaunch'), null);
  assert.notEqual(book.unitStateFor(mismatchedPhoto, 'jlaunch').state, 'ready');
  assert.equal(book.usableDatFor(matchingPhoto), book.dat);
  assert.ok(book.shelvesFor(matchingPhoto, 'jlaunch'));
});
