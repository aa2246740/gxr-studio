// Owner: wave-2 export (export-meta: exif / photo / preview; export-pipeline: export_plan / export_prepare / export_complete /
// export_output_sizes against gxr-cli export-run-*).
// 输入是清单 JSON 的路径，每个清单是 [{label, kind, ...}]；同一份输入，WASM 的 JSON 和 gxr-cli 的 stdout
// 逐字节相同，画布字节和 --out 的 npy 相同，错误的 kind 和 message 也相同。
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

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

function npy(path) {
  const buf = readFileSync(path);
  const headerLen = buf[6] === 1 ? buf.readUInt16LE(8) : buf.readUInt32LE(8);
  const at = buf[6] === 1 ? 10 : 12;
  const header = buf.subarray(at, at + headerLen).toString('latin1');
  const shape = header.match(/'shape': \(([^)]*)\)/)[1].split(',').map((p) => p.trim()).filter(Boolean).map(Number);
  const data = buf.subarray(at + headerLen);
  return { h: shape[0], w: shape[1], data: new Uint8Array(data.buffer, data.byteOffset, data.byteLength) };
}

// Non-finite numbers come as strings ("NaN", "Infinity"); everything else as JSON numbers.
const num = (v) => (typeof v === 'string' ? Number(v) : v);
const text = (path) => (path ? readFileSync(path, 'utf8') : '');
const fontArgs = (fonts) => (fonts || []).flatMap(([name, path]) => ['--font', `${name}=${path}`]);
const sizeArgs = (c) => [...(c.w !== undefined ? ['--w', String(num(c.w))] : []), ...(c.h !== undefined ? ['--h', String(num(c.h))] : [])];

function framed(h, w, payload) {
  const header = Buffer.alloc(8);
  header.writeUInt32LE(h, 0);
  header.writeUInt32LE(w, 4);
  return new Uint8Array(Buffer.concat([header, Buffer.from(payload)]));
}

const bytes = (path) => (path ? new Uint8Array(readFileSync(path)) : new Uint8Array(0));

// A calibration folder as gxr-cli reads it: files sorted by name, packed as [[name, length], ...].
function calibPack(dir) {
  if (!dir) return { index: '', bytes: new Uint8Array(0) };
  const names = readdirSync(dir).filter((n) => statSync(join(dir, n)).isFile()).sort();
  return pack(names.map((n) => [n, join(dir, n)]));
}

function prepareCall(mod, c) {
  const jpg = c.jpg ? npy(c.jpg) : { w: 0, h: 0, data: new Uint8Array(0) };
  const prev = c.preview ? npy(c.preview) : { w: 0, h: 0, data: new Uint8Array(0) };
  const faces = pack(c.fonts);
  const calib = calibPack(c.calib);
  return mod.export_prepare(
    text(c.shot), text(c.side), text(c.defaults), text(c.layout), c.base || 'auto',
    num(c.quality), c.long_edge === null || c.long_edge === undefined ? undefined : num(c.long_edge), Boolean(c.hdr),
    faces.index, faces.bytes, bytes(c.dat), bytes(c.entry), calib.index, calib.bytes, c.binding || '',
    bytes(c.dng), bytes(c.jpg_bytes), bytes(c.cube), text(c.space),
    jpg.data, c.w !== undefined ? num(c.w) : jpg.w, c.h !== undefined ? num(c.h) : jpg.h,
    prev.data, prev.w, prev.h,
    c.mtime_ms === null || c.mtime_ms === undefined ? undefined : num(c.mtime_ms),
    c.tz_offset_min === undefined ? 0 : num(c.tz_offset_min),
  );
}

function prepareArgs(c) {
  const args = ['export-run-prepare', '--shot', c.shot, '--quality', String(num(c.quality)), '--base', c.base || 'auto'];
  for (const [flag, key] of [
    ['--side', 'side'], ['--defaults', 'defaults'], ['--layout', 'layout'], ['--dat', 'dat'], ['--entry', 'entry'],
    ['--calib', 'calib'], ['--dng', 'dng'], ['--jpg', 'jpg'], ['--jpg-bytes', 'jpg_bytes'], ['--preview', 'preview'],
    ['--cube', 'cube'], ['--space', 'space'],
  ]) {
    if (c[key]) args.push(flag, c[key]);
  }
  if (c.binding) args.push('--binding', c.binding);
  if (c.mtime_ms !== null && c.mtime_ms !== undefined) args.push('--mtime-ms', String(num(c.mtime_ms)));
  if (c.tz_offset_min !== undefined) args.push('--tz-offset-min', String(num(c.tz_offset_min)));
  if (c.long_edge !== null && c.long_edge !== undefined) args.push('--long-edge', String(num(c.long_edge)));
  if (c.hdr) args.push('--hdr');
  args.push(...sizeArgs(c), ...fontArgs(c.fonts));
  return args;
}

function pipelineCases(inputs) {
  const out = [];
  for (const manifest of inputs) {
    for (const c of JSON.parse(readFileSync(manifest, 'utf8'))) {
      const label = `${manifest}::${c.label}`;
      if (c.kind === 'plan') {
        const args = ['export-run-plan', '--shot', c.shot, '--base', c.base || 'auto'];
        if (c.side) args.push('--side', c.side);
        if (c.defaults) args.push('--defaults', c.defaults);
        out.push({ label, cliArgs: args, wasm: (mod) => mod.export_plan(text(c.shot), text(c.side), text(c.defaults), c.base || 'auto') });
      } else if (c.kind === 'prepare') {
        out.push({
          label: `${label}-json`,
          cliArgs: [...prepareArgs(c), '--out', `${c.out}.json.npy`],
          wasm: (mod) => prepareCall(mod, c).json(),
        });
        out.push({
          label: `${label}-canvas`,
          cliArgs: [...prepareArgs(c), '--out', `${c.out}.canvas.npy`],
          wasm: (mod) => {
            const p = prepareCall(mod, c);
            return framed(p.height, p.width, p.canvas());
          },
        });
      } else if (c.kind === 'complete') {
        const args = ['export-run-complete', '--canvas', c.canvas, '--state', c.state, ...sizeArgs(c), '--out', c.out];
        if (c.src) args.push('--src', c.src);
        out.push({
          label,
          cliArgs: args,
          wasm: (mod) => {
            const pic = npy(c.canvas);
            return mod
              .export_complete(pic.data, c.w !== undefined ? num(c.w) : pic.w, c.h !== undefined ? num(c.h) : pic.h, text(c.state), bytes(c.src))
              .json();
          },
        });
      } else if (c.kind === 'sizes') {
        const args = ['export-run-sizes', '--width', String(num(c.width)), '--height', String(num(c.height))];
        for (const [flag, key] of [['--side', 'side'], ['--defaults', 'defaults'], ['--layout', 'layout']]) {
          if (c[key]) args.push(flag, c[key]);
        }
        if (c.rect !== null && c.rect !== undefined) args.push('--rect', c.rect);
        if (c.preset !== null && c.preset !== undefined) args.push('--preset', c.preset);
        args.push(...fontArgs(c.fonts));
        out.push({
          label,
          cliArgs: args,
          wasm: (mod) => {
            const faces = pack(c.fonts);
            const preset = c.preset === null || c.preset === undefined ? undefined : c.preset;
            return mod.export_output_sizes(
              num(c.width), num(c.height), text(c.side), text(c.defaults), text(c.layout), c.rect || '', preset, faces.index, faces.bytes,
            );
          },
        });
      } else {
        throw new Error(`unknown case kind ${c.kind}`);
      }
    }
  }
  return out;
}

// ---- export-meta
// Inputs are JSON manifest files (an array of case specs) or direct photo paths:
//   - { label, kind: "exif", src }
//   - { label, kind: "photo", jpg, dng, name, mtime_ms, tz_offset_min }
//   - { label, kind: "preview", dng }  (DNGs whose preview result is non-null or CoreError)

function readBytes(path) {
  return new Uint8Array(readFileSync(path));
}

function parseNumArg(val) {
  if (val === undefined || val === null) return undefined;
  if (val === 'NaN') return Number.NaN;
  if (val === 'Infinity') return Number.POSITIVE_INFINITY;
  if (val === '-Infinity') return Number.NEGATIVE_INFINITY;
  return val;
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
    const mtimeMs = parseNumArg(c.mtime_ms);
    const tzOffsetMin = parseNumArg(c.tz_offset_min);
    if (c.jpg) cliArgs.push('--jpg', c.jpg);
    if (c.dng) cliArgs.push('--dng', c.dng);
    if (c.name !== undefined && c.name !== null) cliArgs.push('--name', String(c.name));
    if (mtimeMs !== undefined) {
      cliArgs.push('--mtime-ms', String(mtimeMs));
    }
    if (tzOffsetMin !== undefined) {
      cliArgs.push('--tz-offset-min', String(tzOffsetMin));
    }
    return {
      label: `${manifestPath}::${c.label}`,
      cliArgs,
      wasm: (mod) =>
        mod.export_meta_photo(
          c.jpg ? readBytes(c.jpg) : undefined,
          c.dng ? readBytes(c.dng) : undefined,
          c.name ?? '',
          mtimeMs,
          tzOffsetMin,
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

function metaCases(inputs) {
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

const PIPELINE_KINDS = new Set(['plan', 'prepare', 'complete', 'sizes']);

// A manifest whose cases are all pipeline kinds goes to the pipeline cases; anything else
// (meta manifests, photo paths) to export-meta's.
export function cases(inputs) {
  const pipeline = [];
  const meta = [];
  for (const path of inputs) {
    let list = null;
    if (path.endsWith('.json')) {
      const parsed = JSON.parse(readFileSync(path, 'utf8'));
      list = Array.isArray(parsed) ? parsed : [parsed];
    }
    if (list && list.length && list.every((c) => PIPELINE_KINDS.has(c.kind))) pipeline.push(path);
    else meta.push(path);
  }
  return [...pipelineCases(pipeline), ...metaCases(meta)];
}
