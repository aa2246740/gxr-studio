// 真正的卡片调度器、gpu_stage 和 api；只有网络、GPU 和像素容器用替身。
// 指定的 Promise 未完成时发输入事件，再交还结果，检查没有提交、解码、画图或缓存旧卡片。
import { pathToFileURL } from 'node:url';
import { installDom } from './mini_dom.mjs';

const [dir, phase, gesture] = process.argv.slice(2);
const file = (name) => pathToFileURL(`${dir}/${name}`).href;
installDom();
let clock = 1000;
let timerId = 0;
const timers = new Map();
Object.defineProperty(globalThis, 'performance', { value: { now: () => clock }, configurable: true });
globalThis.setTimeout = (fn, ms) => { const id = ++timerId; timers.set(id, { at: clock + ms, fn }); return id; };
globalThis.clearTimeout = (id) => timers.delete(id);
const flush = () => new Promise((resolve) => setImmediate(resolve));
async function advance(ms = 200) {
  clock += ms;
  const due = [...timers].filter(([, t]) => t.at <= clock);
  for (const [id, t] of due) { if (timers.delete(id)) t.fn(); }
  await flush();
}
function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}
const held = deferred();
let reached = false;
let released = false;
async function boundary(name) {
  if (phase !== name || released) return;
  reached = true;
  await held.promise;
}
let pixelWrites = 0;
let bitmapDraws = 0;
let bitmapCloses = 0;
const createElement = document.createElement;
globalThis.ImageData = class { constructor(data, w, h) { this.data = data; this.width = w; this.height = h; } };
document.createElement = (tag) => {
  const n = createElement(tag);
  if (tag === 'canvas') n.getContext = () => ({
    putImageData() { pixelWrites++; },
    drawImage() { bitmapDraws++; },
    getImageData: (x, y, w, h) => new ImageData(new Uint8ClampedArray(w * h * 4), w, h),
  });
  return n;
};
globalThis.createImageBitmap = async () => {
  await boundary('server-bitmap');
  return { width: 2, height: 2, close() { bitmapCloses++; } };
};
Object.defineProperty(globalThis, 'navigator', { value: { gpu: {} }, configurable: true });
globalThis.engineLog = [];
globalThis.thumbStub = async () => {
  await boundary('gpu-readback');
  return { w: 2, h: 2, data: new Uint8Array(16) };
};
const renders = [];
const json = (data) => ({ ok: true, status: 200, text: async () => JSON.stringify(data), headers: { get: () => null } });
const binary = (key) => ({
  ok: true, status: 200,
  headers: { get: (name) => name.toLowerCase() === 'x-gxr-bake' ? encodeURIComponent(JSON.stringify({ key, nx: 2, ny: 2, origin: [0, 0], step: 1, calibrated: true })) : null },
  arrayBuffer: async () => new ArrayBuffer(16),
});
globalThis.fetch = async (url, opts = {}) => {
  if (url.includes('/render?')) {
    renders.push(opts.signal);
    // 故意允许取消后的旧结果到达：abort 之外，每个 await 后还必须再检查。
    await boundary('server-response');
    return { ...json(null), blob: async () => { await boundary('server-blob'); return {}; } };
  }
  if (url.includes('/bake?')) {
    const slot = JSON.parse(new URL(url, 'http://localhost').searchParams.get('recipe')).slot;
    if (slot === 2) await boundary('gpu-bake');
    return binary(`K${slot}`);
  }
  if (url.endsWith('/gpu')) return json({ enabled: true, unit: 'jlaunch', raw: { url: '/raw', layer: [100, 80], full: [200, 160] }, jpg: null, scene: { A: [[1, 0, 0], [0, 1, 0], [0, 0, 1]], b: [0, 0, 0] } });
  if (url === '/raw') return binary('raw');
  throw new Error(`没有替身：${url}`);
};
const { state } = await import(file('state.js'));
const { setGpuPreference } = await import(file('gpu_pref.js'));
const { gpuContext } = await import(file('preview.js'));
const gs = await import(file('gpu_stage.js'));
const { newDeck } = await import(file('look_cards.js'));
state.settings = { gpu_preview: !phase.startsWith('server-') };
state.camdata = { dataVersion: 'v1' };
state.catalog = { units: [{ id: 'jlaunch', recipes: [{ slot: 1, controls: [] }, { slot: 2, controls: [] }] }] };
const view = {
  photo: { id: 'P', has_raw: true }, stage: 'final', crop: null,
  ed: { v2: true, lut: null, adj: {}, curve: null, geometry: {}, frame: { style: 'none' }, stamp: { enabled: false }, caps: { recipe: 'full' }, recipe: { kind: 'builtin', unit: 'jlaunch', slot: 1, levels: {} } },
};
async function stageReady() {
  for (let i = 0; i < 30; i++) {
    if (gs.route(view, gpuContext(view), () => {}) === 'gpu') return;
    await flush();
  }
  throw new Error('舞台没有准备好');
}
if (state.settings.gpu_preview) await stageReady();
let deck;
function openDeck() {
  deck = newDeck(view);
  const root = deck.grid([{ id: 'builtin:2', name: '测试配方', render: { kind: 'builtin', unit: 'jlaunch', slot: 2, levels: {} }, onPick() {} }]);
  document.body.replaceChildren(root);
  deck.start(root);
}
const snapshot = () => ({
  painted: deck.stats.painted, gpu: deck.stats.gpu, server: deck.stats.server, pixelWrites,
  failed: !!deck.cards[0].failed,
});
const counts = () => ({ gpu: deck.stats.gpu, server: deck.stats.server, cached: deck.stats.cached });
async function until(test) {
  for (let i = 0; i < 40 && !test(); i++) await advance();
  if (!test()) throw new Error('指定的卡片边界没有到达');
}
const out = {};
if (phase === 'cache') {
  openDeck();
  await until(() => deck.stats.painted === 1);
  out.first = counts();
  openDeck();
  await until(() => deck.stats.painted === 1);
  out.same = counts();
  view.gpuAspect = 111 / 77;
  openDeck();
  await until(() => deck.stats.painted === 1);
  out.aspect = counts();
  setGpuPreference(false);
  openDeck();
  await until(() => deck.stats.painted === 1);
  out.server = counts();
  setGpuPreference(true);
  await stageReady();
  openDeck();
  await until(() => deck.stats.painted === 1);
  out.backToGpu = counts();
  setGpuPreference(false);
  setGpuPreference(true);
  await stageReady();
  openDeck();
  await until(() => deck.stats.painted === 1);
  out.offOn = counts();
} else {
  openDeck();
  await until(() => reached);
  out.reached = reached;
  document.dispatchEvent(new Event(gesture === 'pointer-tap' ? 'pointerdown' : gesture));
  if (gesture === 'pointer-tap') document.dispatchEvent(new Event('pointerup'));
  out.aborted = renders[0]?.aborted ?? false;
  released = true;
  held.resolve();
  await flush();
  out.during = snapshot();
  out.thumbsDuring = engineLog.filter(([name]) => name === 'thumb').length;
  out.serverStartsDuring = renders.length;
  out.bitmapDrawsDuring = bitmapDraws;
  if (gesture === 'pointerdown') {
    await advance(800);
    out.during = snapshot();
    document.dispatchEvent(new Event('pointerup'));
  }
  await until(() => deck.stats.painted === 1);
  out.after = snapshot();
  out.bitmapCloses = bitmapCloses;
}
deck.cancel();
console.log(JSON.stringify(out));
