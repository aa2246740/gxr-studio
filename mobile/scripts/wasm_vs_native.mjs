// 同一批 DNG：WASM 和 gxr-cli 必须给出相同的成功字节，或相同的 kind 与 message。
// CLI 只有退出码正好是 2、且 stdout 是 {"error":{"kind","message"}} 时才算失败结果。
// 其它退出码、信号、spawn 错误、空 stdout、无法解析、超时，都是脚本失败，不能当成一致。
// 用法：node mobile/scripts/wasm_vs_native.mjs <paths.json> <gxr-cli>
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const TIMEOUT_MS = 60_000;
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

function spawnCli(args) {
  const proc = spawnSync(cli, args, { encoding: 'utf8', timeout: TIMEOUT_MS });
  if (proc.error) {
    const timedOut = proc.error.code === 'ETIMEDOUT';
    throw new Error(timedOut ? `timeout ${args.join(' ')}` : `spawn ${proc.error.message}`);
  }
  if (proc.signal) throw new Error(`signal ${proc.signal}`);
  return proc;
}

function cliSide(proc) {
  if (proc.status === 0) return { ok: true, stdout: proc.stdout ?? '' };
  if (proc.status !== 2) throw new Error(`status ${proc.status}`);
  if (!proc.stdout || !proc.stdout.trim()) throw new Error('empty stdout');
  let parsed;
  try {
    parsed = JSON.parse(proc.stdout);
  } catch {
    throw new Error('unparseable stdout');
  }
  const err = parsed && parsed.error;
  if (!err || typeof err.kind !== 'string' || typeof err.message !== 'string') {
    throw new Error('stdout is not a core error');
  }
  return { ok: false, kind: err.kind, message: err.message };
}

function wasmFailure(err) {
  const text = err instanceof Error ? err.message : String(err);
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error(`wasm error is not core JSON: ${text}`);
  }
  if (!parsed.error || typeof parsed.error.kind !== 'string' || typeof parsed.error.message !== 'string') {
    throw new Error('wasm error is not a core error');
  }
  return { ok: false, kind: parsed.error.kind, message: parsed.error.message };
}

function sameError(left, right) {
  return !left.ok && !right.ok && left.kind === right.kind && left.message === right.message;
}

function floatView(block) {
  const height = block.height;
  const width = block.width;
  const values = block.values();
  return {
    height,
    width,
    raw: Buffer.from(values.buffer, values.byteOffset, values.byteLength),
  };
}

let failed = 0;
try {
  for (const file of paths) {
    const name = basename(file);
    const bytes = new Uint8Array(readFileSync(file));
    try {
      const described = cliSide(spawnCli(['dng-describe', file]));
      let fromWasm;
      try {
        const text = wasm.dng_describe_named(bytes, name);
        const parsed = JSON.parse(text);
        fromWasm = parsed.error
          ? { ok: false, kind: parsed.error.kind, message: parsed.error.message }
          : { ok: true, text };
      } catch (err) {
        fromWasm = wasmFailure(err);
      }
      if (described.ok && fromWasm.ok) {
        if (described.stdout !== fromWasm.text) throw new Error('dng_describe bytes differ');
      } else if (!sameError(described, fromWasm)) {
        throw new Error('dng_describe error differs');
      }

      const halfOut = join(dir, 'half.npy');
      const halfCli = cliSide(spawnCli(['dng-half-codes', file, '--out', halfOut]));
      let halfWasm;
      try {
        const half = floatView(wasm.dng_half_codes_named(bytes, name));
        halfWasm = { ok: true, raw: half.raw, height: half.height, width: half.width };
      } catch (err) {
        halfWasm = wasmFailure(err);
      }
      if (halfCli.ok && halfWasm.ok) {
        const npy = npyPayload(readFileSync(halfOut));
        if (sha256(halfWasm.raw) !== sha256(npy.payload) || halfWasm.height !== npy.shape[0] || halfWasm.width !== npy.shape[1]) {
          throw new Error('half-codes bytes differ');
        }
      } else if (!sameError(halfCli, halfWasm)) {
        throw new Error('half-codes error differs');
      }

      const wbOut = join(dir, 'wb.npy');
      const wbCli = cliSide(spawnCli(['dng-wb-block', file, '--rows', '0:1', '--out', wbOut]));
      let wbWasm;
      try {
        const block = floatView(wasm.dng_wb_block_named(bytes, name, 0, 1));
        wbWasm = { ok: true, raw: block.raw, height: block.height, width: block.width };
      } catch (err) {
        wbWasm = wasmFailure(err);
      }
      if (wbCli.ok && wbWasm.ok) {
        const npy = npyPayload(readFileSync(wbOut));
        if (sha256(wbWasm.raw) !== sha256(npy.payload) || wbWasm.height !== npy.shape[0] || wbWasm.width !== npy.shape[1]) {
          throw new Error('wb-block bytes differ');
        }
      } else if (!sameError(wbCli, wbWasm)) {
        throw new Error('wb-block error differs');
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      console.error(`${file}: ${message}`);
      failed += 1;
    }
  }
} finally {
  rmSync(dir, { recursive: true, force: true });
}

if (failed) process.exit(1);
console.log(`wasm matches native on ${paths.length} files`);
