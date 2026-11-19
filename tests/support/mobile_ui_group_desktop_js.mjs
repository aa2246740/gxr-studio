// UI-M3-GROUP 第 7 项：桌面那一边用桌面自己的 JS。从 web/assets 把纯函数原样抠出来在 vm 里跑：
// - preview.js 的 selection / recipeInfo / recipeKey / cleanLevels / ensureRecipe / recipeBody 和 look_cards.js 的
//   recipeTarget：按手机上同样的点卡、动旋钮的顺序走一遍，得到桌面界面会交给 PUT /edit 的 recipe（带 memory）；
// - paste_core.js 的 pasteMessage / undoMessage：桌面结果说成的那句话。
// 用法：node mobile_ui_group_desktop_js.mjs <in.json> <out.json>。结果写文件，不打印。

import { readFileSync, writeFileSync } from 'node:fs';
import vm from 'node:vm';

const ROOT = new URL('../../', import.meta.url);
const read = (name) => readFileSync(new URL(`web/assets/${name}`, ROOT), 'utf8');

function block(source, start, end) {
  const at = source.indexOf(start);
  if (at < 0) throw new Error(`desktop source lost: ${start}`);
  const stop = source.indexOf(end, at);
  if (stop < 0) throw new Error(`desktop source: no end for ${start}`);
  return source.slice(at, stop + end.length).replace(/^export /gm, '');
}

// P1b-ui moved the recipe helpers out of preview.js
const PREVIEW = read('preview_recipe.js');
const LOOKS = read('look_cards.js');
const CORE = read('paste_core.js');

const [input, output] = process.argv.slice(2);
const job = JSON.parse(readFileSync(input, 'utf8'));
const out = {};

if (job.recipe) {
  const { catalog, photoUnit, actions } = job.recipe;
  const code = [
    block(PREVIEW, 'export function selection(view) {', '\n}\n'),
    block(PREVIEW, 'export function recipeInfo(view, sel = selection(view)) {', '\n}\n'),
    block(PREVIEW, 'export function recipeKey(sel) {', '\n}\n'),
    block(PREVIEW, 'export function cleanLevels(info, levels) {', '\n}\n'),
    block(PREVIEW, 'export function ensureRecipe(view) {', '\n}\n'),
    block(PREVIEW, 'export function recipeBody(view, { memory = false } = {}) {', '\n}\n'),
    block(LOOKS, 'export function recipeTarget(view, target) {', '\n}\n'),
    '({ recipeTarget, ensureRecipe, recipeBody })',
  ].join('\n');
  const ctx = vm.createContext({
    state: { catalog },
    unitForPhoto: () => photoUnit,
    localRecipe: () => null,
    Object,
    JSON,
  });
  const desk = vm.runInContext(code, ctx);
  // 桌面一张没编辑过的照片：没有 recipe，memory 是空的（preview.js：ed.memory = edit.memory || {}）。
  const view = { photo: { id: 'p' }, ed: { recipe: null, memory: {} } };
  for (const action of actions) {
    if (action.pick) {
      // recipes.js switchRecipe：same 就不动；否则 memory 和 recipe 换成 recipeTarget 给的。
      const t = desk.recipeTarget(view, action.pick);
      if (!t.same) {
        view.ed.memory = t.memory;
        view.ed.recipe = t.next;
      }
    } else if (action.knob) {
      // recipes.js sliderEl 的 setLevelAt：ensureRecipe(view).levels[name] = level。
      desk.ensureRecipe(view).levels[action.knob.name] = action.knob.level;
    }
  }
  out.body = JSON.parse(JSON.stringify(desk.recipeBody(view, { memory: true })));
}

if (job.paste || job.undo) {
  const code = [
    block(CORE, 'export const PASTE_GROUPS = [', '\n];'),
    block(CORE, 'export const groupName = ', ';\n'),
    block(CORE, 'const SKIP_LINES = {', '\n};'),
    block(CORE, 'const ERROR_LINES = {', '\n};'),
    block(CORE, 'export function pasteMessage(', '\n}\n'),
    block(CORE, 'export function undoMessage(', '\n}\n'),
    '({ pasteMessage, undoMessage })',
  ].join('\n');
  const desk = vm.runInContext(code, vm.createContext({ Map, Object }));
  out.pasteLines = (job.paste ?? []).map((results) => desk.pasteMessage(results));
  out.undoLines = (job.undo ?? []).map((counts) => desk.undoMessage({ counts }));
}

writeFileSync(output, JSON.stringify(out));
