// LUT：解析、空间识别、GPU 表（含 256³ 显示表和 Log 3D 表）、uint8 套用。
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, join } from 'node:path';

const dir = mkdtempSync(join(tmpdir(), 'gxr-wasm-lut-'));
process.on('exit', () => {
  rmSync(dir, { recursive: true, force: true });
});

function framed(height, width, payload) {
  const header = Buffer.alloc(8);
  header.writeUInt32LE(height, 0);
  header.writeUInt32LE(width, 4);
  return Buffer.concat([header, payload]);
}

function npyPayload(buf) {
  if (buf.subarray(0, 6).toString('latin1') !== '\u0093NUMPY') throw new Error('not an npy file');
  const headerLen = buf[6] === 1 ? buf.readUInt16LE(8) : buf.readUInt32LE(8);
  const headerAt = buf[6] === 1 ? 10 : 12;
  return buf.subarray(headerAt + headerLen);
}

export function cases(inputs) {
  const out = [];
  for (const file of inputs) {
    if (file.endsWith('.json')) {
      const spec = JSON.parse(readFileSync(file, 'utf8'));
      const cubeBytes = () => new Uint8Array(readFileSync(spec.cube));
      if (spec.kind === 'parse') {
        out.push({
          label: `${file}::lut_parse`,
          cliArgs: ['lut-parse', '--cube', spec.cube],
          wasm: (mod) => mod.lut_parse(cubeBytes()),
        });
      } else if (spec.kind === 'identify') {
        out.push({
          label: `${file}::lut_identify`,
          cliArgs: ['lut-identify', '--cube', spec.cube, '--name', spec.name],
          wasm: (mod) => mod.lut_identify(cubeBytes(), spec.name),
        });
      } else if (spec.kind === 'table') {
        const spaceJson = spec.space_file !== undefined ? readFileSync(spec.space_file, 'utf8') : JSON.stringify(spec.space);
        const spaceCliArg = spec.space_file !== undefined ? spec.space_file : spaceJson;
        const outBin = join(dir, `tbl-${out.length}.bin`);
        const outMeta = join(dir, `tbl-${out.length}.json`);
        out.push({
          label: `${file}::lut_table`,
          cliArgs: ['lut-table', '--cube', spec.cube, '--space', spaceCliArg, '--out', outBin, '--meta', outMeta],
          wasm: (mod) => mod.lut_table(cubeBytes(), spaceJson).summary_json(),
        });
      } else if (spec.kind === 'apply_u8') {
        const spaceJson = spec.space_file !== undefined ? readFileSync(spec.space_file, 'utf8') : JSON.stringify(spec.space);
        const spaceCliArg = spec.space_file !== undefined ? spec.space_file : spaceJson;
        const outNpy = join(dir, `apply-${out.length}.npy`);
        const cliArgs = ['lut-apply', '--cube', spec.cube, '--space', spaceCliArg, '--rgb', spec.rgb_npy, '--out', outNpy, '--u8'];
        if (spec.xyz_npy) {
          cliArgs.push('--xyz', spec.xyz_npy);
        }
        const rgbRaw = new Uint8Array(npyPayload(readFileSync(spec.rgb_npy)));
        let xyzArr = undefined;
        if (spec.xyz_npy) {
          const xyzBuf = npyPayload(readFileSync(spec.xyz_npy));
          const aligned = new ArrayBuffer(xyzBuf.byteLength);
          new Uint8Array(aligned).set(xyzBuf);
          xyzArr = new Float64Array(aligned);
        }
        out.push({
          label: `${file}::lut_apply_u8`,
          cliArgs,
          wasm: (mod) => {
            const img = mod.lut_apply_u8(cubeBytes(), spaceJson, rgbRaw, spec.height, spec.width, xyzArr);
            const height = img.height;
            const width = img.width;
            const raw = Buffer.from(img.values());
            return new Uint8Array(framed(height, width, raw));
          },
        });
      }
      continue;
    }

    const name = basename(file);
    const bytes = () => new Uint8Array(readFileSync(file));
    out.push({
      label: `${file}::lut_parse`,
      cliArgs: ['lut-parse', '--cube', file],
      wasm: (mod) => mod.lut_parse(bytes()),
    });
    out.push({
      label: `${file}::lut_identify`,
      cliArgs: ['lut-identify', '--cube', file, '--name', name],
      wasm: (mod) => mod.lut_identify(bytes(), name),
    });
  }
  return out;
}
