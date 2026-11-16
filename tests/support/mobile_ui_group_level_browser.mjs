// UI-M3-GROUP（Sol r2）：配方旋钮的同值提交。真的 RecipeView 在开发页里建出来，配方列表和照片都是合成的（不读照片）；
// 用受控的 DOM 事件发 input / change。只输出一行 JSON。

import { pathToFileURL } from 'node:url';

const [runtime, url] = process.argv.slice(2);
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();
const browser = await launchPinnedChromium();
const out = { pageErrors: [] };
try {
  const page = await browser.newPage({ viewport: { width: 440, height: 956 } });
  await page.addInitScript(() => Object.defineProperty(navigator, 'gpu', { value: undefined }));
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  await page.goto(url);
  await page.waitForSelector('.app[data-core="1"]', { timeout: 30_000 });
  out.cases = await page.evaluate(async () => {
    const { RecipeView } = await import('/src/app/recipe_view.ts');
    const { copyFrom, pasteOne, PasteUndo, undoBatch } = await import('/src/lib/paste.ts');
    const { zeroKnobs } = await import('/src/app/model.ts');
    const control = { name: 'amount', label: '强度', min: 0, max: 100, default: 100, cap_lower: true, cap_upper: true, amount_step: 10, levels: [0, 50, 80, 100] };
    const listing = { units: [{ id: 'jlaunch', recipes: Array.from({ length: 8 }, (_, i) => ({ slot: i + 1, controls: [control] })), looks: [] }] };
    const photo = (id, over = {}) => ({ id, stem: id, version: 1, rev: 0, dng: new File(['synthetic'], `${id}.DNG`), jpg: null, lastModified: 0, captureMs: null, status: 'ready', gone: false, cached: null, checked: null, bitmap: null, neutral: null, jpgMeta: null, dngMeta: null, choice: null, levels: new Map(), memory: new Map(), knobs: zeroKnobs(), looks: new Map(), ...over });
    const book = { dat: { listing, generation: 158 }, datError: null, datNote: null, onChange: () => () => {}, unitState: () => ({ state: 'ready' }), bakeKey: () => null, ready: () => false, bake: () => new Promise(() => {}), gpuKey: () => null, pin() {} };
    const env = { recipeWithoutRaw: false, listed: () => true };
    const rows = [];
    // stored：来源把强度 100 显式存下；default：没存（桌面的稀疏档位，强度取默认 100）。
    for (const action of ['default-same', 'default-roundtrip', 'stored-same', 'changed']) {
      const stored = action.startsWith('stored');
      const source = photo('source', { choice: { id: 'builtin:2', unit: 'jlaunch' }, levels: new Map([['builtin:2', stored ? { amount: 100 } : {}]]) });
      const target = photo('target');
      const byId = new Map([['source', source], ['target', target]]);
      const api = { photo: (id) => byId.get(id), photos: () => [...byId.values()], session: () => 1, book: () => book, pickDat() {}, pickCalib() {} };
      const clip = copyFrom(source, 'source', ['recipe']).clip;
      const undo = new PasteUndo();
      const pasted = pasteOne({ photo: target, raw: true, unit: null }, clip, clip.groups, env);
      undo.add('b', target.id, target.rev, pasted.before);
      const view = new RecipeView(api, null, target, { mode: () => 'raw', look() {}, dragging: () => false, recipeTab: () => true });
      const host = document.createElement('div');
      host.className = 'app';
      host.append(view.stripEl(), view.panelEl());
      document.body.append(host);
      const input = host.querySelector('.krow[data-knob="amount"] input');
      const at = input.value;
      const beforeRev = target.rev;
      if (action.endsWith('roundtrip')) {
        input.value = '0';
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }
      input.value = action === 'changed' ? '1' : at;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      const afterRev = target.rev;
      const levels = Object.fromEntries(target.levels);
      const counts = undoBatch(undo.take('b'), (id) => byId.get(id)).counts;
      rows.push({ action, beforeRev, afterRev, levels, counts, choiceAfterUndo: target.choice });
      host.remove();
      view.destroy();
    }
    return rows;
  });
} catch (error) {
  out.error = String(error?.stack ?? error);
} finally {
  await browser.close();
}
console.log(JSON.stringify(out));
if (out.error) process.exit(1);
