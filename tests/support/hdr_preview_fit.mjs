// 用和 Worker 相同的 fitPreviewGrid 跑一张预览 RGB。stdout 只有 JSON，没有路径。
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';

registerHooks({
  resolve(spec, ctx, next) {
    try {
      return next(spec, ctx);
    } catch (err) {
      if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(`${spec}.ts`, ctx);
      throw err;
    }
  },
});

const [rgbPath, width, height, dngPath, jpgPath] = process.argv.slice(2);
const wasm = await import('../../mobile/src/wasm-pkg/gxr_wasm.js');
wasm.initSync({ module: readFileSync(new URL('../../mobile/src/wasm-pkg/gxr_wasm_bg.wasm', import.meta.url)) });
const { fitPreviewGrid } = await import('../../mobile/src/core/hdr_grid.ts');
const rgb = new Uint8Array(readFileSync(rgbPath));
const dng = new Uint8Array(readFileSync(dngPath));
const jpg = jpgPath ? new Uint8Array(readFileSync(jpgPath)) : null;
const grid = fitPreviewGrid(wasm, rgb, Number(width), Number(height), dng, jpg);
const memory = wasm.memory?.buffer?.byteLength ?? 0;
if (!grid.ok) {
  process.stdout.write(JSON.stringify({ ok: false, reason: grid.reason, wasmBytes: memory, rss: process.memoryUsage().rss }));
} else {
  process.stdout.write(JSON.stringify({
    ok: true,
    width: grid.width,
    height: grid.height,
    codes: Buffer.from(grid.codes).toString('base64'),
    meta: grid.meta,
    wasmBytes: memory,
    rss: process.memoryUsage().rss,
    gridBytes: grid.codes.byteLength,
  }));
}
