// UI-M4：裁剪与拉直（lib/crop.ts、lib/crop_gesture.ts）和桌面 web/assets/crop.js 的预设/参考线比对、
// 手势状态机（移动、角边缩放、锁定比例、自由比例、拉直边界约束、双指缩放）、以及编辑状态提交/撤销规则。

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import test from 'node:test';

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
  centeredMaxRect,
  cropIsPortrait,
  fitStraightened,
  geometrySidecar,
  GUIDE_PREFS_KEY,
  GUIDES,
  guideShapes,
  insideStraightened,
  loadGuidePrefs,
  lockRatio,
  MAX_ANGLE,
  ORIENTABLE,
  PRESETS,
  presetLabel,
  presetMatchesSpec,
  rectFromArray,
  sameCropState,
  saveGuidePrefs,
  sideSubset,
  zeroGeometry,
} = await import('../../mobile/src/lib/crop.ts');
const { CropGesture } = await import('../../mobile/src/lib/crop_gesture.ts');

const ROOT = new URL('../../', import.meta.url);
const DESKTOP_CROP = readFileSync(new URL('web/assets/crop.js', ROOT), 'utf8');

test('预设顺序、标签、参考线和 localStorage 键和桌面 crop.js 一致', () => {
  assert.equal(PRESETS.map((p) => p.label).join('、'), '原始、自由、1:1、4:5、3:2、16:9、XPan 65:24、小红书 3:4');
  assert.deepEqual(
    PRESETS.map((p) => [p.id, p.orient ?? null]),
    [
      ['original', null],
      ['free', null],
      ['1:1', null],
      ['4:5', 'portrait'],
      ['3:2', null],
      ['16:9', null],
      ['xpan-65-24', null],
      ['4:3', 'portrait'],
    ],
  );
  assert.equal(GUIDE_PREFS_KEY, 'gxr-studio.crop-guides');
  assert.ok(DESKTOP_CROP.includes("'gxr-studio.crop-guides'") || DESKTOP_CROP.includes('"gxr-studio.crop-guides"'));
  assert.equal(MAX_ANGLE, 45);
  for (const [id, label] of GUIDES) {
    assert.ok(DESKTOP_CROP.includes(`'${id}'`) || DESKTOP_CROP.includes(`"${id}"`), id);
    assert.ok(DESKTOP_CROP.includes(label), label);
  }
  for (const id of ORIENTABLE) {
    assert.ok(DESKTOP_CROP.includes(`'${id}'`) || DESKTOP_CROP.includes(`"${id}"`), id);
  }
});

test('参考线偏好读写和坏值兜底、presetLabel / presetMatchesSpec', () => {
  const mem = new Map();
  const store = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, v) };
  assert.equal(loadGuidePrefs(store).guide, 'thirds');
  saveGuidePrefs({ guide: 'golden' }, store);
  assert.equal(loadGuidePrefs(store).guide, 'golden');
  mem.set(GUIDE_PREFS_KEY, '{bad');
  assert.equal(loadGuidePrefs(store).guide, 'thirds');

  const canvas = { w: 4000, h: 3000 };
  const xhsSpec = PRESETS[PRESETS.length - 1];
  assert.equal(presetLabel('4:3', { x: 0.2, y: 0, w: 0.5625, h: 1 }, canvas), '小红书 3:4');
  assert.equal(presetLabel('4:3', { x: 0, y: 0, w: 1, h: 1 }, canvas), '4:3');
  assert.equal(presetMatchesSpec(xhsSpec, '4:3', { x: 0.2, y: 0, w: 0.5625, h: 1 }, canvas), true);
  assert.equal(presetMatchesSpec(xhsSpec, '4:3', { x: 0, y: 0, w: 1, h: 1 }, canvas), false);
  assert.equal(cropIsPortrait({ x: 0.2, y: 0, w: 0.5, h: 1 }, canvas), true);
});

test('guideShapes 生成各参考线几何', () => {
  assert.equal(guideShapes('off', 0, 1.5).lines.length, 0);
  assert.equal(guideShapes('thirds', 0, 1.5).lines.length, 4);
  assert.equal(guideShapes('grid', 0, 1.5).lines.length, 6);
  assert.equal(guideShapes('golden', 0, 1.5).lines.length, 4);
  assert.equal(guideShapes('diagonal', 0, 1.5).lines.length, 2);
  assert.ok(guideShapes('spiral', 0, 1.5).path.startsWith('M'));
});

test('CropGesture：移动夹在画面内且不越过拉直边界', () => {
  const canvas = { w: 4288, h: 2848 };
  const box = centeredMaxRect(canvas, 65 / 24, 0, [65, 24]);
  const crop = { preset: 'xpan-65-24', rect: [box.x, box.y, box.w, box.h], angle_deg: 0 };
  const g = new CropGesture();
  g.startMove({ x: 0.5, y: 0.5 }, crop, canvas);
  assert.equal(g.mode, 'move');

  const moved = g.move({ x: 0.5, y: 0.1 }, canvas, 0);
  assert.ok(moved.y >= 0 && moved.y < box.y);
  assert.equal(moved.x, box.x);

  const clamped = g.move({ x: 5, y: -5 }, canvas, 0);
  assert.equal(clamped.y, 0);
  assert.equal(clamped.x, 0);

  // 拉直 10° 时，拖动也不能出旋转后的安全边界
  const r10 = g.move({ x: 0.5, y: 0 }, canvas, 10);
  assert.equal(insideStraightened(r10, canvas, 10), true);
  const out = g.end();
  assert.ok(Array.isArray(out) && out.length === 4);
  assert.equal(g.mode, 'idle');
});

test('CropGesture：角和边缩放在锁定比例时保持像素宽高比，自由模式解开比例', () => {
  const canvas = { w: 4000, h: 3000 };
  const xpan = centeredMaxRect(canvas, 65 / 24, 0, [65, 24]);
  const crop = { preset: 'xpan-65-24', rect: [xpan.x, xpan.y, xpan.w, xpan.h], angle_deg: 0 };

  const g = new CropGesture();
  g.startResize('br', { x: xpan.x + xpan.w, y: xpan.y + xpan.h }, crop, canvas, 'xpan-65-24');
  assert.equal(g.mode, 'resize');
  const shrunk = g.move({ x: 0.6, y: xpan.y + 0.2 }, canvas, 0);
  const ratio = (shrunk.w * canvas.w) / (shrunk.h * canvas.h);
  assert.ok(Math.abs(ratio - 65 / 24) < 1e-5, `got ratio ${ratio}`);
  assert.equal(shrunk.x, xpan.x);
  assert.equal(shrunk.y, xpan.y);

  // 拖右边缩放（仍锁比例，保持垂直居中与锁定比）
  const c2 = { preset: 'xpan-65-24', rect: [shrunk.x, shrunk.y, shrunk.w, shrunk.h], angle_deg: 0 };
  g.startResize('r', { x: shrunk.x + shrunk.w, y: shrunk.y + shrunk.h / 2 }, c2, canvas, 'xpan-65-24');
  const widened = g.move({ x: 1.5, y: 0.5 }, canvas, 0);
  assert.equal(insideStraightened(widened, canvas, 0), true);
  const ratio2 = (widened.w * canvas.w) / (widened.h * canvas.h);
  assert.ok(Math.abs(ratio2 - 65 / 24) < 1e-5, `got ratio2 ${ratio2}`);

  // 自由比例：宽高独立改变
  const freeCrop = { preset: 'free', rect: [0.1, 0.1, 0.6, 0.6], angle_deg: 0 };
  g.startResize('br', { x: 0.7, y: 0.7 }, freeCrop, canvas, 'free');
  assert.equal(lockRatio('free', rectFromArray(freeCrop.rect), canvas), null);
  const freeRes = g.move({ x: 0.85, y: 0.35 }, canvas, 0);
  assert.ok(Math.abs(freeRes.w - 0.75) < 1e-6);
  assert.ok(Math.abs(freeRes.h - 0.25) < 1e-6);
});

test('CropGesture：双指捏合不越界且保持比例', () => {
  const canvas = { w: 4000, h: 3000 };
  const xpan = centeredMaxRect(canvas, 65 / 24, 0, [65, 24]);
  const crop = { preset: 'xpan-65-24', rect: [xpan.x, xpan.y, xpan.w, xpan.h], angle_deg: 0 };
  const g = new CropGesture();
  g.startPinch({ x: 0.4, y: 0.5 }, { x: 0.6, y: 0.5 }, crop, canvas, 'xpan-65-24');
  const pinched = g.pinch({ x: 0.45, y: 0.5 }, { x: 0.55, y: 0.5 }, canvas, 5);
  assert.equal(insideStraightened(pinched, canvas, 5), true);
  const ratio = (pinched.w * canvas.w) / (pinched.h * canvas.h);
  assert.ok(Math.abs(ratio - 65 / 24) < 1e-4, `pinched ratio ${ratio}`);
  g.end();
});

test('编辑状态提交与松手回弹：无变化不加 rev，有效提交才加 rev 并吸附到核心返回框', () => {
  const canvas = { w: 4288, h: 2848 };
  const fitted = fitStraightened({ x: 0.1, y: 0.2, w: 0.8, h: 0.44 }, canvas, 1.5);
  assert.equal(fitted.changed, false);

  const photo = {
    frame: { orientation: 1, width: 4288, height: 2848 },
    geometry: zeroGeometry(),
    crop: null,
    rev: 0,
  };
  // sideSubset 给 Worker 传的格式
  assert.deepEqual(sideSubset(photo), {
    frame: { orientation: 1, width: 4288, height: 2848 },
    geometry: null,
    crop: null,
  });
  assert.deepEqual(geometrySidecar(0), null);
  assert.deepEqual(geometrySidecar(1.5), { straighten: 1.5, vertical: 0, horizontal: 0, distortion: 0, rotate90: 0, flip: false });

  // 模拟 applyCommitReply：no-op 不加 rev，有变化加 rev
  function applyReply(reply) {
    const nextAngle = reply.geometry?.straighten ?? 0;
    const nextCrop = reply.crop;
    const changed = nextAngle !== photo.geometry.straighten || !sameCropState(photo.crop, nextCrop);
    if (changed) {
      photo.geometry = { straighten: nextAngle };
      photo.crop = nextCrop;
      photo.rev += 1;
    }
    return changed;
  }

  assert.equal(applyReply({ geometry: null, crop: null }), false);
  assert.equal(photo.rev, 0);

  const snapCrop = { preset: 'xpan-65-24', rect: [0, 0.222086, 1, 0.555829], angle_deg: 1.5 };
  assert.equal(applyReply({ geometry: geometrySidecar(1.5), crop: snapCrop }), true);
  assert.equal(photo.rev, 1);
  assert.equal(photo.geometry.straighten, 1.5);
  assert.deepEqual(photo.crop, snapCrop);

  // 重复提交相同结果（松手没动）：不加 rev
  assert.equal(applyReply({ geometry: geometrySidecar(1.5), crop: snapCrop }), false);
  assert.equal(photo.rev, 1);
});
