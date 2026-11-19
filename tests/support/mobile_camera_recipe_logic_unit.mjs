// 手机端拍摄配方解析逻辑单元测试：EXIF Copyright (0x8298)、采用覆盖、显式优先、JPG/双联不采用、角标与复制行为。

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

const { copyrightFromJpeg, copyrightFromTiff, mergeCopyright, photoMetaFromTiff } = await import('../../mobile/src/lib/exif.ts');
const {
  adoptedSelectionOf,
  cameraRecipeErrorOf,
  datKeyOf,
  parseCameraRecipeResult,
  photoWithAdopted,
} = await import('../../mobile/src/lib/recipes.ts');
const { copyFrom, groupsPresent } = await import('../../mobile/src/lib/paste.ts');
const { zeroKnobs } = await import('../../mobile/src/app/model.ts');
const { zeroGeometry } = await import('../../mobile/src/lib/crop.ts');
const { DEFAULT_FRAME, DEFAULT_STAMP } = await import('../../mobile/src/lib/frame_spec.ts');

/** Minimal little-endian TIFF with optional ASCII tags. */
function makeTiff(tags) {
  const count = tags.length;
  const ifdOffset = 8;
  const headerSize = ifdOffset + 2 + count * 12 + 4;
  const payloadParts = [];
  let offset = headerSize;
  const entries = [];
  for (const [tag, text] of tags) {
    const raw = Buffer.from(`${text}\0`, 'ascii');
    if (raw.length <= 4) {
      const inline = Buffer.alloc(4);
      raw.copy(inline);
      entries.push({ tag, type: 2, count: raw.length, value: inline.readUInt32LE(0) });
    } else {
      entries.push({ tag, type: 2, count: raw.length, value: offset });
      payloadParts.push(raw);
      offset += raw.length;
    }
  }
  const buf = Buffer.alloc(offset);
  buf.write('II', 0, 'ascii');
  buf.writeUInt16LE(42, 2);
  buf.writeUInt32LE(ifdOffset, 4);
  buf.writeUInt16LE(count, ifdOffset);
  entries.forEach((e, i) => {
    const p = ifdOffset + 2 + i * 12;
    buf.writeUInt16LE(e.tag, p);
    buf.writeUInt16LE(e.type, p + 2);
    buf.writeUInt32LE(e.count, p + 4);
    buf.writeUInt32LE(e.value, p + 8);
  });
  buf.writeUInt32LE(0, ifdOffset + 2 + count * 12);
  let pos = headerSize;
  for (const part of payloadParts) {
    part.copy(buf, pos);
    pos += part.length;
  }
  return new Uint8Array(buf);
}

function wrapJpegExif(tiffBytes) {
  const exifHead = Buffer.from('Exif\0\0', 'ascii');
  const segLen = 2 + exifHead.length + tiffBytes.length;
  const out = Buffer.alloc(2 + 2 + segLen + 2);
  let p = 0;
  out[p++] = 0xff;
  out[p++] = 0xd8;
  out[p++] = 0xff;
  out[p++] = 0xe1;
  out.writeUInt16BE(segLen, p);
  p += 2;
  exifHead.copy(out, p);
  p += exifHead.length;
  Buffer.from(tiffBytes).copy(out, p);
  p += tiffBytes.length;
  out[p++] = 0xff;
  out[p++] = 0xd9;
  return new Uint8Array(out);
}

function makePhoto(overrides = {}) {
  return {
    id: 'p1',
    stem: 'R0000001',
    version: 1,
    dng: {},
    jpg: {},
    diptych: false,
    lastModified: 0,
    captureMs: null,
    status: 'ready',
    gone: false,
    cached: null,
    checked: null,
    bitmap: null,
    neutral: null,
    knobs: zeroKnobs(),
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

test('EXIF Copyright 0x8298 preserves 69-char trailing spaces, merges JPG-first, and omits copyright key when absent', () => {
  const padded = 'GXR1 abcdef012345 28 03 01 02000000000000000000'.padEnd(69, ' ');
  assert.equal(padded.length, 69);
  const tiff = makeTiff([[0x8298, padded]]);
  assert.equal(copyrightFromTiff(tiff), padded);
  assert.equal(photoMetaFromTiff(tiff).copyright, padded);

  const jpeg = wrapJpegExif(tiff);
  assert.equal(copyrightFromJpeg(jpeg), padded);

  // Without Copyright tag, photoMetaFromTiff keeps exact { lens, iso } shape for existing deepEqual tests.
  const noCopyTiff = makeTiff([[0xc614, 'RICOH GR LENS A12 28mm']]);
  assert.deepEqual(photoMetaFromTiff(noCopyTiff), { lens: 'GR LENS A12 28mm', iso: null });
  assert.equal(copyrightFromTiff(noCopyTiff), null);

  // JPG takes priority over DNG (Python is-not-None semantics); falls back to DNG only when JPG is null/undefined.
  assert.equal(mergeCopyright('jpg-tag', 'dng-tag'), 'jpg-tag');
  assert.equal(mergeCopyright('', 'dng-tag'), '');
  assert.equal(mergeCopyright(null, 'dng-tag'), 'dng-tag');
  assert.equal(mergeCopyright(null, null), null);
});

test('parseCameraRecipeResult handles ok, none, and structured error JSON payloads', () => {
  assert.deepEqual(
    parseCameraRecipeResult(
      JSON.stringify({
        recipe: { kind: 'looks', unit: 'jlaunch', slot: 1, id: 'look-1', levels: { saturation: 2 } },
        levels_unknown: true,
      }),
    ),
    {
      status: 'ok',
      recipe: { kind: 'looks', unit: 'jlaunch', slot: 1, id: 'look-1', levels: { saturation: 2 } },
      levelsUnknown: true,
    },
  );

  assert.deepEqual(parseCameraRecipeResult(JSON.stringify({ recipe: null })), { status: 'none' });

  assert.deepEqual(
    parseCameraRecipeResult(
      JSON.stringify({ error: { code: 'camera_recipe_catalog_missing', message: '请先导入拍摄时使用的相机配方文件。' } }),
    ),
    {
      status: 'error',
      error: { code: 'camera_recipe_catalog_missing', message: '请先导入拍摄时使用的相机配方文件。' },
    },
  );
});

test('adoptedSelectionOf and photoWithAdopted match desktop rules without mutating photo.choice or grid badge', () => {
  const dat = { generation: 5 };
  const otherDat = { generation: 3 };
  const key = datKeyOf(dat);
  assert.ok(key);

  const photo = makePhoto({
    cameraRecipe: {
      datKey: key,
      copyright: 'GXR1 ...',
      version: 1,
      result: {
        status: 'ok',
        recipe: { kind: 'looks', unit: 'jlaunch', slot: 1, id: 'teal-id', levels: { contrast: 2 } },
        levelsUnknown: false,
      },
    },
  });

  // Untouched adopted photo: raw photo has choice === null and NO grid pencil badge.
  assert.equal(photo.choice, null);
  assert.deepEqual(groupsPresent(photo), []);

  // Adopted selection resolves for the matching DAT, not a different DAT.
  const adopted = adoptedSelectionOf(photo, dat);
  assert.deepEqual(adopted, {
    choice: { id: 'looks:teal-id', unit: 'jlaunch' },
    levels: { contrast: 2 },
    levelsUnknown: false,
  });
  assert.equal(adoptedSelectionOf(photo, otherDat), null);

  // Overlay view exposes adopted recipe for copy/export without mutating original photo.
  const view = photoWithAdopted(photo, dat);
  assert.notEqual(view, photo);
  assert.equal(photo.choice, null);
  assert.deepEqual(groupsPresent(view), ['recipe']);
  const copied = copyFrom(view, photo.stem, ['recipe']);
  assert.ok(copied.clip);
  assert.deepEqual(copied.clip.recipe.choice, { id: 'looks:teal-id', unit: 'jlaunch' });
  assert.deepEqual(copied.clip.recipe.levels, { contrast: 2 });

  // Explicit choice always wins over adoption.
  photo.choice = { id: 'builtin:4', unit: 'jlaunch' };
  assert.equal(adoptedSelectionOf(photo, dat), null);
  assert.equal(photoWithAdopted(photo, dat), photo);

  // JPG-only and P0 diptych never adopt.
  const jpgOnly = makePhoto({ dng: null, cameraRecipe: photo.cameraRecipe });
  assert.equal(adoptedSelectionOf(jpgOnly, dat), null);
  const diptych = makePhoto({ diptych: true, cameraRecipe: photo.cameraRecipe });
  assert.equal(adoptedSelectionOf(diptych, dat), null);

  // Error result surfaces only when choice === null and matching DAT.
  const errored = makePhoto({
    cameraRecipe: {
      datKey: key,
      copyright: 'GXR1 ...',
      version: 1,
      result: {
        status: 'error',
        error: { code: 'camera_recipe_catalog_missing', message: '请先导入拍摄时使用的相机配方文件。' },
      },
    },
  });
  assert.deepEqual(cameraRecipeErrorOf(errored, dat), {
    code: 'camera_recipe_catalog_missing',
    message: '请先导入拍摄时使用的相机配方文件。',
  });
  errored.choice = { id: 'builtin:4', unit: 'jlaunch' };
  assert.equal(cameraRecipeErrorOf(errored, dat), null);
});
