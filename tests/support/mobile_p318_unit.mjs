// 编辑器 P3-18（backlog-4b）的两处小改动，Node --experimental-strip-types + mini_dom：
// a) 配方上叠了 LUT：编辑器顶栏写「配方 + LUT 名」；没叠、LUT 记录没了时照旧只写配方。
// b) 没写明输入色彩空间的 LUT：色彩空间面板一打开，默认用的那个（sRGB）就标成选中；写明的照旧标它自己，「需选择」的一个都不标。
// （c）电脑上不画拖动条是 CSS，浏览器里查：test_mobile_grid_tab_browser.py。

import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import test from 'node:test';
import { installDom } from './mini_dom.mjs';

registerHooks({
  resolve(spec, ctx, next) {
    if (/\.(css|ttf|woff2?|png|svg|txt)(\?.*)?$/.test(spec)) {
      return { url: 'data:text/javascript,export default "";', shortCircuit: true };
    }
    if (spec === './export_ui') {
      return { url: 'data:text/javascript,export function openExportOptions(){return false;}', shortCircuit: true };
    }
    try {
      return next(spec, ctx);
    } catch (err) {
      if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(`${spec}.ts`, ctx);
      throw err;
    }
  },
  load(url, ctx, next) {
    const res = next(url, ctx);
    if (!url.endsWith('/paste_state.ts')) return res;
    const text = typeof res.source === 'string' ? res.source : Buffer.from(res.source).toString('utf8');
    return { ...res, source: text.replaceAll('import.meta.env.DEV', 'false') };
  },
});

installDom();
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };

function photo(lut) {
  return {
    id: 'p1', stem: 'p1', version: 1, dng: { name: 'p1.DNG' }, jpg: null, diptych: false, copy: 0, status: 'ready', gone: false,
    jpgMeta: null, dngMeta: null, choice: null, recipeOff: true, levels: new Map(), memory: new Map(), looks: new Map(), lut,
  };
}

function api(records) {
  const book = { dat: null, usableDatFor: () => null, onChange: () => () => {} };
  return {
    book: () => book,
    lutBook: () => ({ get: (id) => (id ? records[id] : undefined) }),
    back() {},
    swipe() {},
  };
}

test('a. a LUT stacked on the recipe shows in the editor title as 「配方 + LUT 名」', async () => {
  const { mountEditorNav } = await import('../../mobile/src/app/editor_nav.ts');
  const shell = document.createElement('div');
  document.body.append(shell);
  const fake = api({ c1: { cube_id: 'c1', name: 'Cool 33' } });
  const title = (p) => {
    const nav = mountEditorNav(shell, fake, p, 0, 3);
    const text = nav.el.querySelector('.t').children[0].textContent;
    nav.destroy();
    return text;
  };
  assert.equal(title(photo(null)), '中性转换', 'no LUT: the recipe alone');
  assert.equal(title(photo('c1')), '中性转换 + Cool 33', 'LUT stacked: recipe + LUT name');
  assert.equal(title(photo('gone')), '中性转换', 'a LUT whose record is gone: the recipe alone');
  // 叠上 LUT 以后编辑器调 refresh()，顶栏跟着变。
  const p = photo(null);
  const nav = mountEditorNav(shell, fake, p, 0, 3);
  p.lut = 'c1';
  nav.refresh();
  assert.equal(nav.el.querySelector('.t').children[0].textContent, '中性转换 + Cool 33', 'refresh picks up a newly stacked LUT');
  nav.destroy();
});

function record(extra) {
  return {
    cube_id: 'u1', name: 'Plain', filename: 'plain.cube', title: '', comments: [], input_space: null, input_space_manual: false,
    space_evidence: {}, ...extra,
  };
}

function openSheet(openLutSpaceSheet, rec) {
  const host = document.createElement('div');
  document.body.append(host);
  openLutSpaceSheet(host, { setSpace: async () => ({ ok: false, message: '' }) }, rec, { hasRaw: true, onUpdated() {} });
  const rows = host.querySelectorAll('.lut-space-opt');
  const on = rows.filter((row) => row.classList.contains('on'));
  const pressed = rows.filter((row) => row.getAttribute('aria-pressed') === 'true');
  const checked = rows.filter((row) => row.querySelector('.cc')?.classList.contains('on'));
  host.remove();
  return { count: rows.length, on: on.map((r) => r.dataset.space), pressed: pressed.map((r) => r.dataset.space), checked: checked.map((r) => r.dataset.space) };
}

test('b. the untagged-LUT colour-space sheet opens with the default (sRGB) selected', async () => {
  const { openLutSpaceSheet } = await import('../../mobile/src/app/lut_sheet.ts');
  const plain = openSheet(openLutSpaceSheet, record({}));
  assert.equal(plain.count, 12, 'all 12 spaces listed');
  assert.deepEqual(plain.on, ['srgb'], 'untagged: the default sRGB row is selected');
  assert.deepEqual(plain.pressed, ['srgb'], 'untagged: aria-pressed on the default');
  assert.deepEqual(plain.checked, ['srgb'], 'untagged: the radio circle is filled on the default');
  // 写明了的照旧标它自己；对不上的（需选择）一个都不标。
  const declared = openSheet(openLutSpaceSheet, record({ input_space: 'rec709', space_evidence: { input_declarations: ['Rec.709'] } }));
  assert.deepEqual(declared.on, ['rec709'], 'declared: its own space stays selected');
  const conflict = openSheet(openLutSpaceSheet, record({ input_space_state: 'conflict' }));
  assert.deepEqual(conflict.on, [], 'needs a choice: nothing is pre-selected');
});
