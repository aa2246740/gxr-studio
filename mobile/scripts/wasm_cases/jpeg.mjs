// Owner: wave-1 unit jpeg.
// Inputs are JSON spec files: { "npy": "...", "out": "...", "quality": 95, "exif": "..." }.
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

export function cases(inputs) {
  const out = [];
  for (const specPath of inputs) {
    const spec = JSON.parse(readFileSync(specPath, 'utf8'));
    const exifJson = spec.exif ? readFileSync(spec.exif, 'utf8') : '';
    const cliArgs = ['jpeg-encode', '--in', spec.npy, '--quality', String(spec.quality), '--out', spec.out];
    if (spec.exif) {
      cliArgs.push('--exif', spec.exif);
    }
    out.push({
      label: `${specPath}::jpeg_encode`,
      cliArgs,
      wasm: (mod) => {
        const { height, width, payload } = readNpyRgb(spec.npy);
        const jpg = mod.jpeg_encode(payload, width, height, spec.quality, exifJson);
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
            quality: spec.quality,
            bytes: jpg.length,
            sha256,
          }) + '\n'
        );
      },
    });
  }
  return out;
}
