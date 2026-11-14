// Owner: wave-2 unit export-meta.
// Inputs are JSON manifest files (an array of case specs) or direct photo paths:
//   - { label, kind: "exif", src }
//   - { label, kind: "photo", jpg, dng, name, mtime_ms, tz_offset_min }
//   - { label, kind: "preview", dng }  (DNGs whose preview result is non-null or CoreError)
import { readFileSync } from 'node:fs';

function readBytes(path) {
  return new Uint8Array(readFileSync(path));
}

function toCase(manifestPath, c) {
  if (c.kind === 'exif') {
    return {
      label: `${manifestPath}::${c.label}`,
      cliArgs: ['export-meta-exif', '--src', c.src],
      wasm: (mod) => mod.export_meta_exif(readBytes(c.src)),
    };
  }
  if (c.kind === 'photo') {
    const cliArgs = ['export-meta-photo'];
    if (c.jpg) cliArgs.push('--jpg', c.jpg);
    if (c.dng) cliArgs.push('--dng', c.dng);
    if (c.name !== undefined && c.name !== null) cliArgs.push('--name', String(c.name));
    if (c.mtime_ms !== undefined && c.mtime_ms !== null) {
      cliArgs.push('--mtime-ms', String(c.mtime_ms));
    }
    if (c.tz_offset_min !== undefined && c.tz_offset_min !== null) {
      cliArgs.push('--tz-offset-min', String(c.tz_offset_min));
    }
    return {
      label: `${manifestPath}::${c.label}`,
      cliArgs,
      wasm: (mod) =>
        mod.export_meta_photo(
          c.jpg ? readBytes(c.jpg) : undefined,
          c.dng ? readBytes(c.dng) : undefined,
          c.name ?? '',
          c.mtime_ms ?? undefined,
          c.tz_offset_min ?? undefined,
        ),
    };
  }
  if (c.kind === 'preview') {
    return {
      label: `${manifestPath}::${c.label}`,
      cliArgs: ['export-meta-preview', '--dng', c.dng],
      wasm: (mod) => mod.export_meta_preview(readBytes(c.dng)),
    };
  }
  throw new Error(`unknown export wasm case kind: ${c.kind}`);
}

export function cases(inputs) {
  const out = [];
  for (const path of inputs) {
    if (path.endsWith('.json')) {
      const parsed = JSON.parse(readFileSync(path, 'utf8'));
      const list = Array.isArray(parsed) ? parsed : [parsed];
      for (const c of list) {
        out.push(toCase(path, c));
      }
    } else {
      out.push({
        label: `${path}::export_meta_exif`,
        cliArgs: ['export-meta-exif', '--src', path],
        wasm: (mod) => mod.export_meta_exif(readBytes(path)),
      });
    }
  }
  return out;
}
