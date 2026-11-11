// 电脑上的「我的配方」（用 LUT 生成，存在应用数据里，/api/my-recipes）。
// 相机配方文件（GXRCE.DAT）里的自定义配方是另一回事：同样叫「我的配方」，来源写在后面——「相机里」和「电脑上」。
// 大图转交服务返回的稳定完整记录引用，名称隐藏后已有照片和撤销仍能解析。

import { api } from './api.js';
import { on } from './state.js';

let cache = null; // 还没读过 = null
let pending = null;
const retained = new Map();

export function rememberRecipe(recipe) {
  if (recipe?.recipe?.kind === 'local') retained.set(recipe.id, recipe);
}

export function localRecipe(id) {
  return retained.get(id) || null;
}

// 配方页存、删、改名之后通知这里：下次打开大图的配方标签重新读
export const MY_RECIPES_CHANGED = 'my-recipes-changed';
on(MY_RECIPES_CHANGED, () => {
  cache = null;
  pending = null;
});

export function cachedMyRecipes() {
  return cache;
}

// 读失败当作「还没有」：配方标签照常显示，只是少了这一组
export function loadMyRecipes() {
  if (cache) return Promise.resolve(cache);
  if (!pending) {
    const mine = api('/api/my-recipes').then((r) => r.recipes || [], () => []);
    pending = mine;
    mine.then((list) => {
      for (const recipe of list) rememberRecipe(recipe);
      if (pending === mine) cache = list;
    });
  }
  return pending;
}

// 来源：「电脑上 · 来自 LUT「名字」」；LUT 没有名字就不写
export function sourceText(r) {
  const title = r.source && r.source.title;
  const quality = { passed: '质量门通过', rejected: '未通过准入 · 本机草稿', needs_evidence: '质量证据未齐 · 本机草稿', historical_exception: '历史授权收录' }[r.quality?.state];
  return `电脑上 · 来自 LUT${title ? `「${title}」` : ''}${quality ? ` · ${quality}` : ''}`;
}

export const CAMERA_SOURCE = '相机里';
