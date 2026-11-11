import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { installDom } from './mini_dom.mjs';
installDom();
const url = (name) => pathToFileURL(`${process.argv[2]}/${name}`).href;
const { state } = await import(url('state.js'));
const { recipeForRender } = await import(url('preview.js'));
const selection = { kind: 'record', id: 'f'.repeat(64), unit: 'jlaunch', slot: 6, levels: { contrast: -1, saturation: 2 } };
state.catalog = { units: [{ id: 'jlaunch', recipes: [{ ...selection, controls: [
  { name: 'contrast', levels: [-1, 0, 1] }, { name: 'saturation', levels: [0, 1, 2] },
] }] }] };
const view = { photo: { recipe_tag: { knob_code: 'A8999B999-' } }, ed: { recipe: selection, memory: {}, caps: { recipe: 'full' } } };
assert.deepEqual(recipeForRender(view).levels, { contrast: -1, saturation: 2 });
view.ed.caps.recipe = 'approx';
assert.deepEqual(recipeForRender(view).levels, { contrast: -1, saturation: 2 });
view.ed.caps.recipe = 'none';
assert.equal(recipeForRender(view), null);
delete view.photo.recipe_tag;
view.ed.caps.recipe = 'approx';
assert.deepEqual(recipeForRender(view).levels, {});
view.ed.recipe = { kind: 'local', id: 'd'.repeat(64), unit: 'jlaunch', slot: 1, levels: { contrast: 1 } };
view.ed.caps.recipe = 'none';
assert.deepEqual(recipeForRender(view), view.ed.recipe, '当前校准缺失不能把显式本机引用清成空配方');
console.log(JSON.stringify({ actualKnobsKept: true, unsupportedPredictionDisabled: true, legacyApproxUnchanged: true }));
