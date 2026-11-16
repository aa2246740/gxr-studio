// UI-M3-GROUP：500 张合成照片套用一次，量主线程上每一片花了多久（规则：每帧不超过 8 ms）。
// 只输出一行 JSON，断言在 Python 那边（墙钟断言要标 exclusive）。

import { copyFrom, pasteBatch, PasteUndo } from '../../mobile/src/lib/paste.ts';

const knobs = (over = {}) => ({ exposure_ev: 0, contrast: 0, highlight: 0, shadow: 0, black_point: 0, warm: 0, tint: 0, saturation: 0, vibrance: 0, vignette: 0, ...over });
// 来源：22 张配方卡各记着档位（全部 22 的情况），曝光和对比度动过。
const memory = new Map(Array.from({ length: 22 }, (_, i) => [`look:${i}`, { amount: 80, contrast: 2, fade: 1, saturation: -1 }]));
const source = { id: 'src', rev: 0, choice: { id: 'look:3', unit: 'jlaunch' }, levels: new Map([['look:3', { amount: 80 }]]), memory, knobs: knobs({ exposure_ev: 0.7, contrast: -10 }) };
const { clip } = copyFrom(source, 'R0000000', ['recipe', 'adjust']);
const photos = Array.from({ length: 500 }, (_, i) => ({
  id: `p${i}`,
  rev: 0,
  choice: { id: 'builtin:1', unit: 'jlaunch' },
  levels: new Map([['builtin:1', { amount: 50 }]]),
  memory: new Map(),
  knobs: knobs({ warm: i % 7 }),
}));
const targets = photos.map((photo, i) => ({ photo, raw: i % 10 !== 0, unit: 'jlaunch' }));
const slices = [];
const env = { recipeWithoutRaw: false, listed: () => true };
const started = performance.now();
const results = await pasteBatch(targets, clip, clip.groups, env, new PasteUndo(), 'timing', {
  budgetMs: 4,
  yieldFn: () => new Promise((done) => setImmediate(done)),
  onSlice: (ms) => slices.push(ms),
});
const total = performance.now() - started;
console.log(
  JSON.stringify({
    photos: photos.length,
    results: results.length,
    applied: results.filter((r) => r.applied.length).length,
    recipe: results.filter((r) => r.applied.includes('recipe')).length,
    slices: slices.length,
    maxSliceMs: Math.max(...slices),
    workMs: slices.reduce((a, b) => a + b, 0),
    wallMs: total,
  }),
);
