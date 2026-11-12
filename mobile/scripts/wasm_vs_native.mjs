// 同一批 DNG：WASM 的 dng_describe 必须和 gxr-cli 的 stdout 逐字节相同，
// 半分辨率 float32 的 SHA-256 必须和 CLI 写下的 .npy 数据段相同。
// 用法：node mobile/scripts/wasm_vs_native.mjs <paths.json> <gxr-cli>
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const [listPath, cli] = process.argv.slice(2);
if (!listPath || !cli) {
  console.error('usage: node wasm_vs_native.mjs <paths.json> <gxr-cli>');
  process.exit(1);
}

const pkg = fileURLToPath(new URL('../src/wasm-pkg/gxr_wasm.js', import.meta.url));
const wasmPath = fileURLToPath(new URL('../src/wasm-pkg/gxr_wasm_bg.wasm', import.meta.url));
const wasm = await import(pathToFileURL(pkg).href);
wasm.initSync({ module: readFileSync(wasmPath) });

const paths = JSON.parse(readFileSync(listPath, 'utf8'));
const dir = mkdtempSync(join(tmpdir(), 'gxr-wasm-'));

function npyPayload(buf) {
  if (buf.subarray(0, 6).toString('latin1') !== '\u0093NUMPY') throw new Error('not an npy file');
  const headerLen = buf[6] === 1 ? buf.readUInt16LE(8) : buf.readUInt32LE(8);
  const headerAt = buf[6] === 1 ? 10 : 12;
  const header = buf.subarray(headerAt, headerAt + headerLen).toString('latin1');
  const shape = header
    .match(/'shape': \(([^)]*)\)/)[1]
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
    .map(Number);
  return { shape, payload: buf.subarray(headerAt + headerLen) };
}

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

let failed = 0;
try {
  for (const file of paths) {
    const bytes = new Uint8Array(readFileSync(file));
    const described = spawnSync(cli, ['dng-describe', file], { encoding: 'utf8' });
    const fromWasm = wasm.dng_describe(bytes);
    if (described.stdout !== fromWasm) {
      console.error(`dng_describe differs: ${file}`);
      failed += 1;
      continue;
    }
    const out = join(dir, 'half.npy');
    const halfCli = spawnSync(cli, ['dng-half-codes', file, '--out', out], { encoding: 'utf8' });
    if (halfCli.status !== 0) {
      let message = '';
      try {
        wasm.dng_half_codes(bytes);
      } catch (err) {
        message = err instanceof Error ? err.message : String(err);
      }
      if (message.trim() !== (halfCli.stdout || '').trim()) {
        console.error(`half-codes differ: ${file}`);
        failed += 1;
      }
      continue;
    }
    const npy = npyPayload(readFileSync(out));
    const half = wasm.dng_half_codes(bytes);
    const height = half.height;
    const width = half.width;
    const values = half.values();
    const raw = Buffer.from(values.buffer, values.byteOffset, values.byteLength);
    if (sha256(raw) !== sha256(npy.payload) || height !== npy.shape[0] || width !== npy.shape[1]) {
      console.error(`half-codes differ: ${file}`);
      failed += 1;
    }
  }
} finally {
  rmSync(dir, { recursive: true, force: true });
}

if (failed) process.exit(1);
console.log(`wasm matches native on ${paths.length} files`);
