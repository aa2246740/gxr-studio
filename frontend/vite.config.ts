// 库模式：一个不依赖外部包的 ES 模块，写到 web/assets/gpu/engine.js。产物提交进仓库，
// 这样双击「GXR Studio.command」不需要 Node；CI 会重新构建并核对产物和源码一致（scripts/check_frontend.py）。
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { defineConfig, type Plugin } from 'vite';

// GXR_GPU_OUT：scripts/check_frontend.py 构建到临时目录再和提交的产物比较
const out = process.env.GXR_GPU_OUT || fileURLToPath(new URL('../web/assets/gpu', import.meta.url));
const commonWgslPath = fileURLToPath(new URL('src/shaders/common.wgsl', import.meta.url));
const pairColorWgslPath = fileURLToPath(new URL('src/shaders/pair_color.wgsl', import.meta.url));

function generateRecipeChain2(commonSrc: string): string {
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

function pairShaderPlugin(): Plugin {
  return {
    name: 'gxr-pair-recipe-chain2',
    enforce: 'pre',
    load(id) {
      if (!id.endsWith('/shaders/pair_color.wgsl?raw') && !id.endsWith('\\shaders\\pair_color.wgsl?raw')) return null;
      this.addWatchFile(commonWgslPath);
      this.addWatchFile(pairColorWgslPath);
      const commonSrc = readFileSync(commonWgslPath, 'utf8');
      const pairSrc = readFileSync(pairColorWgslPath, 'utf8');
      const marker = '// __RECIPE_CHAIN_2__';
      if (!pairSrc.includes(marker)) throw new Error('pair_color.wgsl: missing // __RECIPE_CHAIN_2__ marker');
      const expanded = pairSrc.replace(marker, generateRecipeChain2(commonSrc));
      return `export default ${JSON.stringify(expanded)};`;
    },
  };
}

export default defineConfig({
  plugins: [pairShaderPlugin()],
  build: {
    outDir: out,
    emptyOutDir: true,
    target: 'es2022',
    minify: true,
    sourcemap: false,
    reportCompressedSize: false,
    // 库模式的 ES 产物默认保留换行和缩进（只改名字）；去掉空白，产物才不会随功能变长、撞上 800 行的文件上限（scripts/large_files.py）
    rolldownOptions: { output: { minify: { compress: true, mangle: true, codegen: { removeWhitespace: true } } } },
    lib: {
      entry: fileURLToPath(new URL('src/index.ts', import.meta.url)),
      formats: ['es'],
      fileName: () => 'engine.js',
    },
  },
});
