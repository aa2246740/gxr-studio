// 配方卡片的前端检查：说明表、分组和原因、缓存键、点卡片写进去的编辑和点原来的按钮一模一样、键盘。
// 接口用 fetch 替身，DOM 用 mini_dom（不画图：画图和显卡在真浏览器里测，见 tests/test_look_cards_browser.py）。
// 用法：node look_cards.mjs <web/assets 目录> <场景>。输出一行 JSON，断言在 tests/test_look_cards.py。
import { pathToFileURL } from 'node:url';
import { installDom, tick } from './mini_dom.mjs';

const dir = process.argv[2];
const scenario = process.argv[3];
const file = (name) => pathToFileURL(`${dir}/${name}`).href;
installDom();
const walk = (n, acc = []) => { acc.push(n); for (const c of n.children || []) walk(c, acc); return acc; };
const find = (n, test) => walk(n).find(test);
const out = {};

if (scenario === 'model') {
  const M = await import(file('look_model.js'));
  out.builtin = [1, 2, 3, 4, 5, 8].map((slot) => M.describeLook({ kind: 'builtin', slot }));
  out.known = M.describeLook({ kind: 'look', name: 'Acros' });
  out.unknown = M.describeLook({ kind: 'look', name: '某个从没见过的 Look' });
  out.camera = M.describeLook({ kind: 'camera_user', name: 'FCC-001' });
  out.computerTitled = M.describeLook({ kind: 'computer', source: { kind: 'lut', title: 'Portra 400' } });
  out.computerUntitled = M.describeLook({ kind: 'computer', source: { kind: 'lut', title: null } });
  const all = [...Object.values(M.BUILTIN_TEXT), ...Object.values(M.LOOK_TEXT)];
  out.longest = Math.max(...all.map((t) => [...t].length));
  out.max = M.LOOK_TEXT_MAX;
  const mine = [
    { name: 'a', recipe: { unit: 'jlaunch', slot: 2 } },
    { name: 'b', recipe: { unit: 'qlaunch', slot: 2 } },
    { name: 'c', recipe: { unit: 'l08firm', slot: 1 } },
  ];
  const g28 = M.groupComputer(mine, 'qlaunch', 'qlaunch');
  out.g28 = { usable: g28.usable.map((r) => r.name), other: g28.other.map((o) => [o.recipe.name, o.reason]) };
  const gMount = M.groupComputer(mine, 'l08firm', 'l08firm');
  out.gMount = { usable: gMount.usable.map((r) => r.name), other: gMount.other.map((o) => o.reason) };
  out.unknownPhoto = M.groupComputer(mine, 'jlaunch', null).other.map((o) => o.reason);
  out.none = M.groupComputer(null, 'jlaunch', 'jlaunch');
  // 缓存键：什么让卡片重画，什么不让
  const base = { photoId: 'p1', dataVersion: 'v1', edge: 300, lut: null, adjust: { exposure_ev: 0 }, curve: null, geometry: { straighten: 0 }, crop: { rect: [0, 0, 1, 1] } };
  const recipe = { kind: 'builtin', unit: 'jlaunch', slot: 2, levels: { contrast: 1 } };
  const k = (patch = {}, r = recipe) => M.cardKey(M.sharedKey({ ...base, ...patch }), r);
  const same = k();
  out.same = k({}) === same;
  out.changes = {
    photo: k({ photoId: 'p2' }) !== same,
    data: k({ dataVersion: 'v2' }) !== same,
    edge: k({ edge: 600 }) !== same,
    lut: k({ lut: 'cube' }) !== same,
    adjust: k({ adjust: { exposure_ev: 1 } }) !== same,
    curve: k({ curve: [[0, 0], [1, 1]] }) !== same,
    geometry: k({ geometry: { straighten: 2 } }) !== same,
    crop: k({ crop: { rect: [0, 0, 0.5, 0.5] } }) !== same,
    levels: k({}, { ...recipe, levels: { contrast: 2 } }) !== same,
    slot: k({}, { ...recipe, slot: 3 }) !== same,
  };
  // 不在键里的东西（边框、时间戳、选中哪张卡片）不会让键变：键只由上面这些字段算出
  out.ignores = k({ frame: { style: 'film' }, stamp: { enabled: true }, selected: 'x', tab: 'frame' }) === same;
  const lru = new M.Lru(2);
  lru.set('a', 1);
  lru.set('b', 2);
  lru.get('a');
  lru.set('c', 3);
  out.lru = [lru.get('a'), lru.get('b'), lru.get('c'), lru.size];
} else if (scenario === 'panel' || scenario === 'panel-approx' || scenario === 'panel-mount' || scenario === 'panel-mount-plain' || scenario === 'panel-jpg-none') {
  const approx = scenario === 'panel-approx';
  const mount = scenario.startsWith('panel-mount');
  const none = scenario === 'panel-jpg-none';
  const calls = [];
  const info = (slot, name) => ({ slot, name, is_stock: slot === 4, controls: [{ name: 'amount', label: 'Amount', levels: [0, 50, 100], default: 100 }, { name: 'contrast', label: 'Contrast', levels: [-1, 0, 1], default: 0 }] });
  const mk = (id) => ({ id, recipes: [info(1, 'GR 正片'), info(2, 'GR 负片'), info(4, '原厂标准')], looks: [{ kind: 'look', id: 'acros', name: 'Acros', slot: 3, controls: [] }, { kind: 'look', id: 'odd', name: '没见过的名字', slot: 4, controls: [] }],
    users: [{ kind: 'camera_user', id: 'u1', name: 'FCC-001', slot: 4, controls: [] }] });
  const units = [mk('jlaunch'), mk('qlaunch'), mk('l08firm')];
  const mine = [
    { id: 'abc123abc123', name: '50 的配方', recipe: { kind: 'builtin', unit: 'jlaunch', slot: 2, levels: { amount: 50, contrast: 1 } }, source: { kind: 'lut', title: 'Portra 400' } },
    { id: 'def456def456', name: '28 的配方', recipe: { kind: 'builtin', unit: 'qlaunch', slot: 2, levels: {} }, source: { kind: 'lut', title: null } },
  ];
  const recipeOnEdit = scenario === 'panel' ? { kind: 'look', id: 'acros', unit: 'jlaunch', slot: 3, levels: {} } : scenario === 'panel-mount' ? { kind: 'look', id: 'acros', unit: 'l08firm', slot: 3, levels: {} } : null;
  globalThis.fetch = async (url, opts = {}) => {
    const method = opts.method || 'GET';
    calls.push({ method, url, body: opts.body ? JSON.parse(opts.body) : null });
    let data = {};
    if (url === '/api/luts') data = { luts: [], groups: [] };
    if (url === '/api/photos/p1/edit') data = { rev: 1, recipe: recipeOnEdit, lut: null, adjust: {}, curve: null, geometry: {}, frame: null, stamp: null, memory: { 'jlaunch/2': { contrast: 1 } } };
    if (url === '/api/photos/p1/capabilities') data = none ? { raw: false, recipe: 'none', reason: '' } : approx ? { raw: false, recipe: 'approx', reason: '' } : { raw: true, recipe: 'full', reason: '' };
    if (url === '/api/photos/p1/meta') data = {};
    if (url === '/api/photos/p1/crop') data = { preset: 'original', rect: [0, 0, 1, 1], angle_deg: 0, orientation: 'landscape', frame: { width: 3000, height: 2000 }, rev: 1 };
    if (url === '/api/frames') data = { styles: [] };
    if (url === '/api/defaults/finish') data = { frame: { style: 'none' }, stamp: { enabled: false } };
    if (url === '/api/my-recipes') data = { recipes: mine };
    if (url === '/api/recipes') data = { status: {}, units };
    if (method === 'PUT') data = { rev: 2 };
    return { ok: true, status: 200, text: async () => JSON.stringify(data) };
  };
  const { state } = await import(file('state.js'));
  const preview = await import(file('preview.js'));
  const { recipePanel } = await import(file('recipes.js'));
  state.camdata = { ready: true, state: 'ready' };
  state.catalog = { units };
  const lens = mount ? 'GXR MOUNT A12' : 'GR LENS A12 50mm F2.5 MACRO';
  const view = { photo: { id: 'p1', iso: 200, lens, has_raw: !approx && !none, has_jpg: true }, onChange() {}, redrawSide: () => { view.redraws = (view.redraws || 0) + 1; } };
  await preview.ensureEdit(view);
  recipePanel(view);
  await tick(5);
  const el = recipePanel(view);
  const cards = walk(el).filter((n) => n.classList && n.classList.contains('lcard'));
  const named = (name) => cards.find((c) => c.getAttribute('data-look') && walk(c).some((n) => n.classList && n.classList.contains('nm') && n.textContent === name));
  const card = (name) => {
    const c = named(name);
    return c && { label: c.getAttribute('aria-label'), pressed: c.getAttribute('aria-pressed'), disabled: c.getAttribute('aria-disabled'), tabindex: c.getAttribute('tabindex'), title: c.getAttribute('title') };
  };
  out.names = cards.map((c) => find(c, (n) => n.classList && n.classList.contains('nm')).textContent);
  out.tag = cards[0].tagName;
  out.acros = card('Acros');
  out.odd = card('没见过的名字');
  out.odd.descLines = walk(named('没见过的名字')).filter((n) => n.classList && n.classList.contains('ds')).length;
  out.fcc = card('FCC-001');
  out.mine = card('50 的配方');
  out.text = el.textContent;
  out.rovingZero = cards.filter((c) => c.getAttribute('tabindex') === '0').length;
  out.rovingOnCurrent = cards.find((c) => c.getAttribute('tabindex') === '0')?.getAttribute('data-look');
  out.otherRows = walk(el).filter((n) => n.classList && n.classList.contains('other-recipe')).map((n) => n.textContent);
  out.otherAria = walk(el).filter((n) => n.classList && n.classList.contains('other-recipe')).map((n) => n.getAttribute('aria-disabled'));
  const link = find(el, (n) => n.tagName === 'A');
  out.link = link ? link.getAttribute('href') : null;
  out.hasOtherNamedCard = !!named('28 的配方');
  const calib = find(el, (n) => n.classList && n.classList.contains('calib-note'));
  out.calib = calib ? { text: calib.textContent, title: calib.getAttribute('title') } : null;
  out.chips = walk(el).filter((n) => n.classList && n.classList.contains('chip')).map((n) => n.textContent);
  out.approxLine = walk(el).some((n) => n.textContent === '只有 JPG：配方按近似效果显示，旋钮不能用');
  out.cur = find(el, (n) => n.classList && n.classList.contains('cur-look'))?.textContent || null;
  // 点卡片 = 点原来的配方按钮：写进去的编辑和以前一模一样
  const before = calls.filter((c) => c.method === 'PUT').length;
  const click = (name) => named(name)?.dispatchEvent(Object.assign(new Event('click'), { detail: 1 }));
  click('02 GR 负片');
  await tick(5);
  const puts = calls.filter((c) => c.method === 'PUT');
  out.puts = puts.length - before;
  out.putBody = puts.length > before ? puts[puts.length - 1].body : null;
  out.applied = view.ed.recipe ? JSON.parse(JSON.stringify(view.ed.recipe)) : null;
  // 点没有说明的 Look、点电脑上的配方
  click('没见过的名字');
  await tick(5);
  out.afterOdd = view.ed.recipe ? JSON.parse(JSON.stringify(view.ed.recipe)) : null;
  out.memoryAfterOdd = JSON.parse(JSON.stringify(view.ed.memory));
  const panel2 = recipePanel(view);
  const mineCard = walk(panel2).find((n) => n.getAttribute && n.getAttribute('data-look') === 'computer:abc123abc123');
  mineCard?.dispatchEvent(Object.assign(new Event('click'), { detail: 1 }));
  await tick(5);
  out.afterMine = view.ed.recipe ? JSON.parse(JSON.stringify(view.ed.recipe)) : null;
  // 键盘：方向键换焦点，并且不让大图把它当成换照片
  const list = walk(panel2).filter((n) => n.classList && n.classList.contains('lcard'));
  list[0].focus();
  const key = (k) => { const e = Object.assign(new Event('keydown', { cancelable: true }), { key: k }); let stopped = false; e.stopPropagation = () => { stopped = true; }; list[0].dispatchEvent(e); return [e.defaultPrevented, stopped]; };
  out.rightKey = key('ArrowRight');
  out.focusAfterRight = document.activeElement === list[1];
  out.tabindexAfterRight = [list[0].getAttribute('tabindex'), list[1].getAttribute('tabindex')];
  out.otherKey = key('a');
} else {
  throw new Error(`没有这个场景：${scenario}`);
}

console.log(JSON.stringify(out));
process.exit(0);
