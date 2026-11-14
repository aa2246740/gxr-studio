// Resample: resample_u8 (L/RGB/RGBA) and resample_f32 (mode F) parity between WASM and gxr-cli.
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const dir = mkdtempSync(join(tmpdir(), 'gxr-wasm-resample-'));
process.on('exit', () => {
  rmSync(dir, { recursive: true, force: true });
});

function readNpy(path) {
  const buf = readFileSync(path);
  if (buf.subarray(0, 6).toString('latin1') !== '\u0093NUMPY') {
    throw new Error(`not an npy file: ${path}`);
  }
  const headerLen = buf[6] === 1 ? buf.readUInt16LE(8) : buf.readUInt32LE(8);
  const at = buf[6] === 1 ? 10 : 12;
  const header = buf.subarray(at, at + headerLen).toString('latin1');
  const descr = header.match(/'descr': '([^']*)'/)[1];
  const shape = header
    .match(/'shape': \(([^)]*)\)/)[1]
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
    .map(Number);
  const payload = new Uint8Array(buf.subarray(at + headerLen));
  if (descr === '<f4') {
    const ab = payload.buffer.slice(payload.byteOffset, payload.byteOffset + payload.byteLength);
    return { descr, shape, values: new Float32Array(ab) };
  }
  return { descr, shape, values: payload };
}

function framed(height, width, payload) {
  const header = Buffer.alloc(8);
  header.writeUInt32LE(height, 0);
  header.writeUInt32LE(width, 4);
  return new Uint8Array(
    Buffer.concat([header, Buffer.from(payload.buffer, payload.byteOffset, payload.byteLength)]),
  );
}

export function cases(inputs) {
  const out = [];
  for (const file of inputs) {
    const spec = JSON.parse(readFileSync(file, 'utf8'));
    const outNpy = join(dir, `out-${out.length}.npy`);
    const inH = spec.inH;
    const inW = spec.inW;
    const outW = spec.outW;
    const outH = spec.outH;
    const filter = spec.filter;

    if (spec.kind === 'u8') {
      const mode = spec.mode;
      const channels = spec.channels;
      const cliArgs = [
        'resample-u8',
        '--in',
        spec.inNpy,
        '--mode',
        mode,
        '--filter',
        filter,
        '--width',
        String(outW),
        '--height',
        String(outH),
        '--out',
        outNpy,
      ];
      if (spec.overrideInShape) {
        cliArgs.push('--in-shape', String(inH), String(inW));
      }
      if (spec.overrideChannels !== undefined) {
        cliArgs.push('--channels', String(spec.overrideChannels));
      }
      out.push({
        label: `${file}::resample_u8`,
        cliArgs,
        wasm: (mod) => {
          const npy = readNpy(spec.inNpy);
          const ch = spec.overrideChannels !== undefined ? spec.overrideChannels : channels;
          const res = mod.resample_u8(npy.values, inW, inH, ch, mode, filter, outW, outH);
          return framed(outH, outW, res);
        },
      });
    } else if (spec.kind === 'f32') {
      const cliArgs = [
        'resample-f32',
        '--in',
        spec.inNpy,
        '--filter',
        filter,
        '--width',
        String(outW),
        '--height',
        String(outH),
        '--out',
        outNpy,
      ];
      if (spec.overrideInShape) {
        cliArgs.push('--in-shape', String(inH), String(inW));
      }
      out.push({
        label: `${file}::resample_f32`,
        cliArgs,
        wasm: (mod) => {
          const npy = readNpy(spec.inNpy);
          const res = mod.resample_f32(npy.values, inW, inH, filter, outW, outH);
          const bytes = new Uint8Array(res.buffer, res.byteOffset, res.byteLength);
          return framed(outH, outW, bytes);
        },
      });
    } else {
      throw new Error(`unknown spec kind: ${spec.kind}`);
    }
  }
  return out;
}
