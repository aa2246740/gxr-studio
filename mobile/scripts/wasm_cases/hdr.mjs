// HDR：对齐、增益图（uint8 码值和元数据 JSON）、容器字节。
// inputs 是测试写好的目录，里面按需放 sdr.npy、raw.npy（对齐用）、reg.npy + clip.npy（增益用）、
// params.json，以及 primary.jpg、gain.jpg、meta.json（容器用）。缺哪个文件就跳过那一项。
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
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
    if (has('primary.jpg') && has('gain.jpg')) {
      // JSON nesting on both sides of the 128 cap, and far past it; arrays and objects.
      const deep = {
        array128: '['.repeat(128) + '0' + ']'.repeat(128),
        array129: '['.repeat(129) + '0' + ']'.repeat(129),
        object128: '{"a":'.repeat(128) + '0' + '}'.repeat(128),
        object129: '{"a":'.repeat(129) + '0' + '}'.repeat(129),
        array20000: '['.repeat(20000) + '0' + ']'.repeat(20000),
        object20000: '{"a":'.repeat(20000) + '0' + '}'.repeat(20000),
      };
      for (const [name, text] of Object.entries(deep)) {
        const metaPath = join(dir, `meta-${name}-${out.length}.json`);
        writeFileSync(metaPath, text);
        out.push({
          label: `${folder}::hdr_assemble_json_${name}`,
          cliArgs: ['hdr-assemble', '--primary', path('primary.jpg'), '--gain', path('gain.jpg'), '--meta', metaPath, '--out', join(dir, `deep-${out.length}.npy`)],
          wasm: (mod) => {
            const bytes = mod.hdr_assemble(
              new Uint8Array(readFileSync(path('primary.jpg'))),
              new Uint8Array(readFileSync(path('gain.jpg'))),
              text,
            );
            return framed(1, bytes.length, bytes);
          },
        });
      }
    }
    if (has('r1-s.npy') && has('r1-raw.npy') && has('r1-clip.npy') && has('r1-clip-short.npy') && has('r1-fft.npy')) {
      // UNIT-COMMON allocation rule 1 (Sol r3 P2-10): Sol's small inputs, with the right
      // dimensions and with same-size mismatched dimensions or a short clip mask. Both sides
      // check before copying and give the same result or error.
      const s = () => readNpy(path('r1-s.npy'));
      for (const [h, w] of [
        [32, 32],
        [31, 32],
        [32, 33],
        [16, 64],
      ]) {
        out.push({
          label: `${folder}::rule1_align_${h}x${w}`,
          cliArgs: ['hdr-align', '--sdr', path('r1-s.npy'), '--raw', path('r1-s.npy'), '--sdr-shape', String(h), String(w), '--raw-shape', String(h), String(w)],
          wasm: (mod) => mod.hdr_align(s().values, h, w, s().values, h, w),
        });
        out.push({
          label: `${folder}::rule1_phase_plane_${h}x${w}`,
          cliArgs: ['hdr-phase-plane', '--a', path('r1-s.npy'), '--b', path('r1-raw.npy'), '--a-shape', String(h), String(w), '--b-shape', String(h), String(w), '--out', join(dir, `r1pc-${out.length}.npy`)],
          wasm: (mod) => mod.hdr_phase_plane(s().values, readNpy(path('r1-raw.npy')).values, h, w).report,
        });
      }
      for (const [h, w, clip] of [
        [32, 32, 'r1-clip.npy'],
        [32, 32, 'r1-clip-short.npy'],
        [31, 32, 'r1-clip.npy'],
      ]) {
        out.push({
          label: `${folder}::rule1_gain_${h}x${w}_${clip}`,
          cliArgs: ['hdr-gain', '--sdr', path('r1-s.npy'), '--raw', path('r1-raw.npy'), '--clip', path(clip), '--sdr-shape', String(h), String(w), '--raw-shape', String(h), String(w), '--out', join(dir, `r1g-${out.length}.npy`)],
          wasm: (mod) => mod.hdr_gain(s().values, readNpy(path('r1-raw.npy')).values, readNpy(path(clip)).values, h, w, '').report,
        });
      }
      for (const [rows, n] of [
        [1, 13],
        [1, 12],
        [2, 13],
      ]) {
        out.push({
          label: `${folder}::rule1_fft_${rows}x${n}`,
          cliArgs: ['hdr-fft', '--in', path('r1-fft.npy'), '--inverse', '0', '--in-shape', String(rows), String(n), '--out', join(dir, `r1fft-${out.length}.npy`)],
          wasm: (mod) => framed(rows, n, mod.hdr_fft(readNpy(path('r1-fft.npy')).values, rows, n, false, false)),
        });
      }
    }
    if (has('numeric_cases.json')) {
      const jsNum = (v) => {
        if (v === 'NaN') return Number.NaN;
        if (v === 'Infinity') return Number.POSITIVE_INFINITY;
        if (v === '-Infinity') return Number.NEGATIVE_INFINITY;
        return v;
      };
      const specs = JSON.parse(readFileSync(path('numeric_cases.json'), 'utf8'));
      for (const spec of specs) {
        if (spec.fn === 'hdr_align') {
          const sh = jsNum(spec.sdr_h);
          const sw = jsNum(spec.sdr_w);
          const rh = jsNum(spec.raw_h);
          const rw = jsNum(spec.raw_w);
          const csh = spec.cli_sdr_h ?? sh;
          const csw = spec.cli_sdr_w ?? sw;
          const crh = spec.cli_raw_h ?? rh;
          const crw = spec.cli_raw_w ?? rw;
          out.push({
            label: `${folder}::num_${spec.label}`,
            cliArgs: [
              'hdr-align',
              '--sdr',
              path(spec.sdr),
              '--raw',
              path(spec.raw),
              '--sdr-shape',
              String(csh),
              String(csw),
              '--raw-shape',
              String(crh),
              String(crw),
            ],
            wasm: (mod) =>
              mod.hdr_align(readNpy(path(spec.sdr)).values, sh, sw, readNpy(path(spec.raw)).values, rh, rw),
          });
        } else if (spec.fn === 'hdr_gain') {
          const h = jsNum(spec.h);
          const w = jsNum(spec.w);
          const ch = spec.cli_h ?? h;
          const cw = spec.cli_w ?? w;
          const cliArgs = [
            'hdr-gain',
            '--sdr',
            path(spec.sdr),
            '--raw',
            path(spec.raw),
            '--clip',
            path(spec.clip),
            '--sdr-shape',
            String(ch),
            String(cw),
            '--raw-shape',
            String(ch),
            String(cw),
            '--out',
            join(dir, `num-gain-${out.length}.npy`),
          ];
          if (spec.params !== undefined && spec.params !== '') {
            const paramsPath = join(dir, `num-params-${out.length}.json`);
            writeFileSync(paramsPath, spec.params);
            cliArgs.push('--params', paramsPath);
          }
          out.push({
            label: `${folder}::num_${spec.label}`,
            cliArgs,
            wasm: (mod) => {
              const g = mod.hdr_gain(
                readNpy(path(spec.sdr)).values,
                readNpy(path(spec.raw)).values,
                readNpy(path(spec.clip)).values,
                h,
                w,
                spec.params ?? '',
              );
              return spec.check_codes ? framed(g.height, g.width, g.codes()) : g.report;
            },
          });
        } else if (spec.fn === 'hdr_fft') {
          const rows = jsNum(spec.rows);
          const n = jsNum(spec.n);
          const crows = spec.cli_rows ?? rows;
          const cn = spec.cli_n ?? n;
          const inverse = Boolean(spec.inverse);
          const twoD = Boolean(spec.two_d);
          const cliArgs = [
            'hdr-fft',
            '--in',
            path(spec.input),
            '--inverse',
            inverse ? '1' : '0',
            '--in-shape',
            String(crows),
            String(cn),
            '--out',
            join(dir, `num-fft-${out.length}.npy`),
          ];
          if (twoD) cliArgs.push('--two-d');
          out.push({
            label: `${folder}::num_${spec.label}`,
            cliArgs,
            wasm: (mod) => framed(rows, n, mod.hdr_fft(readNpy(path(spec.input)).values, rows, n, inverse, twoD)),
          });
        } else if (spec.fn === 'hdr_phase_plane') {
          const h = jsNum(spec.h);
          const w = jsNum(spec.w);
          const ch = spec.cli_h ?? h;
          const cw = spec.cli_w ?? w;
          out.push({
            label: `${folder}::num_${spec.label}`,
            cliArgs: [
              'hdr-phase-plane',
              '--a',
              path(spec.a),
              '--b',
              path(spec.b),
              '--a-shape',
              String(ch),
              String(cw),
              '--b-shape',
              String(ch),
              String(cw),
              '--out',
              join(dir, `num-pc-${out.length}.npy`),
            ],
            wasm: (mod) => {
              const pc = mod.hdr_phase_plane(readNpy(path(spec.a)).values, readNpy(path(spec.b)).values, h, w);
              return spec.check_plane ? framed(h, w, pc.plane()) : pc.report;
            },
          });
        }
      }
    }
  }
  out.push(...fftCases(inputs));
  out.push(...lumaCases());
  return out;
}

// hdr_luma：合成 RGB，和 gxr-cli hdr-luma 比 float32 网格。--rgb-shape 让短缓冲和超大尺寸走同一条校验。
function npyU8(shape, data) {
  const dims = shape.join(', ');
  let dict = `{'descr': '|u1', 'fortran_order': False, 'shape': (${dims}), }`;
  const spaces = (64 - ((11 + dict.length) % 64)) % 64;
  dict = `${dict}${' '.repeat(spaces)}\n`;
  const header = Buffer.alloc(10 + dict.length);
  header.write('\u0093NUMPY', 0, 'latin1');
  header[6] = 1;
  header[7] = 0;
  header.writeUInt16LE(dict.length, 8);
  header.write(dict, 10, 'latin1');
  return Buffer.concat([header, Buffer.from(data)]);
}

function lumaCases() {
  const out = [];
  const rgbOf = (h, w) => {
    const data = new Uint8Array(h * w * 3);
    for (let i = 0; i < data.length; i++) data[i] = (i * 17 + 3) & 255;
    return data;
  };
  const writeRgb = (name, h, w, data) => {
    const path = join(dir, name);
    writeFileSync(path, npyU8([h, w, 3], data));
    return path;
  };
  const push = (label, file, h, w, factor, override) => {
    const args = ['hdr-luma', '--rgb', file, '--factor', String(factor), '--out', join(dir, `${label}.npy`)];
    if (override) args.push('--rgb-shape', String(h), String(w));
    out.push({
      label: `hdr-luma::${label}`,
      cliArgs: args,
      wasm: (mod) => {
        const rgb = readNpy(file);
        const g = mod.hdr_luma(rgb.values, h, w, factor);
        return framed(g.height, g.width, g.bytes());
      },
    });
  };
  const samples = [
    [1, 1, 1],
    [2, 3, 1],
    [5, 7, 2],
    [8, 8, 4],
    [15, 9, 4],
    [17, 13, 16],
    [16, 16, 16],
    [4, 1, 1],
    [0, 0, 1],
  ];
  for (const [h, w, factor] of samples) {
    const file = writeRgb(`luma-${h}x${w}-f${factor}.npy`, h, w, rgbOf(h, w));
    push(`size_${h}x${w}_f${factor}`, file, h, w, factor, false);
  }
  const base = writeRgb('luma-base-2x2.npy', 2, 2, rgbOf(2, 2));
  const empty = writeRgb('luma-empty.npy', 0, 0, new Uint8Array(0));
  push('empty', empty, 4, 4, 1, true);
  push('short', base, 3, 5, 1, true);
  push('huge', base, 4294967295, 4294967295, 1, true);
  push('huge_wide', base, 2147483648, 1, 1, true);
  push('factor0', base, 2, 2, 0, false);
  push('factor_huge', base, 2, 2, 4294967296, false);
  return out;
}

// ---- hdr-fft ----
// Folders with fft.npy (float32 (h, n, 2) complex64 pairs) and/or pa.npy + pb.npy (float32
// planes for hdr-phase-plane); fft0.npy is an empty (1, 0, 2) array for numpy's n = 0 error.
export function fftCases(inputs) {
  const out = [];
  for (const folder of inputs) {
    const has = (name) => existsSync(join(folder, name));
    const path = (name) => join(folder, name);
    if (has('fft.npy')) {
      for (const inverse of [false, true]) {
        for (const twoD of [false, true]) {
          const args = ['hdr-fft', '--in', path('fft.npy'), '--inverse', inverse ? '1' : '0'];
          if (twoD) args.push('--two-d');
          out.push({
            label: `${folder}::hdr_fft_${inverse ? 'inv' : 'fwd'}${twoD ? '_2d' : ''}`,
            cliArgs: [...args, '--out', join(dir, `fft-${out.length}.npy`)],
            wasm: (mod) => {
              const x = readNpy(path('fft.npy'));
              return framed(x.shape[0], x.shape[1], mod.hdr_fft(x.values, x.shape[0], x.shape[1], inverse, twoD));
            },
          });
        }
      }
      for (const [h, n] of [
        [4294967295, 4294967295],
        [2147483648, 1],
        [1, 2147483648],
      ]) {
        out.push({
          label: `${folder}::hdr_fft_huge_${h}x${n}`,
          cliArgs: ['hdr-fft', '--in', path('fft.npy'), '--inverse', '0', '--in-shape', String(h), String(n), '--out', join(dir, `fft-huge-${out.length}.npy`)],
          wasm: (mod) => mod.hdr_fft(readNpy(path('fft.npy')).values, h, n, false, false),
        });
      }
    }
    if (has('fft-empty.npy')) {
      // An empty batch (0, n, 2): 1-D returns the same empty shape; 2-D refuses axis 0.
      for (const inverse of [false, true]) {
        for (const twoD of [false, true]) {
          const args = ['hdr-fft', '--in', path('fft-empty.npy'), '--inverse', inverse ? '1' : '0'];
          if (twoD) args.push('--two-d');
          out.push({
            label: `${folder}::hdr_fft_empty_batch_${inverse ? 'inv' : 'fwd'}${twoD ? '_2d' : ''}`,
            cliArgs: [...args, '--out', join(dir, `fft-empty-${out.length}.npy`)],
            wasm: (mod) => {
              const x = readNpy(path('fft-empty.npy'));
              return framed(x.shape[0], x.shape[1], mod.hdr_fft(x.values, x.shape[0], x.shape[1], inverse, twoD));
            },
          });
        }
      }
    }
    if (has('fft0.npy')) {
      out.push({
        label: `${folder}::hdr_fft_zero`,
        cliArgs: ['hdr-fft', '--in', path('fft0.npy'), '--inverse', '0', '--out', join(dir, `fft0-${out.length}.npy`)],
        wasm: (mod) => mod.hdr_fft(new Float32Array(0), 1, 0, false, false),
      });
    }
    if (has('pa.npy') && has('pb.npy')) {
      const run = (mod) => {
        const a = readNpy(path('pa.npy'));
        const b = readNpy(path('pb.npy'));
        return mod.hdr_phase_plane(a.values, b.values, a.shape[0], a.shape[1]);
      };
      out.push({
        label: `${folder}::hdr_phase_plane_report`,
        cliArgs: ['hdr-phase-plane', '--a', path('pa.npy'), '--b', path('pb.npy'), '--out', join(dir, `pc-r-${out.length}.npy`)],
        wasm: (mod) => run(mod).report,
      });
      out.push({
        label: `${folder}::hdr_phase_plane`,
        cliArgs: ['hdr-phase-plane', '--a', path('pa.npy'), '--b', path('pb.npy'), '--out', join(dir, `pc-${out.length}.npy`)],
        wasm: (mod) => {
          const a = readNpy(path('pa.npy'));
          return framed(a.shape[0], a.shape[1], run(mod).plane());
        },
      });
      for (const [h, w] of [
        [4294967295, 4294967295],
        [2147483648, 1],
      ]) {
        const dims = ['--a-shape', String(h), String(w), '--b-shape', String(h), String(w)];
        out.push({
          label: `${folder}::hdr_phase_plane_huge_${h}x${w}`,
          cliArgs: ['hdr-phase-plane', '--a', path('pa.npy'), '--b', path('pb.npy'), ...dims, '--out', join(dir, `pc-huge-${out.length}.npy`)],
          wasm: (mod) => {
            const a = readNpy(path('pa.npy'));
            return mod.hdr_phase_plane(a.values, a.values, h, w).report;
          },
        });
      }
    }
  }
  return out;
}
