// 从 common.wgsl 的 recipeChain 派生半格第二张用的 recipeChain2（仅重命名 rec[ -> rec2[、p.bake -> pp.bake）。
// 同时供 frontend/vite.config.ts 构建期预展开与 frontend/src/pair.ts 运行期兜底使用，避免任何未挂插件的 Vite 配置（如 mobile）漏展开。

export function generateRecipeChain2(commonSrc: string): string {
  const m = commonSrc.match(/fn recipeChain\([^)]*\)\s*->\s*vec3f\s*\{[\s\S]*?\n\}/);
  if (!m) throw new Error('common.wgsl: recipeChain function not found');
  let body = m[0];
  const fnReplaced = body.replace('fn recipeChain(', 'fn recipeChain2(');
  if (fnReplaced === body) throw new Error('recipeChain2: function name rename failed');
  body = fnReplaced;
  const recReplaced = body.replaceAll('rec[', 'rec2[');
  if (recReplaced === body) throw new Error('recipeChain2: rec[ rename failed');
  body = recReplaced;
  const bakeReplaced = body.replaceAll('p.bake', 'pp.bake');
  if (bakeReplaced === body) throw new Error('recipeChain2: p.bake rename failed');
  return bakeReplaced;
}

export function expandPairColorWgsl(commonSrc: string, pairSrc: string): string {
  if (pairSrc.includes('fn recipeChain2(')) return pairSrc;
  const fn2 = generateRecipeChain2(commonSrc);
  const anchor = '@vertex fn vs(';
  const idx = pairSrc.indexOf(anchor);
  if (idx < 0) throw new Error('pair_color.wgsl: @vertex fn vs not found');
  return `${pairSrc.slice(0, idx)}${fn2}\n\n${pairSrc.slice(idx)}`;
}
