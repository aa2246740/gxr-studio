// 加载态小工具的真跑场景：busy.js（分档、按钮锁、乐观更新）、poll.js（不重叠、后台暂停）、
// gallery.js 的选择纯函数、preview.js 的撤销/重做。用 node 直接 import 拷过去的模块，不开浏览器。
// 用法：node busy_scene.mjs <模块目录> <场景>，结果按 JSON 打到 stdout。
import { installDom } from './mini_dom.mjs';

installDom();

const [dir, scenario] = process.argv.slice(2);
const file = (name) => new URL(`file://${dir}/${name}`).href;
const tick = (n = 1) => new Promise((r) => { let i = 0; const go = () => (++i >= n ? r() : setTimeout(go, 0)); setTimeout(go, 0); });

// 手动时钟：setTimer/clearTimer 注进被测函数，advance(ms) 让到点的回调执行
function fakeClock() {
  let now = 0;
  let seq = 0;
  const timers = new Map();
  return {
    setTimer: (fn, ms) => { const id = ++seq; timers.set(id, { at: now + ms, fn }); return id; },
    clearTimer: (id) => timers.delete(id),
    advance(ms) {
      now += ms;
      for (const [id, t] of [...timers]) if (t.at <= now) { timers.delete(id); t.fn(); }
    },
  };
}

const out = {};

if (scenario === 'thresholds') {
  const busy = await import(file('busy.js'));
  out.tiers = [busy.SHOW_AFTER_MS, busy.DETAIL_AFTER_MS, busy.BACKGROUND_AFTER_MS];
  // 快操作：149ms 内结束 → 什么都不显示
  let c = fakeClock();
  let shown = 0;
  let d = busy.delayed(() => shown++, busy.SHOW_AFTER_MS, c);
  c.advance(149);
  d.cancel();
  c.advance(1000);
  out.fastShown = shown;
  out.fastFired = d.fired;
  // 慢操作：到 150ms 才出现
  c = fakeClock();
  shown = 0;
  d = busy.delayed(() => shown++, busy.SHOW_AFTER_MS, c);
  c.advance(149);
  out.before150 = shown;
  c.advance(1);
  out.at150 = shown;
  out.slowFired = d.fired;
} else if (scenario === 'button-lock') {
  const busy = await import(file('busy.js'));
  const { h } = await import(file('dom.js'));
  const holder = h('div', {});
  const btn = h('button', { class: 'btn', type: 'button' }, h('span', {}, '重建索引'));
  holder.append(btn);
  document.body.append(holder);
  let calls = 0;
  let release;
  const task = () => { calls++; return new Promise((r) => { release = r; }); };
  const first = busyRun();
  function busyRun() { return busy.busyButton(btn, task, { busyText: '正在重建…' }); }
  out.lockedAtOnce = btn.getAttribute('aria-disabled') === 'true';
  out.spinAtOnce = btn.getAttribute('aria-busy');
  const second = await busyRun();              // 锁住时再点：不发第二次
  out.secondResult = second === undefined;
  out.callsWhileLocked = calls;
  await new Promise((r) => setTimeout(r, 170));
  out.spinAfter150 = btn.getAttribute('aria-busy');
  out.textWhileBusy = btn.textContent;
  release('ok');
  out.result = await first;
  out.unlocked = btn.getAttribute('aria-disabled') === null && btn.getAttribute('aria-busy') === null && btn.dataset.busy === '';
  out.textAfter = btn.textContent;
  // 失败：按钮旁边写原因并给「重试」，重试再跑一次 task
  let fail = true;
  const flaky = async () => { calls++; if (fail) throw new Error('图库读不了'); return 'second-ok'; };
  const r = await busy.busyButton(btn, flaky);
  out.failedResult = r === undefined;
  const err = holder.querySelector('.inline-error');
  out.inlineError = err ? err.textContent : null;
  out.errorRole = err ? err.getAttribute('role') : null;
  fail = false;
  const before = calls;
  err.querySelector('button').click();
  await tick(3);
  out.retryRan = calls === before + 1;
  out.errorCleared = !holder.querySelector('.inline-error');
} else if (scenario === 'optimistic') {
  const busy = await import(file('busy.js'));
  const ui = { starred: false };
  const log = [];
  const ok = await busy.optimistic({
    apply: () => { ui.starred = true; log.push('apply'); },
    commit: async () => { log.push(`commit:${ui.starred}`); return 'saved'; },
    rollback: () => { ui.starred = false; log.push('rollback'); },
  });
  out.okResult = ok;
  out.okState = ui.starred;
  let reported = null;
  const bad = await busy.optimistic({
    apply: () => { ui.starred = false; log.push('apply'); },
    commit: async () => { log.push(`commit:${ui.starred}`); throw new Error('没存上'); },
    rollback: () => { ui.starred = true; log.push('rollback'); },
    onError: (e) => { reported = e.message; },
  });
  out.badResult = bad === undefined;
  out.badState = ui.starred;
  out.reported = reported;
  out.log = log;
} else if (scenario === 'poll') {
  const { every } = await import(file('poll.js'));
  const doc = new EventTarget();
  doc.hidden = false;
  let inFlight = 0;
  let maxInFlight = 0;
  let runs = 0;
  const releases = [];
  const task = () => { runs++; inFlight++; maxInFlight = Math.max(maxInFlight, inFlight); return new Promise((r) => releases.push(() => { inFlight--; r(); })); };
  const p = every(5, task, { doc });
  await new Promise((r) => setTimeout(r, 40));   // 第一次请求一直没回来：不能再发
  out.runsWhileSlow = runs;
  releases.shift()();
  await new Promise((r) => setTimeout(r, 20));
  out.runsAfterRelease = runs;
  while (releases.length) releases.shift()();
  // 切到后台：不再发
  doc.hidden = true;
  await new Promise((r) => setTimeout(r, 15));
  const hiddenStart = runs;
  await new Promise((r) => setTimeout(r, 40));
  out.runsWhileHidden = runs - hiddenStart;
  // 回到前台：马上补一次
  doc.hidden = false;
  const back = runs;
  doc.dispatchEvent(new Event('visibilitychange'));
  await tick();
  out.catchUpOnVisible = runs - back;
  while (releases.length) releases.shift()();
  p.stop();
  await tick(2);
  const stopped = runs;
  await new Promise((r) => setTimeout(r, 30));
  out.runsAfterStop = runs - stopped;
  out.maxInFlight = maxInFlight;
} else if (scenario === 'gallery-select') {
  const g = await import(file('gallery.js'));
  const order = ['a', 'b', 'c', 'd', 'e'];
  const list = (s) => [...s].sort();
  out.forward = list(g.rangeSelect(order, 'b', 'd'));
  out.backward = list(g.rangeSelect(order, 'd', 'b'));
  out.keepBase = list(g.rangeSelect(order, 'd', 'e', new Set(['a'])));
  out.noAnchor = list(g.rangeSelect(order, 'zz', 'c'));
  out.unknownTarget = list(g.rangeSelect(order, 'a', 'zz'));
  out.toggleOn = list(g.toggleSelect(new Set(['a']), 'b'));
  out.toggleOff = list(g.toggleSelect(new Set(['a', 'b']), 'b'));
  const orig = new Set(['a']);
  g.toggleSelect(orig, 'c');
  out.inputUntouched = list(orig);
} else if (scenario === 'redo') {
  globalThis.apiStub = async (url, opts = {}) => ({ rev: 1, ...(opts.body || {}) });
  const p = await import(file('preview.js'));
  const view = { photo: { id: 'x' }, undo: [], redo: [], ed: { recipe: null, lut: null, adj: { exposure: 0 }, curve: null, geometry: null, frame: { style: 'none' }, stamp: { enabled: false }, v2: false } };
  const val = () => view.ed.adj.exposure;
  p.beginChange(view); view.ed.adj = { exposure: 1 }; p.endChange(view);
  p.beginChange(view); view.ed.adj = { exposure: 2 }; p.endChange(view);
  out.start = val();
  p.undoChange(view);
  out.afterUndo = val();
  p.undoChange(view);
  out.afterUndo2 = val();
  p.redoChange(view);
  out.afterRedo = val();
  p.redoChange(view);
  out.afterRedo2 = val();
  out.redoEmptyNoop = (p.redoChange(view), val());
  p.undoChange(view);
  p.beginChange(view); view.ed.adj = { exposure: 5 }; p.endChange(view);
  out.redoClearedByNewChange = view.redo.length;
  out.afterNew = val();
  await tick(5);
}

if (scenario === 'recipe-names') {
  const { state } = await import(file('state.js'));
  const p = await import(file('preview.js'));
  state.catalog = { units: [{ id: 'jlaunch', recipes: [{ slot: 1 }, { slot: 5, name: '我的街拍' }] }] };
  out.names = [1, 2, 3, 4, 5, 6, 7, 8].map((s) => p.slotName('jlaunch', s));
  out.unknownUnit = p.slotName('nope', 3);
  out.base = p.BASE_NAMES;
} else if (scenario === 'gpu-pref') {
  const g = await import(file('gpu_pref.js'));
  const { on } = await import(file('state.js'));
  const events = [];
  on('gpu-pref-changed', () => events.push(g.gpuPreference()));
  out.defaultOn = g.gpuPreference();
  g.setGpuPreference(false);
  out.afterOff = g.gpuPreference();
  out.stored = localStorage.getItem('gxr-studio.gpu-preview');
  g.setGpuPreference(true);
  out.afterOn = g.gpuPreference();
  out.events = events;
} else if (scenario === 'lut-import-progress') {
  const { importFiles } = await import(file('lut_files.js'));
  const calls = [];
  const files = [{ file: {}, path: 'a.cube' }, { file: {}, path: '__MACOSX/._a.cube' }, { file: {}, path: 'b.cube' }];
  const res = await importFiles(files, async (f, path) => {
    if (path === 'b.cube') throw new Error('坏文件');
    return { cube_id: path };
  }, (done, total) => calls.push([done, total]));
  out.calls = calls;
  out.status = res.map((r) => r.status);
  out.noCallback = (await importFiles([{ file: {}, path: 'c.cube' }], async () => ({}))).length;
} else if (scenario === 'error-text') {
  const { errorBody } = await import(file('dom.js'));
  const generic = Object.assign(new Error('无法完成操作。请重试。'), { generic: true });
  const specific = new Error('照片已经不在图库里了');
  out.generic = errorBody(generic, '没有导出这个 LUT。请重试。');
  out.specific = errorBody(specific, '没有导出这个 LUT。请重试。');
  out.noFallback = errorBody(generic);
}

console.log(JSON.stringify(out));
