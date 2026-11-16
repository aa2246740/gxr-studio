// UI-M3-GROUP：复制 / 套用到这一组的纯函数（lib/paste.ts）和字（lib/paste_text.ts）。
// 字和桌面逐句核对：从 web/assets/paste_core.js 把那几个纯函数的源码原样抠出来，在 vm 里跑，
// 和手机的函数对同一批输入比输出。桌面改了一个字，这里就失败。

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { registerHooks } from 'node:module';

// mobile/src 里的 import 不带扩展名（vite 的写法）；Node 找不到时补上 .ts（export_request.ts 要用到 calib.ts）。
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
import vm from 'node:vm';
import {
  adjustIsSet,
  copyFrom,
  groupsPresent,
  MAX_UNDO_BATCHES,
  pasteBatch,
  pasteOne,
  PasteUndo,
  planOne,
  restore,
  undoBatch,
} from '../../mobile/src/lib/paste.ts';
import * as text from '../../mobile/src/lib/paste_text.ts';
const { cleanLevels } = await import('../../mobile/src/lib/export_request.ts');
const { recipeSwitch } = await import('../../mobile/src/lib/recipe_memory.ts');

const ROOT = new URL('../../', import.meta.url);
const CORE = readFileSync(new URL('web/assets/paste_core.js', ROOT), 'utf8');
const BATCH = readFileSync(new URL('web/assets/paste_batch.js', ROOT), 'utf8');
const EDITS = readFileSync(new URL('web/assets/paste_edits.js', ROOT), 'utf8');
const PY = readFileSync(new URL('src/gxr_studio/render/paste.py', ROOT), 'utf8');

/** 桌面源码里的一段：从 `start` 那一行到第一个只有 `end` 的行（含）。 */
function block(source, start, end) {
  const at = source.indexOf(start);
  assert.ok(at >= 0, `desktop paste_core.js lost: ${start}`);
  const stop = source.indexOf(end, at);
  assert.ok(stop >= 0, `desktop paste_core.js: no end for ${start}`);
  return source.slice(at, stop + end.length).replace(/^export /gm, '');
}

function desktop() {
  const code = [
    block(CORE, 'export const PASTE_GROUPS = [', '\n];'),
    block(CORE, 'const DEFAULT_GROUPS = ', ';'),
    block(CORE, 'const GROUPS_KEY = ', ';'),
    block(CORE, 'export const groupName = ', ';\n'),
    block(CORE, 'export function chosenGroups()', '\n}\n'),
    block(CORE, 'export function rememberGroups(ids)', '\n}\n'),
    block(CORE, 'export function copyMessage(', '\n}\n'),
    block(CORE, 'const SKIP_LINES = {', '\n};'),
    block(CORE, 'const ERROR_LINES = {', '\n};'),
    block(CORE, 'export function pasteMessage(', '\n}\n'),
    block(CORE, 'export function undoMessage(', '\n}\n'),
    '({ PASTE_GROUPS, DEFAULT_GROUPS, GROUPS_KEY, groupName, chosenGroups, rememberGroups, copyMessage, pasteMessage, undoMessage })',
  ].join('\n');
  const store = new Map();
  const localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) };
  const ctx = vm.createContext({ localStorage, JSON, Map, Array });
  return { api: vm.runInContext(code, ctx), store, localStorage };
}

const D = desktop();
const plain = (value) => JSON.parse(JSON.stringify(value));
const ALL = text.PASTE_GROUPS.map((g) => g[0]);

test('六组的名字、说明、默认和存勾选的键和桌面一样', () => {
  assert.deepEqual(plain(text.PASTE_GROUPS), plain(D.api.PASTE_GROUPS));
  assert.deepEqual(text.DEFAULT_GROUPS, plain(D.api.DEFAULT_GROUPS));
  assert.equal(text.GROUPS_KEY, D.api.GROUPS_KEY);
  for (const id of [...ALL, 'other']) assert.equal(text.groupName(id), D.api.groupName(id));
  assert.ok(EDITS.includes('`${hint}' + text.NOT_SET + '`'), '这张没有设置');
});

test('记住勾选：桌面写的手机读得出，手机写的桌面读得出，坏的用默认', () => {
  const mine = new Map();
  const phoneStore = { getItem: (k) => (mine.has(k) ? mine.get(k) : null), setItem: (k, v) => mine.set(k, v) };
  assert.deepEqual(text.chosenGroups(phoneStore), plain(D.api.chosenGroups()));
  for (const saved of [['adjust', 'recipe'], ['stamp', 'nope', 'crop'], [], ['geometry']]) {
    D.api.rememberGroups(saved);
    mine.set(text.GROUPS_KEY, D.store.get(D.api.GROUPS_KEY));
    assert.deepEqual(text.chosenGroups(phoneStore), plain(D.api.chosenGroups()), JSON.stringify(saved));
    text.rememberGroups(saved, phoneStore);
    assert.equal(mine.get(text.GROUPS_KEY), D.store.get(D.api.GROUPS_KEY));
  }
  for (const bad of ['{', '"recipe"', '42', 'null']) {
    mine.set(text.GROUPS_KEY, bad);
    D.store.set(D.api.GROUPS_KEY, bad);
    assert.deepEqual(text.chosenGroups(phoneStore), plain(D.api.chosenGroups()), bad);
  }
  assert.deepEqual(text.chosenGroups(null), ['recipe', 'adjust']);
});

test('copyMessage 和桌面逐字相同', () => {
  const cases = [
    { copied: [], empty: [] },
    { copied: [], empty: ['recipe', 'adjust'] },
    { copied: ['recipe', 'adjust'], empty: [] },
    { copied: ['recipe'], empty: ['adjust'] },
    { copied: ['adjust'], empty: ['recipe', 'geometry', 'stamp'] },
  ];
  for (const c of cases) assert.equal(text.copyMessage(c), D.api.copyMessage(c), JSON.stringify(c));
  assert.equal(text.copyMessage({ copied: ['recipe', 'adjust'], empty: [] }), '已复制：配方 / 风格、调整');
});

const skip = (reason, group = 'recipe', part, message = '说明') => ({ group, reason, message, ...(part ? { part } : {}) });
const out = (id, applied, skipped = [], extra = {}) => ({ id, status: applied.length ? (skipped.length ? 'partial' : 'applied') : 'skipped', applied, skipped, ...extra });

test('pasteMessage 和桌面逐字相同：每种原因、合并计数、单张、出错', () => {
  const reasons = [
    skip('unit_mismatch', 'recipe', 'recipe'),
    skip('needs_raw', 'recipe', 'recipe'),
    skip('needs_raw', 'recipe', 'lut'),
    skip('lut_missing', 'recipe', 'lut'),
    skip('recipe_rejected', 'recipe', 'recipe'),
    skip('not_copied', 'adjust'),
    skip('not_copied', 'stamp'),
    skip('rejected', 'frame', undefined, '这个边框用不了。'),
  ];
  const cases = [[], [out('a', [])], [out('a', ['recipe', 'adjust'])], [out('a', ['adjust'], [reasons[1]])]];
  for (const r of reasons) {
    cases.push([out('a', ['adjust'], [r])]);
    cases.push([out('a', ['adjust'], [r]), out('b', ['adjust'], [r]), out('c', ['recipe', 'adjust'])]);
    cases.push([out('a', [], [r]), out('b', [], [r])]);
  }
  cases.push([out('a', ['recipe', 'adjust']), out('b', ['recipe', 'adjust']), out('c', ['recipe', 'adjust']), out('d', ['recipe', 'adjust']), out('e', ['adjust'], [reasons[1]])]);
  cases.push([out('a', ['adjust'], [reasons[0]]), out('b', ['adjust'], [reasons[1]]), out('c', ['adjust'], [reasons[0]])]);
  for (const code of ['rev_conflict', 'photo_not_found', 'sidecar_too_new', 'sidecar_unreadable', 'weird']) {
    const err = { id: 'x', status: 'error', applied: [], skipped: [], code, message: '读不了' };
    cases.push([err]);
    cases.push([err, { ...err, id: 'y' }, out('z', ['adjust'])]);
  }
  for (const c of cases) assert.equal(text.pasteMessage(c), D.api.pasteMessage(plain(c)), JSON.stringify(c));
  const five = [out('a', ['recipe', 'adjust']), out('b', ['recipe', 'adjust']), out('c', ['recipe', 'adjust']), out('d', ['recipe', 'adjust']), out('e', ['adjust'], [reasons[1]])];
  assert.equal(text.pasteMessage(five), '已套用到 5 张；1 张只有 JPG，没有套用配方');
});

test('undoMessage 和桌面逐字相同', () => {
  for (const counts of [undefined, {}, { restored: 5 }, { restored: 4, changed: 1 }, { changed: 2 }, { restored: 1, changed: 1, error: 1 }, { error: 3 }]) {
    const res = counts === undefined ? {} : { counts };
    assert.equal(text.undoMessage(res), D.api.undoMessage(plain(res)), JSON.stringify(res));
  }
  assert.equal(text.undoMessage({ counts: { restored: 5, changed: 1 } }), '已撤销 5 张；1 张粘贴之后又改过，没有撤销');
});

test('选择栏的两句来自 paste_batch.js；没复制那句是桌面那句去掉快捷键', () => {
  assert.ok(BATCH.includes(`'${text.ONLY_SOURCE}'`));
  assert.ok(BATCH.includes('，或点「复制调整」。'));
  assert.equal(text.NOTHING_COPIED, '先打开一张照片，点「复制调整」。');
});

test('跳过的说明和 render/paste.py 的 Skip 一样', () => {
  const target = { photo: photo('t'), raw: false, unit: 'qlaunch' };
  const clip = copied();
  const seen = [];
  seen.push(planOne(target, clip, ['recipe'], env()).outcome.skipped[0]);
  seen.push(planOne({ ...target, unit: 'jlaunch' }, clip, ['recipe'], env()).outcome.skipped[0]);
  seen.push(planOne({ ...target, unit: 'jlaunch', raw: true }, clip, ['recipe'], env(false)).outcome.skipped[0]);
  seen.push(planOne({ ...target, unit: 'jlaunch', raw: true }, { ...clip, knobs: null }, ['adjust'], env()).outcome.skipped[0]);
  assert.deepEqual(seen.map((s) => s.reason), ['unit_mismatch', 'needs_raw', 'recipe_rejected', 'not_copied']);
  for (const s of seen) assert.ok(PY.includes(`"${s.message}"`) || PY.includes(`f"${s.message}{exc.message}"`), s.message);
});

// ---- 套用的规则 ---------------------------------------------------------------------

function knobs(over = {}) {
  return { exposure_ev: 0, contrast: 0, highlight: 0, shadow: 0, black_point: 0, warm: 0, tint: 0, saturation: 0, vibrance: 0, vignette: 0, ...over };
}

function photo(id, over = {}) {
  return { id, rev: 0, choice: null, levels: new Map(), memory: new Map(), knobs: knobs(), ...over };
}

function env(listed = true) {
  return { recipeWithoutRaw: false, listed: () => listed };
}

/** 来源：配方 builtin:3（50mm）带两档，memory 里记着切走过的原厂 04（空）和一个 Look，曝光 +0.7、对比度 -10。 */
function source() {
  return photo('src', {
    choice: { id: 'builtin:3', unit: 'jlaunch' },
    levels: new Map([['builtin:3', { amount: 80, contrast: 2 }]]),
    memory: new Map([
      ['builtin:4', {}],
      ['look:abc', { fade: 3 }],
    ]),
    knobs: knobs({ exposure_ev: 0.7, contrast: -10 }),
  });
}

function copied(groups = ['recipe', 'adjust']) {
  return copyFrom(source(), 'R0000001', groups).clip;
}

test('设置过：配方看有没有选，调整看有没有一项不是 0；别的组手机上设不了', () => {
  assert.deepEqual(groupsPresent(photo('a')), []);
  assert.deepEqual(groupsPresent(source()), ['recipe', 'adjust']);
  assert.equal(adjustIsSet(photo('a', { knobs: knobs({ vignette: -1 }) })), true);
  const r = copyFrom(photo('a', { choice: { id: 'builtin:1', unit: 'qlaunch' } }), 'A', ['recipe', 'adjust', 'crop']);
  assert.deepEqual(r.copied, ['recipe']);
  assert.deepEqual(r.empty, ['adjust', 'crop']);
  assert.equal(r.clip.knobs, null);
  const none = copyFrom(photo('a'), 'A', ['recipe', 'adjust']);
  assert.equal(none.clip, null);
  assert.equal(text.copyMessage(none), '这张照片在勾选的项目里没有修改，没有复制。');
});

test('剪贴板是复制那一刻的深拷贝', () => {
  const src = source();
  const clip = copyFrom(src, 'R0000001', ['recipe', 'adjust']).clip;
  src.knobs.exposure_ev = 3;
  src.levels.get('builtin:3').amount = 10;
  src.memory.get('look:abc').fade = 0;
  src.choice.id = 'builtin:1';
  assert.equal(clip.knobs.exposure_ev, 0.7);
  assert.deepEqual(clip.recipe.levels, { amount: 80, contrast: 2 });
  assert.equal(clip.recipe.memory.get('look:abc').fade, 3);
  assert.equal(clip.recipe.choice.id, 'builtin:3');
  assert.deepEqual(clip.from, { id: 'src', stem: 'R0000001' });
});

test('配方：同单元、有 RAW、列表里有 → 换配方和它的档位，memory 整份跟过来（桌面 memory），之后互不影响', () => {
  const clip = copied();
  const target = photo('t', {
    choice: { id: 'builtin:1', unit: 'jlaunch' },
    levels: new Map([['builtin:1', { amount: 50 }]]),
    memory: new Map([['builtin:2', { fade: 1 }]]),
    knobs: knobs({ shadow: 30 }),
  });
  const { outcome, before } = pasteOne({ photo: target, raw: true, unit: 'jlaunch' }, clip, clip.groups, env());
  assert.deepEqual(outcome, { id: 't', status: 'applied', applied: ['recipe', 'adjust'], skipped: [], rev: 1 });
  assert.deepEqual(target.choice, { id: 'builtin:3', unit: 'jlaunch' });
  assert.deepEqual([...target.levels.entries()], [['builtin:3', { amount: 80, contrast: 2 }]]);
  // 目标自己原来的配方不记进 memory（桌面 write 只是换掉 recipe），memory 换成来源的。
  assert.deepEqual([...target.memory.entries()], [['builtin:4', {}], ['look:abc', { fade: 3 }]]);
  // 调整整份换掉，0 也算（阴影原来是 30）。
  assert.deepEqual(target.knobs, knobs({ exposure_ev: 0.7, contrast: -10 }));
  assert.equal(target.rev, 1);
  assert.deepEqual([...before.levels.entries()], [['builtin:1', { amount: 50 }]]);
  assert.deepEqual([...before.memory.entries()], [['builtin:2', { fade: 1 }]]);
  target.levels.get('builtin:3').amount = 1;
  target.memory.get('look:abc').fade = 9;
  assert.equal(clip.recipe.levels.amount, 80);
  assert.equal(clip.recipe.memory.get('look:abc').fade, 3);
  // 撤销连 memory 一起还原。
  const undo = new PasteUndo();
  undo.add('m', 't', target.rev, before);
  assert.deepEqual(undoBatch(undo.take('m'), () => target).counts, { restored: 1, changed: 0, error: 0 });
  assert.deepEqual([...target.memory.entries()], [['builtin:2', { fade: 1 }]]);
  assert.deepEqual([...target.levels.entries()], [['builtin:1', { amount: 50 }]]);
  // 认不出镜头的照片：桌面不算单元不同。
  const unknown = photo('u');
  assert.equal(pasteOne({ photo: unknown, raw: true, unit: null }, clip, clip.groups, env()).outcome.status, 'applied');
});

test('每种跳过：单元不同、只有 JPG、配方不在列表里、复制时没带上；配方组要么整个套用要么不套', () => {
  const clip = copied();
  const cases = [
    [{ raw: true, unit: 'qlaunch' }, env(), 'unit_mismatch'],
    [{ raw: false, unit: 'jlaunch' }, env(), 'needs_raw'],
    [{ raw: false, unit: 'qlaunch' }, env(), 'unit_mismatch'],
    [{ raw: true, unit: 'jlaunch' }, env(false), 'recipe_rejected'],
  ];
  for (const [facts, e, reason] of cases) {
    const target = photo('t', { choice: { id: 'builtin:2', unit: facts.unit ?? 'jlaunch' }, levels: new Map([['builtin:2', { amount: 40 }]]) });
    const { outcome } = pasteOne({ photo: target, ...facts }, clip, clip.groups, e);
    assert.equal(outcome.status, 'partial', reason);
    assert.deepEqual(outcome.applied, ['adjust']);
    assert.deepEqual(outcome.skipped.map((s) => [s.group, s.reason, s.part]), [['recipe', reason, 'recipe']]);
    // 配方和档位都没动，调整套上了。
    assert.equal(target.choice.id, 'builtin:2');
    assert.deepEqual([...target.levels.keys()], ['builtin:2']);
    assert.equal(target.knobs.exposure_ev, 0.7);
  }
  // 只复制了配方，却要套两组：调整算「复制时没带上」。
  const onlyRecipe = copied(['recipe']);
  const t = photo('t');
  const r = pasteOne({ photo: t, raw: true, unit: 'jlaunch' }, onlyRecipe, ['recipe', 'adjust'], env());
  assert.equal(r.outcome.status, 'partial');
  assert.deepEqual(r.outcome.skipped.map((s) => [s.group, s.reason]), [['adjust', 'not_copied']]);
  assert.deepEqual(t.knobs, knobs());
  // 一组都没套上：不改 rev，不记撤销。
  const jpg = photo('j');
  const none = pasteOne({ photo: jpg, raw: false, unit: 'jlaunch' }, onlyRecipe, ['recipe'], env());
  assert.equal(none.outcome.status, 'skipped');
  assert.equal(none.before, null);
  assert.equal(jpg.rev, 0);
  assert.equal(text.pasteMessage([none.outcome]), '没有套用；这张只有 JPG，没有套用配方');
});

test('一批：来源这张不是目标，结果句和桌面同一个函数说', async () => {
  const clip = copied();
  const src = source();
  const ids = ['a', 'b', 'c', 'd', 'e'];
  const targets = [{ photo: src, raw: true, unit: 'jlaunch' }, ...ids.map((id) => ({ photo: photo(id), raw: true, unit: 'jlaunch' }))];
  targets.push({ photo: photo('jpg'), raw: false, unit: 'jlaunch' });
  const undo = new PasteUndo();
  const results = await pasteBatch(targets, clip, clip.groups, env(), undo, 'b1');
  assert.deepEqual(results.map((r) => r.id), [...ids, 'jpg']);
  assert.equal(src.rev, 0);
  assert.equal(text.pasteMessage(results), '已套用到 6 张；1 张只有 JPG，没有套用配方');
  assert.equal(undo.size(), 1);
});

// ---- 撤销 -------------------------------------------------------------------------

test('撤销：还停在套用后那一版的还原（并加 rev），之后又改过的不动，找不到的算没成功', async () => {
  const clip = copied();
  const photos = ['a', 'b', 'c'].map((id) => photo(id, { knobs: knobs({ warm: 5 }), choice: { id: 'builtin:1', unit: 'jlaunch' } }));
  const byId = new Map(photos.map((p) => [p.id, p]));
  const undo = new PasteUndo();
  await pasteBatch(photos.map((p) => ({ photo: p, raw: true, unit: 'jlaunch' })), clip, clip.groups, env(), undo, 'b1');
  assert.deepEqual(photos.map((p) => p.rev), [1, 1, 1]);
  photos[1].knobs.contrast = 20;
  photos[1].rev += 1; // 套用之后又在编辑器里改过
  byId.delete('c'); // 这张被去掉了
  const res = undoBatch(undo.take('b1'), (id) => byId.get(id));
  assert.deepEqual(res.counts, { restored: 1, changed: 1, error: 1 });
  assert.equal(text.undoMessage(res), '已撤销 1 张；1 张粘贴之后又改过，没有撤销；1 张没有撤销成功');
  assert.deepEqual(photos[0].knobs, knobs({ warm: 5 }));
  assert.deepEqual(photos[0].choice, { id: 'builtin:1', unit: 'jlaunch' });
  assert.equal(photos[0].rev, 2);
  assert.equal(photos[1].knobs.contrast, 20);
  assert.equal(photos[1].rev, 2);
  // 一批只能撤销一次。
  assert.equal(undo.take('b1'), null);
});

test('同一批里套到同一张两次，撤销回到最早的样子', () => {
  const undo = new PasteUndo();
  const p = photo('a', { knobs: knobs({ tint: 9 }) });
  const first = copied(['adjust']);
  const second = copyFrom(photo('s2', { knobs: knobs({ tint: -3 }) }), 'S2', ['adjust']).clip;
  for (const clip of [first, second]) {
    const { outcome, before } = pasteOne({ photo: p, raw: true, unit: null }, clip, clip.groups, env());
    undo.add('b', p.id, outcome.rev, before);
  }
  assert.equal(p.knobs.tint, -3);
  assert.equal(p.rev, 2);
  const res = undoBatch(undo.take('b'), () => p);
  assert.deepEqual(res.counts, { restored: 1, changed: 0, error: 0 });
  assert.equal(p.knobs.tint, 9);
  assert.equal(p.rev, 3);
});

test('只留最近 20 批（MAX_UNDO_BATCHES），再加的挤掉最早的；动过的批排到最后', () => {
  assert.equal(MAX_UNDO_BATCHES, 20);
  const undo = new PasteUndo();
  const before = { choice: null, levels: new Map(), memory: new Map(), knobs: knobs() };
  for (let i = 0; i < 20; i++) undo.add(`b${i}`, 'x', 1, before);
  undo.add('b0', 'y', 1, before); // 再用一次最早那批：它排到最新
  undo.add('b20', 'x', 1, before);
  assert.equal(undo.size(), 20);
  assert.equal(undo.has('b0'), true);
  assert.equal(undo.has('b1'), false);
  assert.equal(undo.has('b20'), true);
});

test('还原只改编辑状态的三项，rev 对不上时什么都不写', () => {
  const p = photo('a', { rev: 4, knobs: knobs({ saturation: 7 }) });
  const entry = { rev: 3, before: { choice: { id: 'builtin:2', unit: 'qlaunch' }, levels: new Map([['builtin:2', { amount: 1 }]]), memory: new Map([['builtin:4', {}]]), knobs: knobs() } };
  assert.equal(restore(p, entry), 'changed');
  assert.equal(p.knobs.saturation, 7);
  assert.equal(p.rev, 4);
  assert.equal(restore(undefined, entry), 'error');
  entry.rev = 4;
  assert.equal(restore(p, entry), 'restored');
  assert.deepEqual(p.choice, { id: 'builtin:2', unit: 'qlaunch' });
  entry.before.levels.get('builtin:2').amount = 99;
  assert.equal(p.levels.get('builtin:2').amount, 1);
});

test('按时间片套用：每片之后让出主线程；清空照片后不再套用', async () => {
  const clip = copied(['adjust']);
  let t = 0;
  const now = () => t;
  const photos = Array.from({ length: 10 }, (_, i) => photo(`p${i}`));
  let yields = 0;
  const slices = [];
  await pasteBatch(
    photos.map((p) => ({ photo: p, raw: true, unit: null })),
    clip,
    clip.groups,
    env(),
    new PasteUndo(),
    'b',
    {
      budgetMs: 4,
      now: () => {
        t += 1;
        return now();
      },
      yieldFn: async () => {
        yields += 1;
      },
      onSlice: (ms) => slices.push(ms),
    },
  );
  assert.ok(yields >= 2, String(yields));
  assert.ok(photos.every((p) => p.knobs.exposure_ev === 0.7));
  let alive = true;
  const later = Array.from({ length: 10 }, (_, i) => photo(`q${i}`));
  const results = await pasteBatch(later.map((p) => ({ photo: p, raw: true, unit: null })), clip, clip.groups, env(), new PasteUndo(), 'c', {
    budgetMs: 0,
    yieldFn: async () => {
      alive = false;
    },
    alive: () => alive,
  });
  assert.equal(results.length, 1);
  assert.equal(later[1].rev, 0);
});

test('来源的 memory 是空的：目标的 memory 留着（桌面 recipeBody 不带 memory 键，write 沿用 sidecar 里的）', () => {
  const src = photo('s', { choice: { id: 'builtin:2', unit: 'jlaunch' }, levels: new Map([['builtin:2', { amount: 60 }]]) });
  const clip = copyFrom(src, 'S', ['recipe']).clip;
  assert.equal(clip.recipe.memory.size, 0);
  const target = photo('t', { memory: new Map([['look:x', { fade: 2 }]]) });
  pasteOne({ photo: target, raw: true, unit: 'jlaunch' }, clip, clip.groups, env());
  assert.deepEqual(target.choice, { id: 'builtin:2', unit: 'jlaunch' });
  assert.deepEqual([...target.levels.entries()], [['builtin:2', { amount: 60 }]]);
  assert.deepEqual([...target.memory.entries()], [['look:x', { fade: 2 }]]);
});

// ---- 换配方时的 memory（look_cards.js recipeTarget）和 cleanLevels -------------------------

const control = (name, levels, dflt = 0) => ({ name, label: name, min: Math.min(...levels), max: Math.max(...levels), default: dflt, cap_lower: false, cap_upper: false, amount_step: null, levels });
const card = (id, controls) => ({ id, kind: id.split(':')[0], name: id, desc: null, slot: 1, recipeId: null, controls, number: '01' });
const B4 = card('builtin:4', [control('amount', [0, 50, 100], 100), control('contrast', [-2, -1, 0, 1, 2])]);
const B2 = card('builtin:2', [control('amount', [0, 50, 100], 100), control('fade', [0, 1, 2, 3])]);
const LOOK = card('look:abc', [control('fade', [0, 1, 2, 3])]);

test('cleanLevels：只留这个配方有的、值在可选里的档位，不补默认', () => {
  assert.deepEqual(cleanLevels(B2, { amount: 50, fade: 9, contrast: 1 }), { amount: 50 });
  assert.deepEqual(cleanLevels(B2, undefined), {});
});

test('换配方：切走的（没选过时是原厂 04、档位为空）记进 memory，新配方取回上次的档位；当前配方不进 memory', () => {
  // 没选配方，第一次点 builtin:2：memory 里多一项空的原厂 04（桌面 selection 的默认），builtin:2 从空档位开始。
  const first = recipeSwitch(new Map(), { id: 'builtin:4', card: B4, levels: undefined, chosen: false }, B2);
  assert.equal(first.same, false);
  assert.deepEqual([...first.memory.entries()], [['builtin:4', {}]]);
  assert.deepEqual(first.levels, {});
  // 在 builtin:2 上动了档位再换到 Look：builtin:2 的档位（清理过）记进 memory。
  const second = recipeSwitch(first.memory, { id: 'builtin:2', card: B2, levels: { amount: 50, fade: 9 }, chosen: true }, LOOK);
  assert.deepEqual([...second.memory.entries()], [['builtin:4', {}], ['builtin:2', { amount: 50 }]]);
  assert.deepEqual(second.levels, {});
  // 换回 builtin:2：取回 memory 里的档位；memory 里那一项留着（桌面也不删）。
  const back = recipeSwitch(second.memory, { id: 'look:abc', card: LOOK, levels: { fade: 2 }, chosen: true }, B2);
  assert.deepEqual(back.levels, { amount: 50 });
  assert.deepEqual([...back.memory.entries()], [['builtin:4', {}], ['builtin:2', { amount: 50 }], ['look:abc', { fade: 2 }]]);
  // 点已经选中的那张：什么都不改。
  assert.deepEqual(recipeSwitch(back.memory, { id: 'builtin:2', card: B2, levels: { amount: 50 }, chosen: true }, B2), { same: true });
  // 没选配方时点原厂 04：桌面不算同一个（ed.recipe 是空的），照样记一项。
  const stock = recipeSwitch(new Map(), { id: 'builtin:4', card: B4, levels: undefined, chosen: false }, B4);
  assert.equal(stock.same, false);
  assert.deepEqual([...stock.memory.entries()], [['builtin:4', {}]]);
  // 输入的 memory 不被改动。
  assert.deepEqual([...first.memory.entries()], [['builtin:4', {}]]);
});

// ---- Sol r1 P2：片与片之间目标变了，不写入 ---------------------------------------------

/** 每张各占一片；第一片之后（让出主线程时）跑一次 `between`。 */
async function sliced(targets, clip, between, current) {
  let ran = false;
  return pasteBatch(targets, clip, clip.groups, env(), new PasteUndo(), 'drift', {
    budgetMs: 0,
    yieldFn: async () => {
      if (!ran) {
        ran = true;
        between();
      }
    },
    current,
  });
}

test('片之间目标被编辑（rev 变了）：不写入、不记撤销，按桌面 rev_conflict 计数', async () => {
  const clip = copied(['adjust']);
  const a = photo('a', { version: 1 });
  const b = photo('b', { version: 1 });
  const byId = new Map([['a', a], ['b', b]]);
  const results = await sliced([a, b].map((p) => ({ photo: p, raw: true, unit: null })), clip, () => {
    b.knobs.exposure_ev = 3;
    b.rev += 1;
  }, (id) => byId.get(id));
  assert.deepEqual(results.map((r) => [r.id, r.status, r.code ?? null]), [['a', 'applied', null], ['b', 'error', 'rev_conflict']]);
  assert.equal(b.knobs.exposure_ev, 3);
  assert.equal(b.rev, 1);
  assert.equal(text.pasteMessage(results), '已套用到 1 张；1 张在别处改过，没有粘贴');
});

test('片之间目标被重新选过（version 变了）、去掉了、换了对象：都不写入', async () => {
  const clip = copied(['adjust']);
  for (const [name, change, code] of [
    ['re-picked', (b, byId) => { b.version += 1; }, 'rev_conflict'],
    ['removed', (b, byId) => { byId.delete('b'); }, 'photo_not_found'],
    ['replaced', (b, byId) => { byId.set('b', photo('b', { version: 1 })); }, 'photo_not_found'],
  ]) {
    const a = photo('a', { version: 1 });
    const b = photo('b', { version: 1 });
    const byId = new Map([['a', a], ['b', b]]);
    const undo = new PasteUndo();
    const results = await pasteBatch([a, b].map((p) => ({ photo: p, raw: true, unit: null })), clip, clip.groups, env(), undo, 'x', {
      budgetMs: 0,
      yieldFn: async () => change(b, byId),
      current: (id) => byId.get(id),
    });
    assert.deepEqual(results.map((r) => [r.id, r.code ?? r.status]), [['a', 'applied'], ['b', code]], name);
    assert.equal(b.knobs.exposure_ev, 0, name);
    assert.equal(b.rev, 0, name);
    assert.deepEqual([...undo.take('x').keys()], ['a'], name);
  }
});

test('同一批里同一张出现两次：第二次按这一批自己写完的 rev 核对，照样套用，撤销回到最早的样子', async () => {
  const clip = copied(['adjust']);
  const a = photo('a', { version: 1, knobs: knobs({ tint: 4 }) });
  const undo = new PasteUndo();
  const results = await pasteBatch([{ photo: a, raw: true, unit: null }, { photo: a, raw: true, unit: null }], clip, clip.groups, env(), undo, 'd', {
    budgetMs: 0,
    yieldFn: async () => undefined,
    current: () => a,
  });
  assert.deepEqual(results.map((r) => r.status), ['applied', 'applied']);
  assert.equal(a.rev, 2);
  undoBatch(undo.take('d'), () => a);
  assert.equal(a.knobs.tint, 4);
});
