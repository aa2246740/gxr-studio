// 色彩：烘焙表和套用。每个输入是一份 JSON，列出这一例要用的文件（测试现场生成）。
// bake: {"kind": "bake", "entry", "stock", "menu"?, "ctx", "ring", "params"?}
// apply: {"kind": "apply", "bake", "meta", "codes", "params"?, "out"}
import { readFileSync } from 'node:fs';

const bytes = (path) => new Uint8Array(readFileSync(path));

function framed(height, width, payload) {
  const header = Buffer.alloc(8);
  header.writeUInt32LE(height, 0);
  header.writeUInt32LE(width, 4);
  return new Uint8Array(Buffer.concat([header, Buffer.from(payload)]));
}

// float32 codes from a little-endian .npy, with its shape.
function npyFloat32(path) {
  const buf = readFileSync(path);
  const headerLen = buf.readUInt16LE(8);
  const header = buf.subarray(10, 10 + headerLen).toString('latin1');
  if (!header.includes("'descr': '<f4'")) throw new Error('codes must be <f4');
  const shape = header
    .match(/'shape': \(([^)]*)\)/)[1]
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
    .map(Number);
  const data = buf.subarray(10 + headerLen);
  const copy = new Uint8Array(data.byteLength);
  copy.set(data);
  return { shape, values: new Float32Array(copy.buffer) };
}

export function cases(inputs) {
  const out = [];
  for (const file of inputs) {
    const spec = JSON.parse(readFileSync(file, 'utf8'));
    if (spec.kind === 'bake') {
      const cliArgs = ['color-bake', '--entry', spec.entry, '--stock', spec.stock, '--ctx', spec.ctx, '--ring', spec.ring];
      if (spec.params) cliArgs.push('--params', spec.params);
      if (spec.menu) cliArgs.push('--menu', spec.menu);
      cliArgs.push('--out', `${file}.bin`, '--meta', `${file}.meta.json`);
      out.push({
        label: `${file}::color_bake`,
        cliArgs,
        wasm: (mod) => {
          const params = spec.params ? bytes(spec.params) : new TextEncoder().encode('{}');
          const menu = spec.menu ? bytes(spec.menu) : new Uint8Array(0);
          const bake = mod.color_bake(bytes(spec.entry), bytes(spec.stock), menu, bytes(spec.ctx), bytes(spec.ring), params);
          return bake.summary;
        },
      });
    } else if (spec.kind === 'apply') {
      const cliArgs = ['color-apply', '--bake', spec.bake, '--meta', spec.meta, '--codes', spec.codes];
      if (spec.params) cliArgs.push('--params', spec.params);
      cliArgs.push('--out', spec.out);
      out.push({
        label: `${file}::color_apply`,
        cliArgs,
        wasm: (mod) => {
          const codes = npyFloat32(spec.codes);
          const params = spec.params ? bytes(spec.params) : new TextEncoder().encode('{}');
          const rgb = mod.color_apply(bytes(spec.bake), bytes(spec.meta), codes.values, params);
          return framed(codes.shape[0], codes.shape[1], rgb);
        },
      });
    } else {
      throw new Error(`unknown color case kind ${spec.kind}`);
    }
  }
  return out;
}
