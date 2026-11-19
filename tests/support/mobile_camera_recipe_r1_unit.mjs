// Fix round 1 unit tests: each test fails on a27fc0c and passes after the corresponding r1 fix.
// Covers P1-1, P2-1, P3-1, P3-2, P3-3, and P3-4 (a-e).

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

function makeNode(tag) {
  const node = {
    tag,
    className: '',
    textContent: '',
    type: '',
    min: '',
    max: '',
    step: '',
    value: '',
    disabled: false,
    title: '',
    parentElement: null,
    style: { setProperty() {} },
    dataset: {},
    attrs: {},
    children: [],
    listeners: {},
    setAttribute(k, v) {
      this.attrs[k] = String(v);
      if (k === 'class') this.className = String(v);
    },
    getAttribute(k) {
      return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
    },
    append(...kids) {
      for (const kid of kids) {
        if (kid && typeof kid === 'object') kid.parentElement = this;
        this.children.push(kid);
      }
    },
    replaceChildren(...kids) {
      this.children = [];
      if (kids.length) this.append(...kids);
    },
    addEventListener(type, fn) {
      (this.listeners[type] ??= []).push(fn);
    },
    removeEventListener(type, fn) {
      this.listeners[type] = (this.listeners[type] ?? []).filter((item) => item !== fn);
    },
    click() {
      for (const fn of [...(this.listeners.click ?? [])]) fn({ target: this });
    },
    closest() {
      return null;
    },
    querySelectorAll(sel) {
      const found = [];
      const walk = (n) => {
        for (const kid of n.children ?? []) {
          if (kid.tag === sel) found.push(kid);
          walk(kid);
        }
      };
      walk(this);
      return found;
    },
  };
  node.classList = {
    add(name) {
      const parts = new Set(node.className.split(/\s+/).filter(Boolean));
      parts.add(name);
      node.className = [...parts].join(' ');
    },
    toggle(name, on) {
      const parts = new Set(node.className.split(/\s+/).filter(Boolean));
      if (on) parts.add(name);
      else parts.delete(name);
      node.className = [...parts].join(' ');
      return on;
    },
    contains(name) {
      return node.className.split(/\s+/).includes(name);
    },
  };
  return node;
}

globalThis.document = {
  createElement: (tag) => makeNode(tag),
  createElementNS: (_ns, tag) => makeNode(tag),
  createTextNode: (text) => ({ tag: '#text', textContent: String(text) }),
};

const apiMod = await import('../../mobile/src/core/api.ts');
const exifMod = await import('../../mobile/src/lib/exif.ts');
const recipesMod = await import('../../mobile/src/lib/recipes.ts');
const pasteMod = await import('../../mobile/src/lib/paste.ts');
const modelMod = await import('../../mobile/src/app/model.ts');
const adjustMod = await import('../../mobile/src/app/adjust_view.ts');
const curvesMod = await import('../../mobile/src/app/curves_view.ts');
const { zeroGeometry } = await import('../../mobile/src/lib/crop.ts');
const { DEFAULT_FRAME, DEFAULT_STAMP } = await import('../../mobile/src/lib/frame_spec.ts');

function makePhoto(overrides = {}) {
  return {
    id: 'p1',
    stem: 'R0000001',
    version: 1,
    dng: { name: 'R0000001.DNG' },
    jpg: { name: 'R0000001.JPG' },
    diptych: false,
    lastModified: 0,
    captureMs: null,
    status: 'ready',
    gone: false,
    cached: null,
    checked: null,
    bitmap: null,
    neutral: null,
    knobs: modelMod.zeroKnobs(),
    curve: null,
    jpgMeta: null,
    dngMeta: null,
    border: { ...DEFAULT_FRAME },
    stamp: { ...DEFAULT_STAMP },
    frameMeta: null,
    frame: null,
    geometry: zeroGeometry(),
    crop: null,
    rev: 0,
    choice: null,
    levels: new Map(),
    memory: new Map(),
    lut: null,
    looks: new Map(),
    ...overrides,
  };
}

function makeBook(dat = { read: { bytes: new ArrayBuffer(8), library: null } }) {
  return { dat };
}

test('P1-1: core/api metaOf preserves EXIF Copyright from describeDng worker metadata', () => {
  assert.equal(typeof apiMod.metaOf, 'function', 'core/api must export metaOf');
  const padded = 'GXR1 0123456789 28 06'.padEnd(69, ' ');
  assert.deepEqual(apiMod.metaOf({ lens: 'GR LENS A12 28mm', iso: 200, copyright: padded }), {
    lens: 'GR LENS A12 28mm',
    iso: 200,
    copyright: padded,
  });
  assert.deepEqual(apiMod.metaOf({ lens: 'GR LENS A12 28mm', iso: 200 }), {
    lens: 'GR LENS A12 28mm',
    iso: 200,
  });
});

test('P2-1: mergeCopyright is JPG-first with Python is-not-None semantics (empty string wins over DNG)', () => {
  assert.equal(exifMod.mergeCopyright('jpg-tag', 'dng-tag'), 'jpg-tag');
  assert.equal(exifMod.mergeCopyright('', 'dng-tag'), '');
  assert.equal(exifMod.mergeCopyright(null, 'dng-tag'), 'dng-tag');
  assert.equal(exifMod.mergeCopyright(undefined, 'dng-tag'), 'dng-tag');
  assert.equal(exifMod.mergeCopyright(null, null), null);
});

test('P2-1: ensureCameraRecipe skips DNG head read when JPG has Copyright and memoizes (dat, tag)', async () => {
  let headReads = 0;
  modelMod.registerCameraRecipeReader(async () => {
    headReads += 1;
    return new ArrayBuffer(16);
  });
  const tag = 'GXR1 0123456789 28 06';
  let resolves = 0;
  const resolve = async () => {
    resolves += 1;
    return {
      status: 'ok',
      recipe: { kind: 'record', unit: 'jlaunch', slot: 6, id: 'rec6', levels: { amount: 100 } },
      levelsUnknown: false,
    };
  };
  const book = makeBook();
  const p1 = makePhoto({ id: 'p1', jpgMeta: { lens: 'GR LENS A12 28mm', iso: 200, copyright: tag } });
  const p2 = makePhoto({ id: 'p2', jpgMeta: { lens: 'GR LENS A12 28mm', iso: 200, copyright: tag } });
  const pEmpty = makePhoto({ id: 'p3', jpgMeta: { lens: 'GR LENS A12 28mm', iso: 200, copyright: '' } });

  await modelMod.ensureCameraRecipe(p1, book, { resolve });
  await modelMod.ensureCameraRecipe(p2, book, { resolve });
  await modelMod.ensureCameraRecipe(pEmpty, book, { resolve });

  assert.equal(headReads, 0, 'must never read DNG header when JPG has Copyright (including empty string)');
  assert.equal(resolves, 1, 'shared (dat, tag) must be memoized across photos');
  assert.equal(recipesMod.adoptedSelectionOf(p1, book.dat)?.choice.id, 'record:rec6');
  assert.equal(recipesMod.adoptedSelectionOf(p2, book.dat)?.choice.id, 'record:rec6');
  assert.equal(recipesMod.adoptedSelectionOf(pEmpty, book.dat), null);
});

test('P3-1: cameraRecipeErrorOf surfaces missing-DAT error for tagged RAW export note while keeping choice null', async () => {
  const tag = 'GXR1 0123456789 28 06';
  const photo = makePhoto({ jpgMeta: { lens: 'GR LENS A12 28mm', iso: 200, copyright: tag } });
  const book = makeBook(null);
  let calledWithNullDat = false;
  const resolve = async (_tag, dat) => {
    if (dat === null) calledWithNullDat = true;
    return {
      status: 'error',
      error: { code: 'camera_recipe_catalog_missing', message: '请先导入拍摄时使用的相机配方文件。' },
    };
  };
  await modelMod.ensureCameraRecipe(photo, book, { checkMissingDat: true, resolve });
  assert.equal(calledWithNullDat, true, 'missing-DAT export check must invoke resolver with dat=null');
  assert.deepEqual(recipesMod.cameraRecipeErrorOf(photo, null), {
    code: 'camera_recipe_catalog_missing',
    message: '请先导入拍摄时使用的相机配方文件。',
  });
  assert.equal(recipesMod.adoptedSelectionOf(photo, null), null);
  assert.equal(recipesMod.photoWithAdopted(photo, null).choice, null);
});

test('P3-2: editor error text appends desktop suffix', () => {
  assert.equal(typeof recipesMod.formatCameraRecipeEditorError, 'function');
  assert.equal(
    recipesMod.formatCameraRecipeEditorError('请先导入拍摄时使用的相机配方文件。'),
    '请先导入拍摄时使用的相机配方文件。 照片调整仍可保存，暂不生成拍摄配方预测。',
  );
});

test('P3-3: pasting LUT-only clip onto untouched tagged RAW turns adopted recipe off and undo restores it', async () => {
  const dat = { generation: 250 };
  const key = recipesMod.datKeyOf(dat);
  const target = makePhoto({
    id: 'target',
    cameraRecipe: {
      datKey: key,
      copyright: 'GXR1 0123456789 28 06',
      version: 1,
      result: {
        status: 'ok',
        recipe: { kind: 'record', unit: 'jlaunch', slot: 6, id: 'rec6', levels: {} },
        levelsUnknown: false,
      },
    },
  });
  const source = makePhoto({ id: 'src', choice: null, lut: 'cube-abc' });
  const { clip } = pasteMod.copyFrom(source, 'SRC', ['recipe']);
  assert.ok(clip, 'LUT-only source produces clip');
  assert.equal(clip.recipe, null);
  assert.equal(clip.lut, 'cube-abc');
  assert.equal(recipesMod.adoptedSelectionOf(target, dat)?.choice.id, 'record:rec6');

  const env = {
    listed: () => true,
    recipeWithoutRaw: false,
    lutNeedsRaw: () => false,
  };
  const { outcome, before } = pasteMod.pasteOne({ photo: target, raw: true, unit: 'jlaunch' }, clip, ['recipe'], env);
  assert.equal(outcome.status, 'applied');
  assert.equal(target.lut, 'cube-abc');
  assert.equal(target.recipeOff, true, 'LUT-only paste must explicitly turn adopted recipe off');
  assert.equal(recipesMod.adoptedSelectionOf(target, dat), null, 'adopted recipe must be off after LUT-only paste');
  assert.equal(recipesMod.photoWithAdopted(target, dat).choice, null, 'export/copy view must not re-adopt');

  // Undo restores adoption back on.
  const status = pasteMod.restore(target, { rev: target.rev, before });
  assert.equal(status, 'restored');
  assert.equal(target.lut, null);
  assert.equal(Boolean(target.recipeOff), false);
  assert.equal(recipesMod.adoptedSelectionOf(target, dat)?.choice.id, 'record:rec6', 'undo must restore adoption');

  // Async paste path also sets recipeOff and restores cleanly.
  const asyncRes = await pasteMod.pasteOneAsync({ photo: target, raw: true, unit: 'jlaunch' }, clip, ['recipe'], env);
  assert.equal(asyncRes.outcome.status, 'applied');
  assert.equal(target.recipeOff, true);
  assert.equal(recipesMod.adoptedSelectionOf(target, dat), null);
  assert.equal(pasteMod.restore(target, { rev: target.rev, before: asyncRes.before }), 'restored');
  assert.equal(recipesMod.adoptedSelectionOf(target, dat)?.choice.id, 'record:rec6');
});

test('P3-4a: recipeUnitOf picks explicit choice, then adopted recipe, then lens unit', () => {
  assert.equal(typeof recipesMod.recipeUnitOf, 'function');
  const adopted = { choice: { id: 'record:rec6', unit: 'qlaunch' }, levels: {}, levelsUnknown: false };
  assert.equal(recipesMod.recipeUnitOf(null, adopted, 'jlaunch'), 'qlaunch');
  assert.equal(recipesMod.recipeUnitOf({ id: 'builtin:1', unit: 'l08firm' }, adopted, 'jlaunch'), 'l08firm');
  assert.equal(recipesMod.recipeUnitOf(null, null, 'jlaunch'), 'jlaunch');
  assert.equal(recipesMod.recipeUnitOf(null, null, null), 'jlaunch');
});

test('P3-4b: plain structured cameraRecipe worker op replaces hex smuggling and @camera-recipe: prefix', () => {
  assert.equal(typeof apiMod.cameraRecipe, 'function', 'core/api must export cameraRecipe');
  assert.equal(typeof recipesMod.parseCameraRecipeResult, 'function', 'lib/recipes must export parseCameraRecipeResult');
  assert.equal('CAMERA_RECIPE_PREFIX' in recipesMod, false, 'CAMERA_RECIPE_PREFIX smuggling must be removed');
  assert.equal('encodeCameraRecipePayload' in recipesMod, false, 'hex encoder must be removed');
  assert.deepEqual(
    recipesMod.parseCameraRecipeResult(
      JSON.stringify({
        recipe: { kind: 'record', unit: 'jlaunch', slot: 6, id: 'rec6', levels: { contrast: 1 } },
        levels_unknown: true,
      }),
    ),
    {
      status: 'ok',
      recipe: { kind: 'record', unit: 'jlaunch', slot: 6, id: 'rec6', levels: { contrast: 1 } },
      levelsUnknown: true,
    },
  );
  assert.deepEqual(recipesMod.parseCameraRecipeResult(JSON.stringify({ recipe: null })), { status: 'none' });
  assert.deepEqual(
    recipesMod.parseCameraRecipeResult(JSON.stringify({ error: { code: 'camera_recipe_catalog_missing', message: 'msg' } })),
    { status: 'error', error: { code: 'camera_recipe_catalog_missing', message: 'msg' } },
  );
});

test('P3-4c: AdjustView reset invokes host.refreshCurves directly and CurvesView exposes refresh()', () => {
  const photo = makePhoto({
    curve: { master: [[0, 0], [128, 170], [255, 255]] },
  });
  let refreshed = 0;
  const view = new adjustMod.AdjustView(
    {
      photo: () => photo,
      push: () => undefined,
      enterDrag: () => undefined,
      leaveDrag: () => undefined,
      dragging: () => false,
      refreshCurves: () => {
        refreshed += 1;
      },
    },
    makeNode('div'),
  );
  const reset = view.panelEl().querySelectorAll('button').find((b) => b.textContent === '全部重置');
  assert.ok(reset);
  reset.click();
  assert.equal(refreshed, 1, '全部重置 must invoke host.refreshCurves() directly');
  assert.equal(typeof curvesMod.CurvesView.prototype.refresh, 'function', 'CurvesView must expose refresh()');
});

test('P3-4d: clearCameraRecipe helper lives in model.ts and clears all camera-recipe cache fields', () => {
  assert.equal(typeof modelMod.clearCameraRecipe, 'function', 'model.ts must export clearCameraRecipe');
  const photo = makePhoto({
    dngCopyright: 'GXR1 0123456789 28 06',
    dngCopyrightRead: 1,
    cameraRecipe: { datKey: 'dat:1', copyright: 'GXR1 0123456789 28 06', version: 1, result: { status: 'none' } },
  });
  modelMod.clearCameraRecipe(photo);
  assert.equal(photo.cameraRecipe, null);
  assert.equal(photo.dngCopyright, null);
  assert.equal(photo.dngCopyrightRead, null);
});

test('P3-4e: transient worker errors are not cached as none and next request retries', async () => {
  const book = makeBook();
  const tag = 'GXR1 9999999999 28 06';
  const photo = makePhoto({ jpgMeta: { lens: 'GR LENS A12 28mm', iso: 200, copyright: tag } });

  let attempts = 0;
  const flakyResolve = async () => {
    attempts += 1;
    if (attempts === 1) throw new Error('worker crashed');
    return {
      status: 'ok',
      recipe: { kind: 'record', unit: 'jlaunch', slot: 6, id: 'rec6', levels: {} },
      levelsUnknown: false,
    };
  };

  await modelMod.ensureCameraRecipe(photo, book, { resolve: flakyResolve });
  assert.equal(photo.cameraRecipe ?? null, null, 'transient worker failure must not cache { status: none }');
  assert.equal(modelMod.needsCameraRecipe(photo, book.dat), true, 'next request must still know recipe needs resolution');

  await modelMod.ensureCameraRecipe(photo, book, { resolve: flakyResolve });
  assert.equal(attempts, 2, 'second request must retry after transient failure');
  assert.equal(recipesMod.adoptedSelectionOf(photo, book.dat)?.choice.id, 'record:rec6');
});
