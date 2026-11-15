// Finish (raster): finish_array, finish_frame_canvas, finish_logo_key, finish_logo_fit parity between WASM and gxr-cli.
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const dir = mkdtempSync(join(tmpdir(), 'gxr-wasm-finish-'));
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
    if (spec.kind === 'array') {
      const idx = out.length;
      const outNpy = join(dir, `array-${idx}.npy`);
      const outRep = join(dir, `array-rep-${idx}.npy`);
      const editText = typeof spec.edit === 'string' ? spec.edit : JSON.stringify(spec.edit ?? {});
      const optsText = typeof spec.options === 'string' ? spec.options : JSON.stringify(spec.options ?? {});
      const editPath = join(dir, `edit-${idx}.json`);
      const optsPath = join(dir, `opts-${idx}.json`);
      writeFileSync(editPath, editText);
      writeFileSync(optsPath, optsText);
      const baseArgs = ['finish-array', '--in', spec.rgbNpy, '--edit', editPath, '--options', optsPath];
      if (spec.overrideDims) {
        baseArgs.push('--w', String(spec.w), '--h', String(spec.h));
      }
      out.push({
        label: `${file}::finish_array_rgb`,
        cliArgs: [...baseArgs, '--out', outNpy],
        wasm: (mod) => {
          const rgb = npyPayload(readFileSync(spec.rgbNpy));
          const res = mod.finish_array(rgb, spec.w, spec.h, editText, optsText);
          try {
            return framed(res.height, res.width, res.rgb());
          } finally {
            // res.rgb() consumes self in wasm-bindgen
          }
        },
      });
      out.push({
        label: `${file}::finish_array_report`,
        cliArgs: [...baseArgs, '--out', outRep],
        wasm: (mod) => {
          const rgb = npyPayload(readFileSync(spec.rgbNpy));
          const res = mod.finish_array(rgb, spec.w, spec.h, editText, optsText);
          const rep = res.report;
          res.free();
          return rep;
        },
      });
    } else if (spec.kind === 'frame_canvas') {
      const idx = out.length;
      const outNpy = join(dir, `canvas-${idx}.npy`);
      const outRep = join(dir, `canvas-rep-${idx}.npy`);
      const layoutText = typeof spec.layout === 'string' ? spec.layout : JSON.stringify(spec.layout);
      const layoutPath = join(dir, `layout-${idx}.json`);
      writeFileSync(layoutPath, layoutText);
      const baseArgs = ['finish-frame-canvas', '--in', spec.rgbNpy, '--layout', layoutPath];
      if (spec.overrideDims) {
        baseArgs.push('--w', String(spec.w), '--h', String(spec.h));
      }
      out.push({
        label: `${file}::finish_frame_canvas_rgb`,
        cliArgs: [...baseArgs, '--out', outNpy],
        wasm: (mod) => {
          const rgb = npyPayload(readFileSync(spec.rgbNpy));
          const res = mod.finish_frame_canvas(rgb, spec.w, spec.h, layoutText);
          return framed(res.height, res.width, res.rgb());
        },
      });
      out.push({
        label: `${file}::finish_frame_canvas_report`,
        cliArgs: [...baseArgs, '--out', outRep],
        wasm: (mod) => {
          const rgb = npyPayload(readFileSync(spec.rgbNpy));
          const res = mod.finish_frame_canvas(rgb, spec.w, spec.h, layoutText);
          const rep = res.report;
          res.free();
          return rep;
        },
      });
    } else if (spec.kind === 'logo_key') {
      const idx = out.length;
      const outNpy = join(dir, `logo-${idx}.npy`);
      const outRep = join(dir, `logo-rep-${idx}.npy`);
      const baseArgs = ['finish-logo-key', '--in', spec.rgbaNpy];
      if (spec.overrideDims) {
        baseArgs.push('--w', String(spec.w), '--h', String(spec.h));
      }
      out.push({
        label: `${file}::finish_logo_key_rgba`,
        cliArgs: [...baseArgs, '--out', outNpy],
        wasm: (mod) => {
          const rgba = npyPayload(readFileSync(spec.rgbaNpy));
          const res = mod.finish_logo_key(rgba, spec.w, spec.h);
          return framed(res.height, res.width, res.rgba());
        },
      });
      out.push({
        label: `${file}::finish_logo_key_report`,
        cliArgs: [...baseArgs, '--out', outRep],
        wasm: (mod) => {
          const rgba = npyPayload(readFileSync(spec.rgbaNpy));
          const res = mod.finish_logo_key(rgba, spec.w, spec.h);
          const rep = res.report;
          res.free();
          return rep;
        },
      });
    } else if (spec.kind === 'logo_fit') {
      const idx = out.length;
      const infoText = typeof spec.info === 'string' ? spec.info : JSON.stringify(spec.info ?? {});
      const reqText = typeof spec.request === 'string' ? spec.request : JSON.stringify(spec.request ?? {});
      const infoPath = join(dir, `info-${idx}.json`);
      const reqPath = join(dir, `req-${idx}.json`);
      writeFileSync(infoPath, infoText);
      writeFileSync(reqPath, reqText);
      out.push({
        label: `${file}::finish_logo_fit`,
        cliArgs: ['finish-logo-fit', '--info', infoPath, '--request', reqPath],
        wasm: (mod) => mod.finish_logo_fit(infoText, reqText),
      });
    } else {
      throw new Error(`unknown finish case kind ${spec.kind}`);
    }
  }
  return out;
}
