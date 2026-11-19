// Runs `camdata_camera_recipe` from the built WASM package for a batch of parity cases.
// Reads a JSON manifest path from argv[2] and writes JSON results to argv[3].
// Never logs tag strings or file paths to stdout.

import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const [pkgJs, pkgWasm, manifestPath, outPath] = process.argv.slice(2);
const wasm = await import(pathToFileURL(pkgJs).href);
await wasm.default({ module_or_path: readFileSync(pkgWasm) });

const cases = JSON.parse(readFileSync(manifestPath, 'utf8'));
const cache = new Map();
function loadBytes(path) {
  if (!path) return undefined;
  let cached = cache.get(path);
  if (!cached) {
    cached = new Uint8Array(readFileSync(path));
    cache.set(path, cached);
  }
  return cached.slice();
}

const results = [];
for (const item of cases) {
  try {
    const dat = loadBytes(item.dat);
    const lib = loadBytes(item.library);
    const raw = wasm.camdata_camera_recipe(item.tag, dat, lib);
    results.push({ ok: true, data: JSON.parse(raw) });
  } catch (err) {
    const text = typeof err === 'string' ? err : err instanceof Error ? err.message : String(err);
    let parsed = null;
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = { error: { kind: 'js_error', message: text } };
    }
    results.push({ ok: false, data: parsed });
  }
}

writeFileSync(outPath, JSON.stringify(results));
