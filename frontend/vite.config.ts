// 库模式：一个不依赖外部包的 ES 模块，写到 web/assets/gpu/engine.js。产物提交进仓库，
// 这样双击「GXR Studio.command」不需要 Node；CI 会重新构建并核对产物和源码一致（scripts/check_frontend.py）。
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

// GXR_GPU_OUT：scripts/check_frontend.py 构建到临时目录再和提交的产物比较
const out = process.env.GXR_GPU_OUT || fileURLToPath(new URL('../web/assets/gpu', import.meta.url));

export default defineConfig({
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
