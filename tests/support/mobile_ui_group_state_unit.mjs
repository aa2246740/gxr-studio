// UI-M3-GROUP：app/paste_state.ts 的 PasteState（Sol r1）：结果栏离开网格 15 秒后收起（结果在离开之后才到也一样），
// 以及按时间片套用时片之间目标变了不写入。Node 跑 TypeScript；定时器和 Date 用 node:test 的 mock。

import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import test, { mock } from 'node:test';

// mobile/src 里的 import 不带扩展名（vite 的写法）；Node 找不到时补上 .ts。
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
globalThis.window = globalThis;

const { PasteState, UNDO_MS } = await import('../../mobile/src/app/paste_state.ts');
const { copyFrom } = await import('../../mobile/src/lib/paste.ts');

const knobs = (over = {}) => ({ exposure_ev: 0, contrast: 0, highlight: 0, shadow: 0, black_point: 0, warm: 0, tint: 0, saturation: 0, vibrance: 0, vignette: 0, ...over });
const photo = (id, over = {}) => ({ id, stem: id.toUpperCase(), version: 1, rev: 0, dng: {}, jpg: null, jpgMeta: null, dngMeta: null, lastModified: 0, captureMs: 0, choice: null, levels: new Map(), memory: new Map(), knobs: knobs(), ...over });

function setup(ids = ['a']) {
  const byId = new Map(ids.map((id) => [id, photo(id)]));
  const api = { session: () => 0, photo: (id) => byId.get(id), book: () => ({ dat: null }) };
  const state = new PasteState();
  state.clip = copyFrom(photo('src', { knobs: knobs({ exposure_ev: 0.7 }) }), 'SRC', ['adjust']).clip;
  return { state, api, byId };
}

test('先离开网格、结果后到：从离开那一刻起 15 秒收起', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 1_000_000 });
  const { state, api, byId } = setup();
  const done = state.apply(api, [byId.get('a')], '10:00');
  state.gridLeft();
  await done;
  assert.equal(state.bar?.kind, 'applied');
  t.mock.timers.tick(UNDO_MS - 1);
  assert.equal(state.bar?.kind, 'applied');
  t.mock.timers.tick(1);
  assert.equal(state.bar, null);
});

test('离开 10 秒后结果才到：只再留 5 秒', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 1_000_000 });
  const { state, api, byId } = setup();
  state.gridLeft();
  t.mock.timers.tick(10_000);
  await state.apply(api, [byId.get('a')], '10:00');
  t.mock.timers.tick(4_999);
  assert.equal(state.bar?.kind, 'applied');
  t.mock.timers.tick(1);
  assert.equal(state.bar, null);
});

test('先有结果再离开：15 秒收起；中途回到网格就不收', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 1_000_000 });
  const first = setup();
  await first.state.apply(first.api, [first.byId.get('a')], '10:00');
  first.state.gridLeft();
  t.mock.timers.tick(UNDO_MS);
  assert.equal(first.state.bar, null);
  const second = setup();
  await second.state.apply(second.api, [second.byId.get('a')], '10:00');
  second.state.gridLeft();
  t.mock.timers.tick(5_000);
  second.state.gridShown();
  t.mock.timers.tick(60_000);
  assert.equal(second.state.bar?.kind, 'applied');
});

test('按时间片套用：片之间 B 被编辑，B 不被改回，结果句照实说', async () => {
  const { state, api, byId } = setup(['a', 'b']);
  state.budgetMs = 0;
  const b = byId.get('b');
  // 排在第一片的让出之前：第一张写完、第二张还没写时改 B。
  setTimeout(() => {
    b.knobs.exposure_ev = 3;
    b.rev += 1;
  }, 0);
  const results = await state.apply(api, [byId.get('a'), b], '10:00');
  assert.deepEqual(results.map((r) => [r.id, r.code ?? r.status]), [['a', 'applied'], ['b', 'rev_conflict']]);
  assert.equal(b.knobs.exposure_ev, 3);
  assert.equal(b.rev, 1);
  assert.equal(state.bar?.line, '已套用到 1 张；1 张在别处改过，没有粘贴');
});
