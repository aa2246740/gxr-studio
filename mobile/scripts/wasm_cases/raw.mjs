// RAW：描述文本、半分辨率码值和白平衡条带。和拆分前的三组比较是同一件事。
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, join } from 'node:path';

const dir = mkdtempSync(join(tmpdir(), 'gxr-wasm-'));
process.on('exit', () => {
  rmSync(dir, { recursive: true, force: true });
});

function framed(height, width, payload) {
  const header = Buffer.alloc(8);
  header.writeUInt32LE(height, 0);
  header.writeUInt32LE(width, 4);
  return Buffer.concat([header, payload]);
}

function floatBytes(block) {
  const height = block.height;
  const width = block.width;
  const values = block.values();
  const raw = Buffer.from(values.buffer, values.byteOffset, values.byteLength);
  return new Uint8Array(framed(height, width, raw));
}

export function cases(inputs) {
  const out = [];
  for (const file of inputs) {
    const name = basename(file);
    const bytes = () => new Uint8Array(readFileSync(file));
    out.push({
      label: `${file}::dng_describe`,
      cliArgs: ['dng-describe', file],
      wasm: (mod) => {
        const text = mod.dng_describe_named(bytes(), name);
        const parsed = JSON.parse(text);
        if (parsed.error) throw new Error(text);
        return text;
      },
    });
    const halfOut = join(dir, `half-${out.length}.npy`);
    out.push({
      label: `${file}::half-codes`,
      cliArgs: ['dng-half-codes', file, '--out', halfOut],
      wasm: (mod) => floatBytes(mod.dng_half_codes_named(bytes(), name)),
    });
    const wbOut = join(dir, `wb-${out.length}.npy`);
    out.push({
      label: `${file}::wb-block`,
      cliArgs: ['dng-wb-block', file, '--rows', '0:1', '--out', wbOut],
      wasm: (mod) => floatBytes(mod.dng_wb_block_named(bytes(), name, 0, 1)),
    });
  }
  return out;
}
