import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { installDom, tick } from './mini_dom.mjs';

installDom();
const catalog = JSON.parse(readFileSync(0, 'utf8'));
const file = (name) => pathToFileURL(`${process.argv[2]}/${name}`).href;
const walk = (n) => [n, ...(n.children || []).flatMap(walk)];
const has = (n, cls) => n.classList?.contains(cls);
const saved = catalog.units.flatMap((u) => u.recipes.map((r, i) => ({
  id: `${u.id}-${i}`, name: `Synthetic ${i}`, camera_short_name: 'TEST',
  recipe: { unit: u.id, kind: r.kind || 'builtin', slot: r.slot, id: r.id, levels: {} },
  fit: { similarity: 100 }, source: { kind: 'lut', title: 'Synthetic', input_space_guessed: false },
})));
globalThis.fetch = async (url) => {
  let data = {};
  if (url === '/api/recipes') data = catalog;
  if (url === '/api/camdata/status') data = { ready: true, camera_data: { state: 'ready', imported: true }, calibration: { state: 'missing' }, cards: [] };
  if (url === '/api/my-recipes') data = { recipes: saved };
  if (url === '/api/lut/spaces') data = { spaces: [], units: [] };
  if (url === '/api/luts') data = { luts: [], groups: [] };
  if (url.startsWith('/api/photos')) data = [];
  return { ok: true, status: 200, text: async () => JSON.stringify(data) };
};
const { state } = await import(file('state.js'));
state.catalog = catalog;
const { slotName, recipeLabel } = await import(file('preview.js'));
const { renderCamera } = await import(file('camera_page.js'));
const root = document.createElement('div');
document.body.append(root);
renderCamera(root);
await tick(8);
const camera = walk(root).filter((n) => has(n, 'unit')).map((unit) => walk(unit).filter((n) => n.tagName === 'LI').map((row) => ({
  number: walk(row).find((n) => has(n, 'num')).textContent,
  name: walk(row).find((n) => has(n, 'nm')).textContent,
})));
const { renderLutPage } = await import(file('lut.js'));
renderLutPage(root);
await tick(12);
const lut = walk(root).filter((n) => has(n, 'recipe-card')).map((card) => card.children.find((n) => has(n, 'small')).textContent);
const bases = catalog.units.map((u) => Array.from({ length: 8 }, (_, i) => slotName(u.id, i + 1)));
const labels = catalog.units.map((u) => u.recipes.map((r) => recipeLabel(u.id, r)));
console.log(JSON.stringify({ camera, lut, bases, labels }));
process.exit(0);
