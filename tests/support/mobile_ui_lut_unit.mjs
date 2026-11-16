// UI-M4-LUT Step A 单元测试：色彩空间目录、记录构建、选择建议、配方+LUT 粘贴规则、出图请求与桌面文本核对。
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import test from 'node:test';
import vm from 'node:vm';

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

const {
  MAX_CUBE_BYTES,
  MAX_LUT_LIBRARY_BYTES,
  LUT_FOOTNOTE,
  LUT_ERRORS,
  DISPLAY_SPACES,
  ignoredFilesMessage,
  SPACES,
  SPACE_GROUPS,
  OUTPUT_GAMUTS,
  SOURCE_TAGS,
  isValidSpace,
  isDisplaySpace,
  lutNeedsRaw,
  cleanLutLabel,
  fileParts,
  classifyLutImports,
  commentStart,
  decodeCubeUtf8,
  readLutHeader,
  guessSpace,
  computeInputChoice,
  buildLutRecord,
  lutTargetSpec,
  lutRenderToken,
  deletedLutToken,
  sha256Hex,
} = await import('../../mobile/src/lib/lut.ts');

const { LutBook } = await import('../../mobile/src/app/lut_book.ts');
const { LutGpu } = await import('../../mobile/src/app/lut_gpu.ts');

const {
  copyFrom,
  groupsPresent,
  pasteOne,
  PasteUndo,
  planOne,
  recipeIsSet,
  restore,
  undoBatch,
} = await import('../../mobile/src/lib/paste.ts');

const pasteText = await import('../../mobile/src/lib/paste_text.ts');
const { buildRequest, buildSide } = await import('../../mobile/src/lib/export_request.ts');

const ROOT = new URL('../../', import.meta.url);
const LUT_CHOICES_JS = readFileSync(new URL('web/assets/lut_choices.js', ROOT), 'utf8');
const PASTE_CORE_JS = readFileSync(new URL('web/assets/paste_core.js', ROOT), 'utf8');
const LUT_LIBRARY_PY = readFileSync(new URL('src/gxr_studio/lut/library.py', ROOT), 'utf8');
const PASTE_PY = readFileSync(new URL('src/gxr_studio/render/paste.py', ROOT), 'utf8');

test('常量、错误文案与 03g 说明和规范/桌面逐字一致', () => {
  assert.equal(MAX_CUBE_BYTES, 64 << 20);
  assert.equal(MAX_LUT_LIBRARY_BYTES, 256 << 20);
  assert.equal(LUT_FOOTNOTE, 'LUT 只存在这台 iPhone 上，直接套在照片上，不写入相机。');
  assert.equal(ignoredFilesMessage(3), '3 个文件不是 .cube，没有导入。');
  assert.equal(LUT_ERRORS.library_full, 'LUT 太多了。删掉几个再导入。');
  assert.equal(LUT_ERRORS.duplicate, '已经导入过');
  assert.ok(LUT_LIBRARY_PY.includes(LUT_ERRORS.cube_not_found));
  assert.ok(LUT_LIBRARY_PY.includes(LUT_ERRORS.lut_space_required));
  assert.ok(LUT_LIBRARY_PY.includes(LUT_ERRORS.cube_unreadable));
  assert.ok(LUT_LIBRARY_PY.includes(LUT_ERRORS.cube_too_big));
  assert.ok(PASTE_PY.includes('这个 LUT 需要 RAW 文件，这张照片只有 JPG，没有套用 LUT。'));
  assert.ok(PASTE_PY.includes('图库里找不到这个 LUT，没有套用 LUT。'));
  assert.equal(SPACES.length, 12);
  assert.equal(SPACE_GROUPS[0].label, '普通照片与视频显示');
  assert.equal(SPACE_GROUPS[1].label, '相机 Log 与高级空间');
  assert.ok(LUT_CHOICES_JS.includes(SPACE_GROUPS[0].label));
  assert.ok(LUT_CHOICES_JS.includes(SPACE_GROUPS[1].label));
  assert.deepEqual(OUTPUT_GAMUTS, [
    ['', '默认'],
    ['rec709', 'Rec.709'],
    ['rec2020', 'Rec.2020'],
  ]);
  for (const tag of Object.values(SOURCE_TAGS)) {
    assert.ok(LUT_CHOICES_JS.includes(tag), tag);
  }
  assert.equal(isValidSpace('applelog'), true);
  assert.equal(isValidSpace('unknown'), false);
  assert.equal(isDisplaySpace('srgb'), true);
  assert.equal(isDisplaySpace('rec709'), true);
  assert.equal(isDisplaySpace('gamma24'), true);
  assert.equal(isDisplaySpace('applelog'), false);
  assert.equal(lutNeedsRaw(null), false);
  assert.equal(lutNeedsRaw('srgb'), false);
  assert.equal(lutNeedsRaw('applelog'), true);
});

test('fileParts、classifyLutImports、cleanLutLabel 与 readLutHeader', () => {
  assert.deepEqual(fileParts('__MACOSX/._foo.cube'), { name: '._foo.cube', group: '__MACOSX', skip: true });
  assert.deepEqual(fileParts('packs/film/warm.cube'), { name: 'warm.cube', group: 'packs', skip: false });
  const batch = classifyLutImports([
    { name: 'a.cube' },
    { name: 'b.CUBE' },
    { name: 'note.txt' },
    { name: 'photo.jpg' },
    { name: '__MACOSX/._a.cube' },
  ]);
  assert.equal(batch.cubes.length, 2);
  assert.equal(batch.ignored, 2);
  assert.equal(batch.skipped, 1);

  assert.equal(cleanLutLabel('  Warm   Film  '), 'Warm Film');
  assert.throws(() => cleanLutLabel('   '), /1–255/);
  assert.throws(() => cleanLutLabel('bad\x00name'), /名称无效/);

  assert.equal(commentStart('TITLE "Film #1" # trailing'), 16);
  const hdr = readLutHeader([
    '# Author: Studio Demo',
    '# Source: https://example.com',
    '# License: MIT',
    '# Input: S-Log3',
    'TITLE "My # LUT" # inline note',
  ].join('\n'));
  assert.equal(hdr.author, 'Studio Demo');
  assert.equal(hdr.source, 'https://example.com');
  assert.equal(hdr.license, 'MIT');
  assert.deepEqual(hdr.comments, ['Input: S-Log3', 'inline note']);
});

test('buildLutRecord、lutTargetSpec、lutRenderToken 与 sha256Hex', () => {
  const sample = new TextEncoder().encode('TITLE "Warm #1"\nLUT_3D_SIZE 2\n');
  const expectedSha = crypto.createHash('sha256').update(sample).digest('hex');
  assert.equal(sha256Hex(sample), expectedSha);

  const rec = buildLutRecord({
    cubeId: expectedSha,
    filename: 'dir/Warm_1.cube',
    byteLength: sample.byteLength,
    parsed: { title: 'Warm #1', kind: '3D', size_1d: 0, size_3d: 2 },
    identified: {
      input_space: 'srgb',
      input_space_state: 'declared',
      input_space_manual: false,
      output_space: 'srgb',
      output_space_state: 'unknown',
      technical: false,
      space_evidence: { input_declarations: ['srgb'] },
    },
    header: { source: '', author: '', license: '', comments: [] },
    savedAt: 10,
  });
  assert.equal(rec.name, 'Warm #1');
  assert.equal(rec.kind, '3D');
  assert.equal(rec.size_3d, 2);
  assert.equal(rec.input_range, 'full');
  assert.equal(rec.output_range, 'full');

  const spec = lutTargetSpec(rec);
  assert.deepEqual(spec, {
    input_space: 'srgb',
    output_space: 'srgb',
    input_gamut: null,
    output_gamut: null,
    input_range: 'full',
    output_range: 'full',
  });
  const token1 = lutRenderToken(rec);
  const token2 = lutRenderToken({ ...rec, output_gamut: 'rec2020' });
  assert.equal(token1.length, 16);
  assert.notEqual(token1, token2);
  assert.notEqual(deletedLutToken(expectedSha), token1);
  assert.equal(lutTargetSpec({ ...rec, input_space: null }), null);
});

test('computeInputChoice：declared / guessed / fallback / manual / conflict / hint', () => {
  assert.equal(
    computeInputChoice({
      input_space: 'applelog',
      input_space_state: 'manual',
      input_space_manual: true,
      filename: 'a.cube',
      title: '',
      comments: [],
      space_evidence: {},
    }).default_input_source,
    'manual',
  );
  assert.equal(
    computeInputChoice({
      input_space: 'srgb',
      input_space_state: 'declared',
      input_space_manual: false,
      filename: 'a.cube',
      title: '',
      comments: [],
      space_evidence: { input_declarations: ['sRGB'] },
    }).default_input_source,
    'declared',
  );
  assert.equal(
    computeInputChoice({
      input_space: null,
      input_space_state: 'conflict',
      input_space_manual: false,
      filename: 'a.cube',
      title: '',
      comments: [],
      space_evidence: {},
    }).default_input_source,
    'required',
  );
  const fallback = computeInputChoice({
    input_space: null,
    input_space_state: 'unknown',
    input_space_manual: false,
    filename: 'warm_look.cube',
    title: 'Warm Look',
    comments: [],
    space_evidence: { content: 'display' },
  });
  assert.equal(fallback.default_input_space, 'srgb');
  assert.equal(fallback.default_input_source, 'fallback');

  const [guessed, reason] = guessSpace('Apple_Log_to_Rec709.cube', '');
  assert.equal(guessed, 'applelog');
  assert.ok(reason?.includes('Apple_Log'));
});

function knobs(over = {}) {
  return {
    exposure_ev: 0,
    contrast: 0,
    highlight: 0,
    shadow: 0,
    black_point: 0,
    warm: 0,
    tint: 0,
    saturation: 0,
    vibrance: 0,
    vignette: 0,
    ...over,
  };
}

function photo(id, over = {}) {
  return {
    id,
    rev: 0,
    choice: null,
    lut: null,
    levels: new Map(),
    memory: new Map(),
    knobs: knobs(),
    ...over,
  };
}

test('配方组携带 LUT：recipeIsSet、互相清空、all-or-nothing 与撤销', () => {
  const onlyLut = photo('s', { lut: 'a'.repeat(64) });
  assert.equal(recipeIsSet(onlyLut), true);
  assert.deepEqual(groupsPresent(onlyLut), ['recipe']);
  const clipLut = copyFrom(onlyLut, 'S', ['recipe', 'adjust']).clip;
  assert.ok(clipLut);
  assert.equal(clipLut.recipe, null);
  assert.equal(clipLut.lut, 'a'.repeat(64));

  const env = {
    recipeWithoutRaw: false,
    listed: () => true,
    lutNeedsRaw: (id) => (id === 'l'.repeat(64) ? true : id === 'm'.repeat(64) ? 'missing' : false),
  };

  // 目标原来有配方：只复制 LUT 过来 → 配方被清空，只剩 LUT。
  const t1 = photo('t1', {
    choice: { id: 'builtin:2', unit: 'jlaunch' },
    levels: new Map([['builtin:2', { amount: 50 }]]),
  });
  const res1 = pasteOne({ photo: t1, raw: true, unit: 'jlaunch' }, clipLut, ['recipe'], env);
  assert.equal(res1.outcome.status, 'applied');
  assert.equal(t1.choice, null);
  assert.equal(t1.lut, 'a'.repeat(64));
  assert.equal(restore(t1, { rev: t1.rev, before: res1.before }), 'restored');
  assert.equal(t1.choice?.id, 'builtin:2');
  assert.equal(t1.lut, null);

  // 来源有配方 + Log LUT，目标只有 JPG → 配方组整体跳过（needs_raw，both parts reported 或至少 recipe 整组不变）。
  const srcBoth = photo('sb', {
    choice: { id: 'builtin:3', unit: 'jlaunch' },
    lut: 'l'.repeat(64),
    levels: new Map([['builtin:3', { amount: 80 }]]),
  });
  const clipBoth = copyFrom(srcBoth, 'SB', ['recipe', 'adjust']).clip;
  const jpgTarget = photo('tj', { choice: { id: 'builtin:1', unit: 'jlaunch' }, lut: 'k'.repeat(64) });
  const rJpg = pasteOne({ photo: jpgTarget, raw: false, unit: 'jlaunch' }, clipBoth, ['recipe'], env);
  assert.equal(rJpg.outcome.status, 'skipped');
  assert.equal(rJpg.before, null);
  assert.equal(jpgTarget.choice?.id, 'builtin:1');
  assert.equal(jpgTarget.lut, 'k'.repeat(64));

  // 来源有配方（jlaunch）+ 显示 LUT（无需 RAW），目标换了单元（qlaunch）→ recipe unit_mismatch，整个配方组都不写入，LUT 也不写！
  const srcDisp = photo('sd', {
    choice: { id: 'builtin:3', unit: 'jlaunch' },
    lut: 'a'.repeat(64),
  });
  const clipDisp = copyFrom(srcDisp, 'SD', ['recipe']).clip;
  const qTarget = photo('tq', { lut: null });
  const rUnit = pasteOne({ photo: qTarget, raw: true, unit: 'qlaunch' }, clipDisp, ['recipe'], env);
  assert.equal(rUnit.outcome.status, 'skipped');
  assert.equal(rUnit.outcome.skipped[0].reason, 'unit_mismatch');
  assert.equal(rUnit.outcome.skipped[0].part, 'recipe');
  assert.equal(qTarget.lut, null);

  // 来源有配方 + 已删 LUT → lut_missing，整个配方组都不写。
  const srcMissing = photo('sm', {
    choice: { id: 'builtin:3', unit: 'jlaunch' },
    lut: 'm'.repeat(64),
  });
  const clipMissing = copyFrom(srcMissing, 'SM', ['recipe']).clip;
  const tGood = photo('tg', { choice: null, lut: null });
  const rMiss = pasteOne({ photo: tGood, raw: true, unit: 'jlaunch' }, clipMissing, ['recipe'], env);
  assert.equal(rMiss.outcome.status, 'skipped');
  assert.equal(rMiss.outcome.skipped[0].reason, 'lut_missing');
  assert.equal(rMiss.outcome.skipped[0].part, 'lut');
  assert.equal(tGood.choice, null);

  // 撤销批还原 LUT
  const undo = new PasteUndo();
  const rDispOk = pasteOne({ photo: tGood, raw: true, unit: 'jlaunch' }, clipDisp, ['recipe'], env);
  undo.add('b1', tGood.id, rDispOk.outcome.rev, rDispOk.before);
  assert.equal(tGood.lut, 'a'.repeat(64));
  assert.equal(undoBatch(undo.take('b1'), () => tGood).counts.restored, 1);
  assert.equal(tGood.lut, null);
  assert.equal(tGood.choice, null);
});

test('export_request：side.lut 与 Log LUT 不解相机 JPG（needsJpg = false）', () => {
  const basePhoto = {
    stem: 'R0000001',
    dngName: 'R0000001.DNG',
    jpgName: 'R0000001.JPG',
    lens: '50mm',
    iso: 200,
    choice: null,
    lut: 'a'.repeat(64),
    lutInputSpace: 'srgb',
    levels: new Map(),
    memory: new Map(),
    knobs: knobs(),
    sourceModified: 1_760_000_000_000,
  };
  const side = buildSide(basePhoto, []);
  assert.equal(side.lut, 'a'.repeat(64));
  const reqDisp = buildRequest(basePhoto, [], { size: 'original', quality: 'high' }, 'dev');
  assert.equal(reqDisp.needsJpg, true);
  assert.equal(reqDisp.side.lut, 'a'.repeat(64));

  const reqLog = buildRequest({ ...basePhoto, lutInputSpace: 'applelog' }, [], { size: 'original', quality: 'high' }, 'dev');
  assert.equal(reqLog.needsJpg, false);
});

test('pasteMessage：lut_missing 与 needs_raw (part: lut) 和桌面逐字相同', () => {
  const start = PASTE_CORE_JS.indexOf('const SKIP_LINES = {');
  const end = PASTE_CORE_JS.indexOf('\nexport function undoMessage', start);
  const code = PASTE_CORE_JS.slice(start, end).replace(/^export /gm, '') + '\n({ pasteMessage })';
  const desk = vm.runInContext(code, vm.createContext({ Map, Array }));
  const cases = [
    [{ id: '1', status: 'skipped', applied: [], skipped: [{ group: 'recipe', reason: 'lut_missing', part: 'lut', message: 'x' }] }],
    [{ id: '1', status: 'skipped', applied: [], skipped: [{ group: 'recipe', reason: 'needs_raw', part: 'lut', message: 'x' }] }],
    [
      { id: '1', status: 'applied', applied: ['recipe'], skipped: [] },
      { id: '2', status: 'skipped', applied: [], skipped: [{ group: 'recipe', reason: 'lut_missing', part: 'lut', message: 'x' }] },
      { id: '3', status: 'skipped', applied: [], skipped: [{ group: 'recipe', reason: 'needs_raw', part: 'lut', message: 'x' }] },
    ],
  ];
  for (const c of cases) {
    assert.equal(pasteText.pasteMessage(c), desk.pasteMessage(JSON.parse(JSON.stringify(c))));
  }
});

function makeRec(id, over = {}) {
  return buildLutRecord({
    cubeId: id,
    filename: `${id.slice(0, 6)}.cube`,
    byteLength: 128,
    parsed: { title: over.name ?? id.slice(0, 6), kind: '3D', size_1d: 0, size_3d: 2 },
    identified: {
      input_space: 'srgb',
      input_space_state: 'declared',
      output_space: 'srgb',
      output_space_state: 'unknown',
      space_evidence: { input_declarations: ['sRGB'] },
      ...over,
    },
    header: { source: '', author: '', license: '', comments: [] },
    savedAt: 1,
  });
}

test('P3-01：decodeCubeUtf8 拒绝非法 UTF-8 字节并去除 UTF-8 BOM', () => {
  assert.equal(decodeCubeUtf8(new Uint8Array([0xff, 0xfe, 0x00, 0x41])), null);
  const bom = new Uint8Array([0xef, 0xbb, 0xbf, ...new TextEncoder().encode('# Input: sRGB\nTITLE "BOM"\n')]);
  assert.equal(decodeCubeUtf8(bom), '# Input: sRGB\nTITLE "BOM"\n');
  assert.equal(decodeCubeUtf8(new TextEncoder().encode('# Input: Rec.709\n')), '# Input: Rec.709\n');
});

test('P1-01：DISPLAY_SPACES 对齐桌面 display 空间，setSpace 未传 output_space 时保留原值', async () => {
  assert.deepEqual(
    DISPLAY_SPACES.map((s) => s.id),
    ['srgb', 'rec709', 'gamma24'],
  );
  const idA = '1'.repeat(64);
  const store = new Map([[idA, makeRec(idA, { output_space: 'rec709', output_space_state: 'declared' })]]);
  const book = new LutBook({
    importLut: async () => {
      throw new Error('unused');
    },
    restore: async () => ({ records: [...store.values()], order: [idA], totalBytes: 128, watermark: 1 }),
    patch: async (cubeId, patch, version) => {
      const prev = store.get(cubeId);
      if (!prev) return { ok: false, reason: 'not_found' };
      const next = { ...prev, ...patch, version: prev.version + 1, savedAt: version };
      store.set(cubeId, next);
      return { ok: true, record: next };
    },
    remove: async () => true,
    readBlob: async () => null,
  });
  await book.restore();

  const kept = await book.setSpace(idA, {
    input_space: 'srgb',
    input_range: 'legal',
    output_gamut: 'rec2020',
  });
  assert.equal(kept.ok, true);
  assert.equal(kept.record.output_space, 'rec709');
  assert.equal(kept.record.input_range, 'legal');
  assert.equal(kept.record.output_gamut, 'rec2020');

  const changed = await book.setSpace(idA, {
    input_space: 'srgb',
    output_space: 'gamma24',
  });
  assert.equal(changed.ok, true);
  assert.equal(changed.record.output_space, 'gamma24');
});

test('P2-02：exportSnapshot 在延迟 readBlob(A) + 并发 setSpace(B)/remove(B) 时仍保持 B 的快照一致', async () => {
  const idA = 'a'.repeat(64);
  const idB = 'b'.repeat(64);
  const store = new Map([
    [idA, makeRec(idA, { input_space: 'srgb', output_space: 'srgb' })],
    [idB, makeRec(idB, { input_space: 'srgb', output_space: 'srgb' })],
  ]);
  const blobA = new TextEncoder().encode('# A').buffer;
  const blobB = new TextEncoder().encode('# B').buffer;
  const blobs = new Map([
    [idA, blobA],
    [idB, blobB],
  ]);

  let releaseA;
  const waitA = new Promise((r) => {
    releaseA = r;
  });

  const book = new LutBook({
    importLut: async () => {
      throw new Error('unused');
    },
    restore: async () => ({ records: [...store.values()], order: [idA, idB], totalBytes: 256, watermark: 1 }),
    patch: async (cubeId, patch, version) => {
      const prev = store.get(cubeId);
      if (!prev) return { ok: false, reason: 'not_found' };
      const next = { ...prev, ...patch, version: prev.version + 1, savedAt: version };
      store.set(cubeId, next);
      return { ok: true, record: next };
    },
    remove: async (cubeId) => {
      blobs.delete(cubeId);
      return store.delete(cubeId);
    },
    readBlob: async (cubeId) => {
      if (cubeId === idA) await waitA;
      return blobs.get(cubeId) ?? null;
    },
  });
  await book.restore();

  const snapTask = book.exportSnapshot([idA, idB]);
  const patchTask = book.setSpace(idB, { input_space: 'applelog' });
  const removeTask = patchTask.then(() => book.remove(idB));
  await removeTask;
  releaseA();

  const snap = await snapTask;
  assert.ok(snap[idA]);
  assert.ok(snap[idB]);
  assert.equal(snap[idB].cube, blobB);
  assert.equal(JSON.parse(snap[idB].space).input_space, 'srgb');
});

test('P2-03：IDB AbortError 时 LutBook 不改动内存目录且返回清晰中文错误', async () => {
  const idA = 'c'.repeat(64);
  const recA = makeRec(idA, { name: 'KeepMe', input_space: 'srgb', output_space: 'rec709' });
  const abortErr = () => Object.assign(new Error('Transaction aborted'), { name: 'AbortError' });
  const book = new LutBook({
    importLut: async () => {
      throw abortErr();
    },
    restore: async () => ({ records: [recA], order: [idA], totalBytes: 128, watermark: 1 }),
    patch: async () => {
      throw abortErr();
    },
    remove: async () => {
      throw abortErr();
    },
    readBlob: async () => null,
  });
  await book.restore();

  const rem = await book.removeChecked(idA);
  assert.equal(rem.ok, false);
  assert.equal(rem.message, '删除没有成功，请再试一次。');
  assert.equal(await book.remove(idA), false);
  assert.equal(book.list().length, 1);
  assert.equal(book.bytes(), 128);

  const ren = await book.rename(idA, 'Renamed');
  assert.equal(ren.ok, false);
  assert.equal(ren.message, '保存未生效，请重试。');
  assert.equal(book.get(idA)?.name, 'KeepMe');

  const sp = await book.setSpace(idA, { input_space: 'applelog' });
  assert.equal(sp.ok, false);
  assert.equal(sp.message, '保存未生效，请重试。');
  assert.equal(book.get(idA)?.input_space, 'srgb');

  const imp = await book.importFiles([new File(['not saved'], 'abort.cube')]);
  assert.equal(imp.added.length, 0);
  assert.equal(imp.errors.length, 1);
  assert.equal(book.list().length, 1);
});

test('P2-04：LutGpu.clear() 同步终止短命 Worker、拒绝过期结果并清零 GPU 纹理槽', async () => {
  const luts = new Map();
  let clearLutsCalled = 0;
  const engine = {
    hasLut: (t) => luts.has(t),
    addLut: (t, _meta, _data) => luts.set(t, true),
    clearLuts: () => {
      clearLutsCalled += 1;
      luts.clear();
    },
  };

  let cancelledCalls = 0;
  let workerResolvers = [];
  const gpu = new LutGpu(() => engine, {
    engine: () => engine,
    readBlob: async () => new Uint8Array([1, 2, 3, 4]).buffer,
    runWorker: () => {
      let resolve;
      const promise = new Promise((r) => {
        resolve = r;
      });
      workerResolvers.push(resolve);
      return {
        promise,
        cancel: () => {
          cancelledCalls += 1;
          resolve({ ok: false, reason: 'cancelled', message: '已取消', wasmBytes: 0 });
        },
      };
    },
  });

  const rec1 = makeRec('1'.repeat(64));
  const rec2 = makeRec('2'.repeat(64));
  const rec3 = makeRec('3'.repeat(64));

  const p1 = gpu.ensure(rec1);
  await new Promise((r) => setTimeout(r, 0));
  workerResolvers[0]({
    ok: true,
    metaJson: JSON.stringify({ n: 2, table8: false }),
    data: new ArrayBuffer(2 * 2 * 2 * 16),
    wasmBytes: 1024,
  });
  assert.equal((await p1).ok, true);
  assert.equal(gpu.stats().gpuSlots, 1);

  // 启动 rec2（处于 runWorker 中）并排队 rec3，然后立刻 clear()
  const p2 = gpu.ensure(rec2);
  const p3 = gpu.ensure(rec3);
  await new Promise((r) => setTimeout(r, 0));
  gpu.clear();

  const st = gpu.stats();
  assert.equal(clearLutsCalled, 1);
  assert.equal(cancelledCalls, 1);
  assert.equal(st.alive, 0);
  assert.equal(st.terminated, st.spawned);
  assert.equal(st.pending, 0);
  assert.equal(st.gpuSlots, 0);
  assert.equal(st.gpuBytes, 0);
  assert.equal(luts.size, 0);

  // 即使晚到的完成回调尝试返回成功，也不能重新污染引擎或统计
  workerResolvers[1]?.({
    ok: true,
    metaJson: JSON.stringify({ n: 2, table8: false }),
    data: new ArrayBuffer(2 * 2 * 2 * 16),
    wasmBytes: 1024,
  });
  const [r2, r3] = await Promise.all([p2, p3]);
  assert.equal(r2.ok, false);
  assert.equal(r3.ok, false);
  assert.equal(luts.size, 0);
  assert.equal(gpu.stats().gpuSlots, 0);
  assert.equal(gpu.stats().pending, 0);
});
