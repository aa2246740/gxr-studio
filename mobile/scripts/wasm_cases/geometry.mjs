// Geometry: final_box JSON and warp RGB + mask parity between WASM and gxr-cli.
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const dir = mkdtempSync(join(tmpdir(), 'gxr-wasm-geom-'));
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
    const w = spec.w;
    const h = spec.h;
    const geomJson = typeof spec.geometry === 'string' ? spec.geometry : JSON.stringify(spec.geometry ?? {});
    const cropJson = typeof spec.crop === 'string' ? spec.crop : JSON.stringify(spec.crop ?? null);

    if (spec.checkFinalBox !== false) {
      out.push({
        label: `${file}::geom_final_box`,
        cliArgs: ['geom-final-box', '--w', String(w), '--h', String(h), '--geometry', geomJson, '--crop', cropJson],
        wasm: (mod) => `${mod.geom_final_box(w, h, geomJson, cropJson)}\n`,
      });
    }

    if (spec.rgbNpy) {
      const idx = out.length;
      const outNpy = join(dir, `warp-${idx}.npy`);
      const maskNpy = join(dir, `mask-${idx}.npy`);
      const maskOnlyNpy = join(dir, `mask-only-${idx}.npy`);
      out.push({
        label: `${file}::geom_warp_rgb`,
        cliArgs: [
          'geom-warp',
          '--in',
          spec.rgbNpy,
          '--geometry',
          geomJson,
          '--out',
          outNpy,
          '--mask',
          maskNpy,
          ...(spec.overrideDims ? ['--w', String(w), '--h', String(h)] : []),
        ],
        wasm: (mod) => {
          const rgbBytes = npyPayload(readFileSync(spec.rgbNpy));
          const res = mod.geom_warp(rgbBytes, w, h, geomJson);
          try {
            return framed(res.height, res.width, res.rgb());
          } finally {
            res.free();
          }
        },
      });
      if (!spec.expectError) {
        out.push({
          label: `${file}::geom_warp_mask`,
          cliArgs: ['geom-warp-valid', '--w', String(w), '--h', String(h), '--geometry', geomJson, '--out', maskOnlyNpy],
          wasm: (mod) => {
            const rgbBytes = npyPayload(readFileSync(spec.rgbNpy));
            const res = mod.geom_warp(rgbBytes, w, h, geomJson);
            try {
              return framed(res.height, res.width, res.mask());
            } finally {
              res.free();
            }
          },
        });
      }
    }
  }
  return out;
}
