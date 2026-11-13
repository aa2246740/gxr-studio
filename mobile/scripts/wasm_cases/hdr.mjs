// HDR：对齐、增益图（uint8 码值和元数据 JSON）、容器字节。
// inputs 是测试写好的目录，里面按需放 sdr.npy、raw.npy（对齐用）、reg.npy + clip.npy（增益用）、
// params.json，以及 primary.jpg、gain.jpg、meta.json（容器用）。缺哪个文件就跳过那一项。
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const dir = mkdtempSync(join(tmpdir(), 'gxr-wasm-hdr-'));
process.on('exit', () => {
  rmSync(dir, { recursive: true, force: true });
});

function framed(height, width, payload) {
  const header = Buffer.alloc(8);
  header.writeUInt32LE(height, 0);
  header.writeUInt32LE(width, 4);
  return new Uint8Array(Buffer.concat([header, Buffer.from(payload.buffer, payload.byteOffset, payload.byteLength)]));
}

function readNpy(path) {
  const buf = readFileSync(path);
  const headerLen = buf[6] === 1 ? buf.readUInt16LE(8) : buf.readUInt32LE(8);
  const at = buf[6] === 1 ? 10 : 12;
  const header = buf.subarray(at, at + headerLen).toString('latin1');
  const descr = header.match(/'descr': '([^']*)'/)[1];
  const shape = header
    .match(/'shape': \(([^)]*)\)/)[1]
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
    .map(Number);
  const payload = new Uint8Array(buf.subarray(at + headerLen));
  if (descr === '<f4') return { shape, values: new Float32Array(payload.buffer.slice(payload.byteOffset, payload.byteOffset + payload.byteLength)) };
  return { shape, values: payload };
}

export function cases(inputs) {
  const out = [];
  for (const folder of inputs) {
    const has = (name) => existsSync(join(folder, name));
    const path = (name) => join(folder, name);
    if (has('sdr.npy') && has('raw.npy')) {
      out.push({
        label: `${folder}::hdr_align`,
        cliArgs: ['hdr-align', '--sdr', path('sdr.npy'), '--raw', path('raw.npy')],
        wasm: (mod) => {
          const s = readNpy(path('sdr.npy'));
          const r = readNpy(path('raw.npy'));
          return mod.hdr_align(s.values, s.shape[0], s.shape[1], r.values, r.shape[0], r.shape[1]);
        },
      });
    }
    if (has('sdr.npy') && has('raw.npy')) {
      // Huge caller dimensions: both sides must refuse with the same core error, not panic or wrap.
      for (const [h, w] of [
        [4294967295, 4294967295],
        [2147483648, 1],
      ]) {
        const dims = ['--sdr-shape', String(h), String(w), '--raw-shape', String(h), String(w)];
        out.push({
          label: `${folder}::hdr_align_${h}x${w}`,
          cliArgs: ['hdr-align', '--sdr', path('sdr.npy'), '--raw', path('raw.npy'), ...dims],
          wasm: (mod) => {
            const s = readNpy(path('sdr.npy'));
            return mod.hdr_align(s.values, h, w, s.values, h, w);
          },
        });
        out.push({
          label: `${folder}::hdr_gain_${h}x${w}`,
          cliArgs: ['hdr-gain', '--sdr', path('sdr.npy'), '--raw', path('raw.npy'), '--clip', path('sdr.npy'), ...dims, '--out', join(dir, `huge-${out.length}.npy`)],
          wasm: (mod) => {
            const s = readNpy(path('sdr.npy'));
            return mod.hdr_gain(s.values, s.values, new Uint8Array(0), h, w, '').report;
          },
        });
      }
    }
    if (has('sdr.npy') && has('reg.npy') && has('clip.npy')) {
      const codesOut = join(dir, `codes-${out.length}.npy`);
      const args = ['hdr-gain', '--sdr', path('sdr.npy'), '--raw', path('reg.npy'), '--clip', path('clip.npy')];
      const params = has('params.json') ? readFileSync(path('params.json'), 'utf8') : '';
      if (params) args.push('--params', path('params.json'));
      const run = (mod) => {
        const s = readNpy(path('sdr.npy'));
        const r = readNpy(path('reg.npy'));
        const c = readNpy(path('clip.npy'));
        return mod.hdr_gain(s.values, r.values, c.values, s.shape[0], s.shape[1], params);
      };
      out.push({
        label: `${folder}::hdr_gain_codes`,
        cliArgs: [...args, '--out', codesOut],
        wasm: (mod) => {
          const g = run(mod);
          const h = g.height;
          const w = g.width;
          return framed(h, w, g.codes());
        },
      });
      out.push({
        label: `${folder}::hdr_gain_report`,
        cliArgs: [...args, '--out', join(dir, `codes-r-${out.length}.npy`)],
        wasm: (mod) => run(mod).report,
      });
    }
    if (has('primary.jpg') && has('gain.jpg') && has('meta.json')) {
      out.push({
        label: `${folder}::hdr_assemble`,
        cliArgs: ['hdr-assemble', '--primary', path('primary.jpg'), '--gain', path('gain.jpg'), '--meta', path('meta.json'), '--out', join(dir, `hdr-${out.length}.npy`)],
        wasm: (mod) => {
          const bytes = mod.hdr_assemble(
            new Uint8Array(readFileSync(path('primary.jpg'))),
            new Uint8Array(readFileSync(path('gain.jpg'))),
            readFileSync(path('meta.json'), 'utf8'),
          );
          return framed(1, bytes.length, bytes);
        },
      });
    }
  }
  return out;
}
