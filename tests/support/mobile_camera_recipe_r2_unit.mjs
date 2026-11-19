// Fix round 2 unit tests: camera-JPG fallback decode & retry predicates in export_request.ts.

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

const {
  NEEDS_CAMERA_JPG_MESSAGE,
  shouldDecodeCameraJpg,
  shouldRetryWithCameraJpg,
} = await import('../../mobile/src/lib/export_request.ts');

test('shouldDecodeCameraJpg decodes upfront when DAT+recipe export has no calibration pack', () => {
  assert.equal(
    shouldDecodeCameraJpg({
      hasJpg: true,
      needsJpg: false,
      planOriginal: false,
      isLogLut: false,
      isLocalRecipe: false,
      hasCalib: false,
      hasDat: true,
    }),
    true,
  );
});

test('shouldDecodeCameraJpg decodes upfront when DAT is missing on RAW+JPG recipe export', () => {
  assert.equal(
    shouldDecodeCameraJpg({
      hasJpg: true,
      needsJpg: false,
      planOriginal: false,
      isLogLut: false,
      isLocalRecipe: false,
      hasCalib: true,
      hasDat: false,
    }),
    true,
  );
});

test('shouldDecodeCameraJpg skips upfront decode on calibrated RAW+JPG recipe path', () => {
  assert.equal(
    shouldDecodeCameraJpg({
      hasJpg: true,
      needsJpg: false,
      planOriginal: false,
      isLogLut: false,
      isLocalRecipe: false,
      hasCalib: true,
      hasDat: true,
    }),
    false,
  );
});

test('shouldDecodeCameraJpg skips upfront decode for Log LUT, local recipe, and RAW-only', () => {
  assert.equal(
    shouldDecodeCameraJpg({
      hasJpg: true,
      needsJpg: false,
      planOriginal: false,
      isLogLut: true,
      isLocalRecipe: false,
      hasCalib: false,
      hasDat: true,
    }),
    false,
  );
  assert.equal(
    shouldDecodeCameraJpg({
      hasJpg: true,
      needsJpg: false,
      planOriginal: false,
      isLogLut: false,
      isLocalRecipe: true,
      hasCalib: false,
      hasDat: true,
    }),
    false,
  );
  assert.equal(
    shouldDecodeCameraJpg({
      hasJpg: false,
      needsJpg: false,
      planOriginal: false,
      isLogLut: false,
      isLocalRecipe: false,
      hasCalib: false,
      hasDat: true,
    }),
    false,
  );
});

test('shouldDecodeCameraJpg always decodes when needsJpg or planOriginal is true on RAW+JPG', () => {
  assert.equal(
    shouldDecodeCameraJpg({
      hasJpg: true,
      needsJpg: true,
      planOriginal: false,
      isLogLut: false,
      isLocalRecipe: false,
      hasCalib: true,
      hasDat: true,
    }),
    true,
  );
  assert.equal(
    shouldDecodeCameraJpg({
      hasJpg: true,
      needsJpg: false,
      planOriginal: true,
      isLogLut: false,
      isLocalRecipe: false,
      hasCalib: true,
      hasDat: true,
    }),
    true,
  );
});

test('shouldRetryWithCameraJpg triggers only once on core bad_request requesting camera JPG pixels', () => {
  assert.equal(
    shouldRetryWithCameraJpg({
      hasJpg: true,
      alreadyDecodedJpg: false,
      failKind: 'core',
      failCode: 'bad_request',
      failMessage: NEEDS_CAMERA_JPG_MESSAGE,
    }),
    true,
  );
  // Already decoded -> never retry infinitely.
  assert.equal(
    shouldRetryWithCameraJpg({
      hasJpg: true,
      alreadyDecodedJpg: true,
      failKind: 'core',
      failCode: 'bad_request',
      failMessage: NEEDS_CAMERA_JPG_MESSAGE,
    }),
    false,
  );
  // RAW-only -> cannot retry with camera JPG.
  assert.equal(
    shouldRetryWithCameraJpg({
      hasJpg: false,
      alreadyDecodedJpg: false,
      failKind: 'core',
      failCode: 'bad_request',
      failMessage: NEEDS_CAMERA_JPG_MESSAGE,
    }),
    false,
  );
  // Other errors (calibration_missing, raw_decode, unrelated bad_request) -> do not retry with camera JPG.
  assert.equal(
    shouldRetryWithCameraJpg({
      hasJpg: true,
      alreadyDecodedJpg: false,
      failKind: 'core',
      failCode: 'calibration_missing',
      failMessage: '无法生成电脑预测。请先导入校准数据。',
    }),
    false,
  );
  assert.equal(
    shouldRetryWithCameraJpg({
      hasJpg: true,
      alreadyDecodedJpg: false,
      failKind: 'core',
      failCode: 'bad_request',
      failMessage: '需要 DNG 文件',
    }),
    false,
  );
});
