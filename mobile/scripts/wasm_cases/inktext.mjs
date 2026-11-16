// Ink-text: inktext_draw parity between WASM and gxr-cli.
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const dir = mkdtempSync(join(tmpdir(), 'gxr-wasm-inktext-'));
process.on('exit', () => {
  rmSync(dir, { recursive: true, force: true });
});

function npyPayload(buf) {
  if (buf.subarray(0, 6).toString('latin1') !== '\u0093NUMPY') throw new Error('not an npy file');
  const headerLen = buf[6] === 1 ? buf.readUInt16LE(8) : buf.readUInt32LE(8);
  const headerAt = buf[6] === 1 ? 10 : 12;
  return new Uint8Array(buf.subarray(headerAt + headerLen));
}

function framed(height, width, payload) {
  const header = Buffer.alloc(8);
  header.writeUInt32LE(height, 0);
  header.writeUInt32LE(width, 4);
  return new Uint8Array(Buffer.concat([header, Buffer.from(payload.buffer, payload.byteOffset, payload.byteLength)]));
}

function pack(fonts) {
  const index = [];
  const parts = [];
  for (const [name, path] of fonts || []) {
    const bytes = readFileSync(path);
    index.push([name, bytes.length]);
    parts.push(bytes);
  }
  return { index: JSON.stringify(index), bytes: new Uint8Array(Buffer.concat(parts)) };
}

function jsNum(v) {
  if (v === 'NaN') return Number.NaN;
  if (v === 'Infinity') return Number.POSITIVE_INFINITY;
  if (v === '-Infinity') return Number.NEGATIVE_INFINITY;
  return v;
}

export function cases(inputs) {
  const out = [];
  for (const file of inputs) {
    const spec = JSON.parse(readFileSync(file, 'utf8'));
    const idx = out.length;
    const fontArgs = (spec.fonts || []).flatMap(([name, path]) => ['--font', `${name}=${path}`]);
    if (spec.kind === 'mark') {
      out.push({
        label: `${file}::gxr_mark`,
        cliArgs: ['inktext-mark', ...fontArgs],
        wasm: (mod) => {
          const faces = pack(spec.fonts);
          return mod.gxr_mark(faces.index, faces.bytes);
        },
      });
      continue;
    }
    const outNpy = join(dir, `draw-${idx}.npy`);
    const outRep = join(dir, `draw-rep-${idx}.npy`);
    const opsText = typeof spec.ops === 'string' ? spec.ops : JSON.stringify(spec.ops ?? []);
    const opsPath = join(dir, `ops-${idx}.json`);
    writeFileSync(opsPath, opsText);
    const w = jsNum(spec.w);
    const h = jsNum(spec.h);
    const cliW = spec.cliW ?? w;
    const cliH = spec.cliH ?? h;
    const baseArgs = ['inktext-draw', '--in', spec.npy, '--mode', spec.mode, '--ops', opsPath, ...fontArgs];
    if (spec.overrideDims) {
      baseArgs.push('--w', String(cliW), '--h', String(cliH));
    }

    out.push({
      label: `${file}::inktext_draw_pixels`,
      cliArgs: [...baseArgs, '--out', outNpy],
      wasm: (mod) => {
        const pixels = npyPayload(readFileSync(spec.npy));
        const faces = pack(spec.fonts);
        const res = mod.inktext_draw(pixels, w, h, spec.mode, opsText, faces.index, faces.bytes);
        return framed(res.height, res.width, res.pixels());
      },
    });
    out.push({
      label: `${file}::inktext_draw_report`,
      cliArgs: [...baseArgs, '--out', outRep],
      wasm: (mod) => {
        const pixels = npyPayload(readFileSync(spec.npy));
        const faces = pack(spec.fonts);
        const res = mod.inktext_draw(pixels, w, h, spec.mode, opsText, faces.index, faces.bytes);
        const rep = res.report;
        res.free();
        return rep;
      },
    });
  }
  return out;
}
