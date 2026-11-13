// 同一批输入：WASM 和 gxr-cli 必须给出相同的成功字节，或相同的 kind 与 message。
// CLI 只有退出码正好是 2、且 stdout 是 {"error":{"kind","message"}} 时才算失败结果。
// 其它退出码、信号、spawn 错误、空 stdout、无法解析、超时，都是脚本失败，不能当成一致。
// 一边成功一边失败也是失败。每个子进程最多 60 秒。
// 用法：node wasm_vs_native.mjs --area <name> --cli <path> --inputs <json>
//
// 领域模块导出 cases(inputs)，每项是 {label, cliArgs, wasm}。
// label 用「<文件>::<检查名>」。字节或错误不一致时，输出仍是「<文件>: <检查名> bytes differ」。
// wasm 返回 string 时和 CLI 的 stdout 比；返回 Uint8Array 时，先比 --out 的 npy 宽高，再比样本字节。
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

const TIMEOUT_MS = 60_000;

function flag(name) {
  const at = process.argv.indexOf(name);
  if (at < 0 || at + 1 >= process.argv.length) return '';
  return process.argv[at + 1];
}

const area = flag('--area');
const cli = flag('--cli');
const listPath = flag('--inputs');
if (!area || !cli || !listPath || !/^[a-z][a-z0-9_-]*$/.test(area)) {
  console.error('usage: node wasm_vs_native.mjs --area <name> --cli <path> --inputs <json>');
  process.exit(1);
}

const wasm = await import(new URL('../src/wasm-pkg/gxr_wasm.js', import.meta.url).href);
wasm.initSync({ module: readFileSync(new URL('../src/wasm-pkg/gxr_wasm_bg.wasm', import.meta.url)) });

const inputs = JSON.parse(readFileSync(listPath, 'utf8'));
if (!Array.isArray(inputs)) {
  console.error('inputs JSON must be an array');
  process.exit(1);
}

const areaMod = await import(new URL(`./wasm_cases/${area}.mjs`, import.meta.url).href);
if (typeof areaMod.cases !== 'function') {
  console.error(`area ${area} does not export cases`);
  process.exit(1);
}
const list = areaMod.cases(inputs);
if (!Array.isArray(list)) {
  console.error('cases() did not return a list');
  process.exit(1);
}

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

function framed(height, width, payload) {
  const header = Buffer.alloc(8);
  header.writeUInt32LE(height, 0);
  header.writeUInt32LE(width, 4);
  return Buffer.concat([header, payload]);
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

function bytesOf(cliArgs, stdout, value) {
  if (typeof value === 'string') {
    if (value !== stdout) throw new Error('bytes differ');
    return;
  }
  if (!(value instanceof Uint8Array)) throw new Error('wasm result is not bytes or string');
  const at = cliArgs.lastIndexOf('--out');
  if (at < 0 || !cliArgs[at + 1]) throw new Error('wasm returned bytes but the command has no --out');
  const npy = npyPayload(readFileSync(cliArgs[at + 1]));
  if (npy.shape.length < 2) throw new Error('npy shape is shorter than 2');
  const got = Buffer.from(value.buffer, value.byteOffset, value.byteLength);
  const expect = framed(npy.shape[0], npy.shape[1], npy.payload);
  if (!got.equals(expect)) throw new Error('bytes differ');
}

function report(label, err) {
  const message = err instanceof Error ? err.message : String(err);
  const mark = label.lastIndexOf('::');
  const file = mark < 0 ? label : label.slice(0, mark);
  const name = mark < 0 ? '' : label.slice(mark + 2);
  const named = name && (message === 'bytes differ' || message === 'error differs');
  console.error(`${file}: ${named ? `${name} ${message}` : message}`);
}

let failed = 0;
for (const item of list) {
  try {
    if (typeof item.label !== 'string' || !Array.isArray(item.cliArgs) || typeof item.wasm !== 'function') {
      throw new Error('bad case');
    }
    const side = cliSide(spawnCli(item.cliArgs));
    let fromWasm;
    try {
      fromWasm = { ok: true, value: item.wasm(wasm) };
    } catch (err) {
      fromWasm = wasmFailure(err);
    }
    if (side.ok && fromWasm.ok) {
      bytesOf(item.cliArgs, side.stdout, fromWasm.value);
    } else if (!sameError(side, fromWasm)) {
      throw new Error('error differs');
    }
  } catch (err) {
    report(typeof item.label === 'string' ? item.label : '(case)', err);
    failed += 1;
  }
}

if (failed) process.exit(1);
console.log(`wasm matches native on ${inputs.length} files`);
