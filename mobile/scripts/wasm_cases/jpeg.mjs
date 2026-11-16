// Owner: wave-1 unit jpeg.
// Inputs are JSON spec files:
//   { "npy": "...", "out": "...", "quality": 95, "exif": "...", "optimize": true }
// ("optimize" is optional: true adds `--optimize`, Pillow optimize=True as `_finish` writes.)
// or raw byte buffer specs (including empty huge-dimension error tests):
//   { "raw": "...", "width": 65535, "height": 65535, "out": "...", "quality": 85, "exif": "..." }
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';

function readNpyRgb(path) {
  const buf = readFileSync(path);
  if (buf.subarray(0, 6).toString('latin1') !== '\u0093NUMPY') {
    throw new Error(`not an npy file: ${path}`);
  }
  const major = buf[6];
  const hdrLen = major === 1 ? buf.readUInt16LE(8) : buf.readUInt32LE(8);
  const hdrAt = major === 1 ? 10 : 12;
  const header = buf.subarray(hdrAt, hdrAt + hdrLen).toString('latin1');
  const dims = header
    .match(/'shape': \(([^)]*)\)/)[1]
    .split(',')
    .map((p) => p.trim())
    .filter(Boolean)
    .map(Number);
  return { height: dims[0], width: dims[1], payload: new Uint8Array(buf.subarray(hdrAt + hdrLen)) };
}

function jsNum(v) {
  if (v === 'NaN') return Number.NaN;
  if (v === 'Infinity') return Number.POSITIVE_INFINITY;
  if (v === '-Infinity') return Number.NEGATIVE_INFINITY;
  return v;
}

export function cases(inputs) {
  const out = [];
  for (const specPath of inputs) {
    const spec = JSON.parse(readFileSync(specPath, 'utf8'));
    const exifJson = spec.exif ? readFileSync(spec.exif, 'utf8') : '';
    const cliWidth = spec.cliWidth ?? spec.width;
    const cliHeight = spec.cliHeight ?? spec.height;
    const cliQuality = spec.cliQuality ?? spec.quality;
    const cliArgs = spec.raw
      ? [
          'jpeg-encode',
          '--raw',
          spec.raw,
          '--width',
          String(cliWidth),
          '--height',
          String(cliHeight),
          '--quality',
          String(cliQuality),
          '--out',
          spec.out,
        ]
      : ['jpeg-encode', '--in', spec.npy, '--quality', String(cliQuality), '--out', spec.out];
    if (spec.exif) {
      cliArgs.push('--exif', spec.exif);
    }
    const optimize = spec.optimize === true;
    if (optimize) {
      cliArgs.push('--optimize');
    }
    out.push({
      label: `${specPath}::jpeg_encode`,
      cliArgs,
      wasm: (mod) => {
        const { height, width, payload } = spec.raw
          ? { height: jsNum(spec.height), width: jsNum(spec.width), payload: new Uint8Array(readFileSync(spec.raw)) }
          : readNpyRgb(spec.npy);
        const quality = jsNum(spec.quality);
        const jpg = mod.jpeg_encode(payload, width, height, quality, exifJson, optimize);
        const cliJpg = readFileSync(spec.out);
        if (!Buffer.from(jpg.buffer, jpg.byteOffset, jpg.byteLength).equals(cliJpg)) {
          return 'JPEG_BYTES_DIFFER\n';
        }
        const sha256 = createHash('sha256').update(jpg).digest('hex');
        return (
          JSON.stringify({
            out: spec.out,
            width,
            height,
            quality,
            bytes: jpg.length,
            sha256,
          }) + '\n'
        );
      },
    });
  }
  return out;
}
