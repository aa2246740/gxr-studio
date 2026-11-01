// 用明确的请求完成顺序检查产品缓存模块，不依赖时间间隔。
import { pathToFileURL } from 'node:url';

const folder = process.argv[2];
const scenario = process.argv[3];
const file = (name) => pathToFileURL(`${folder}/${name}`).href;
const calls = new Map();
let recipe = 'none';
let lookCount = 0;
let heldPath = null;
let release;
let reject;
let notifyStarted;
let started;
const capability = () => ({ raw: true, recipe });
const catalog = () => ({ units: [{ id: 'jlaunch', looks: Array.from({ length: lookCount }, (_, i) => ({ name: `Look ${i}` })) }] });

globalThis.cacheApi = async (path) => {
  calls.set(path, (calls.get(path) || 0) + 1);
  if (path === heldPath) {
    heldPath = null;
    const result = path === '/api/recipes' ? catalog() : capability();
    return new Promise((done, failed) => {
      release = () => done(result);
      reject = () => failed(new Error('旧版本请求未能读取'));
      notifyStarted();
    });
  }
  if (path.endsWith('/capabilities')) return capability();
  if (path.endsWith('/meta')) return { camera: 'GXR' };
  if (path === '/api/recipes') return catalog();
  throw new Error(`未预期的接口 ${path}`);
};

const shared = await import(file('state.js'));
const camera = await import(file('camdata.js'));
const caps = await import(file('caps.js'));
const preview = await import(file('preview.js'));
const update = (version, ready) => {
  recipe = ready ? 'full' : 'none';
  lookCount = ready ? 10 : 0;
  camera.applyCamdataStatus({ ready, data_version: version, camera_data: { state: 'ready', generation: 152 },
    calibration: { state: ready ? 'ready' : 'missing' } });
};
const hold = (path) => {
  heldPath = path;
  started = new Promise((done) => { notifyStarted = done; });
};
const stateOf = (result) => result.state === 'ok' ? result.data.recipe : result.state;

update('version-1', false);
let result;
if (scenario === 'previous-photo-after-import') {
  const before = await caps.loadPhotoCaps('A');
  await caps.loadPhotoCaps('B');
  await caps.loadMeta('A');
  update('version-2', true);
  const current = await caps.loadPhotoCaps('B', { refresh: true });
  const revisited = await caps.loadPhotoCaps('A');
  await caps.loadMeta('A');
  result = { before: stateOf(before), current: stateOf(current), revisited: stateOf(revisited),
    requests: calls.get('/api/photos/A/capabilities'), metaRequests: calls.get('/api/photos/A/meta') };
} else if (scenario.startsWith('data-change-pending-caps')) {
  hold('/api/photos/A/capabilities');
  const waiting = caps.loadPhotoCaps('A');
  await started;
  update('version-2', true);
  if (scenario.endsWith('-error')) reject(); else release();
  const resolved = await waiting;
  const cached = await caps.loadPhotoCaps('A');
  result = { resolved: stateOf(resolved), cached: stateOf(cached), requests: calls.get('/api/photos/A/capabilities') };
} else if (scenario.startsWith('data-change-pending-catalog')) {
  hold('/api/recipes');
  const waiting = preview.loadCatalog();
  await started;
  update('version-2', true);
  if (scenario.endsWith('-error')) reject(); else release();
  try {
    const resolved = await waiting;
    result = { resolved: resolved.units[0].looks.length, cached: shared.state.catalog?.units[0].looks.length ?? null,
      requests: calls.get('/api/recipes') };
  } catch {
    result = { resolved: 'rejected', cached: shared.state.catalog?.units[0].looks.length ?? null, requests: calls.get('/api/recipes') };
  }
} else {
  throw new Error(`未预期的场景 ${scenario}`);
}
console.log(JSON.stringify(result));
