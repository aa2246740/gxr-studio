// M4 ink: ink_ops and finish_logo_prepare parity between WASM and gxr-cli.
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const dir = mkdtempSync(join(tmpdir(), 'gxr-wasm-ink-'));
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

export function cases(inputs) {
  const out = [];
  for (const file of inputs) {
    const spec = JSON.parse(readFileSync(file, 'utf8'));
    if (spec.kind === 'ink_ops') {
      const idx = out.length;
      const outNpy = join(dir, `ops-${idx}.npy`);
      const opsText = typeof spec.ops === 'string' ? spec.ops : JSON.stringify(spec.ops);
      const opsPath = join(dir, `ops-${idx}.json`);
      writeFileSync(opsPath, opsText);

      const cliArgs = ['ink-ops', '--canvas', spec.canvasNpy, '--mode', spec.mode, '--ops', opsPath];
      const logos = Array.isArray(spec.logos) ? spec.logos : [];
      const indexEntries = [];
      const chunks = [];
      let offset = 0;
      for (const logo of logos) {
        cliArgs.push('--logo', `${logo.id}=${logo.rgbaNpy}`);
        const bytes = npyPayload(readFileSync(logo.rgbaNpy));
        indexEntries.push({ id: logo.id, w: logo.w, h: logo.h, offset, len: bytes.byteLength });
        chunks.push(bytes);
        offset += bytes.byteLength;
      }
      const logoIndexText = typeof spec.logoIndexText === 'string' ? spec.logoIndexText : JSON.stringify(indexEntries);
      const logoBlob = chunks.length === 0 ? new Uint8Array(0) : new Uint8Array(Buffer.concat(chunks));

      const w = Number(spec.w);
      const h = Number(spec.h);
      if (spec.overrideDims) {
        cliArgs.push('--w', String(spec.w), '--h', String(spec.h));
      }

      out.push({
        label: `${file}::ink_ops_bytes`,
        cliArgs: [...cliArgs, '--out', outNpy],
        wasm: (mod) => {
          const canvas = npyPayload(readFileSync(spec.canvasNpy));
          const res = mod.ink_ops(canvas, w, h, spec.mode, opsText, logoIndexText, logoBlob);
          if (spec.expectMode && res.mode !== spec.expectMode) {
            const got = res.mode;
            res.free();
            throw new Error(`mode mismatch: expected ${spec.expectMode}, got ${got}`);
          }
          return framed(res.height, res.width, res.data());
        },
      });
    } else if (spec.kind === 'logo_prepare') {
      const idx = out.length;
      const outNpy = join(dir, `logo-prep-${idx}.npy`);
      const outRep = join(dir, `logo-prep-rep-${idx}.npy`);
      const keyText = typeof spec.key === 'string' ? spec.key : JSON.stringify(spec.key ?? {});
      const keyPath = join(dir, `key-${idx}.json`);
      writeFileSync(keyPath, keyText);

      const cliArgs = ['ink-logo-prepare', '--in', spec.rgbaNpy, '--key', keyPath];
      const w = Number(spec.w);
      const h = Number(spec.h);
      if (spec.overrideDims) {
        cliArgs.push('--w', String(spec.w), '--h', String(spec.h));
      }

      out.push({
        label: `${file}::finish_logo_prepare_rgba`,
        cliArgs: [...cliArgs, '--out', outNpy],
        wasm: (mod) => {
          const rgba = npyPayload(readFileSync(spec.rgbaNpy));
          const res = mod.finish_logo_prepare(rgba, w, h, keyText);
          return framed(res.height, res.width, res.rgba());
        },
      });
      out.push({
        label: `${file}::finish_logo_prepare_report`,
        cliArgs: [...cliArgs, '--out', outRep],
        wasm: (mod) => {
          const rgba = npyPayload(readFileSync(spec.rgbaNpy));
          const res = mod.finish_logo_prepare(rgba, w, h, keyText);
          const rep = res.report;
          res.free();
          return rep;
        },
      });
    } else {
      throw new Error(`unknown ink case kind ${spec.kind}`);
    }
  }
  return out;
}
