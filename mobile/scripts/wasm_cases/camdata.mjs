// Camdata: camdata_parse, camdata_entries, camdata_menu (single-file, r5 bundles, and damaged inputs).
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';

const dir = mkdtempSync(join(tmpdir(), 'gxr-wasm-camdata-'));
process.on('exit', () => {
  rmSync(dir, { recursive: true, force: true });
});

function framed(height, width, payload) {
  const header = Buffer.alloc(8);
  header.writeUInt32LE(height, 0);
  header.writeUInt32LE(width, 4);
  return Buffer.concat([header, payload]);
}

function readBytes(path) {
  return new Uint8Array(readFileSync(path));
}

export function cases(inputs) {
  const out = [];
  // Default menu without a DAT
  out.push({
    label: 'final.json::camdata_menu_default',
    cliArgs: ['camdata-menu'],
    wasm: (mod) => mod.camdata_menu(undefined, undefined),
  });

  for (const file of inputs) {
    if (file.endsWith('.kv')) {
      const kvBytes = () => readBytes(file);
      out.push({
        label: `${file}::camdata_knob_table`,
        cliArgs: ['camdata-knob-table', '--kv', file],
        wasm: (mod) => mod.camdata_knob_table(kvBytes(), false),
      });
      continue;
    }
    if (file.endsWith('.json')) {
      const jsonBytes = () => readBytes(file);
      out.push({
        label: `${file}::camdata_parse_json`,
        cliArgs: ['camdata-parse-json', '--json', file],
        wasm: (mod) => mod.camdata_parse_json(jsonBytes()),
      });
      continue;
    }
    const datBytes = () => readBytes(file);
    const fxPath = join(dirname(file), 'GXRFX.BIN');
    const hasFx = existsSync(fxPath);

    out.push({
      label: `${file}::camdata_parse`,
      cliArgs: ['camdata-parse', '--dat', file],
      wasm: (mod) => mod.camdata_parse(datBytes(), undefined),
    });

    const entriesOut = join(dir, `entries-${out.length}.npy`);
    out.push({
      label: `${file}::camdata_entries`,
      cliArgs: ['camdata-entries', '--dat', file, '--out', entriesOut],
      wasm: (mod) => {
        const raw = mod.camdata_entries(datBytes(), undefined);
        return new Uint8Array(framed(24, 552, Buffer.from(raw.buffer, raw.byteOffset, raw.byteLength)));
      },
    });

    out.push({
      label: `${file}::camdata_menu`,
      cliArgs: ['camdata-menu', '--dat', file],
      wasm: (mod) => mod.camdata_menu(datBytes(), undefined),
    });

    if (hasFx) {
      const fxBytes = () => readBytes(fxPath);
      out.push({
        label: `${file}::camdata_parse_r5`,
        cliArgs: ['camdata-parse', '--dat', file, '--library', fxPath],
        wasm: (mod) => mod.camdata_parse(datBytes(), fxBytes()),
      });

      const entriesR5Out = join(dir, `entries-r5-${out.length}.npy`);
      out.push({
        label: `${file}::camdata_entries_r5`,
        cliArgs: ['camdata-entries', '--dat', file, '--library', fxPath, '--out', entriesR5Out],
        wasm: (mod) => {
          const raw = mod.camdata_entries(datBytes(), fxBytes());
          return new Uint8Array(framed(24, 552, Buffer.from(raw.buffer, raw.byteOffset, raw.byteLength)));
        },
      });

      out.push({
        label: `${file}::camdata_menu_r5`,
        cliArgs: ['camdata-menu', '--dat', file, '--library', fxPath],
        wasm: (mod) => mod.camdata_menu(datBytes(), fxBytes()),
      });
    }
  }
  return out;
}
