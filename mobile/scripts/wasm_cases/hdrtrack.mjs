// HDR-Track: warp_float, crop_valid, begin, warp, box, crop, stamp, frame, to_grid, finish_prepare, finish_wrap.
// inputs is a list of directories containing test artifacts (layer.npy, state.json, rgb.npy, dng, jpg, etc.).
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const dir = mkdtempSync(join(tmpdir(), 'gxr-wasm-hdrtrack-'));
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
  if (descr === '<f4') {
    const ab = payload.buffer.slice(payload.byteOffset, payload.byteOffset + payload.byteLength);
    return { shape, values: new Float32Array(ab) };
  }
  return { shape, values: payload };
}

const BAD_NUMS = [4294967297, -1, 9007199254740992, Number.NaN, Number.POSITIVE_INFINITY, 1.5];
const HUGE_DIMS = [
  [4294967295, 4294967295],
  [2147483648, 1],
];

function deepJsonCases() {
  return {
    array128: '['.repeat(128) + '0' + ']'.repeat(128),
    array129: '['.repeat(129) + '0' + ']'.repeat(129),
    object128: '{"a":'.repeat(128) + '0' + '}'.repeat(128),
    object129: '{"a":'.repeat(129) + '0' + '}'.repeat(129),
    array20000: '['.repeat(20000) + '0' + ']'.repeat(20000),
    object20000: '{"a":'.repeat(20000) + '0' + '}'.repeat(20000),
  };
}

export function cases(inputs) {
  const out = [];
  for (const folder of inputs) {
    const has = (name) => existsSync(join(folder, name));
    const path = (name) => join(folder, name);

    if (has('float_layer.npy') && has('geom.json')) {
      const fNpy = path('float_layer.npy');
      const gJson = readFileSync(path('geom.json'), 'utf8');
      const loadF = () => {
        const d = readNpy(fNpy);
        const h = d.shape[0];
        const w = d.shape[1];
        const c = d.shape.length >= 3 ? d.shape[2] : 1;
        return { values: d.values, w, h, c };
      };
      out.push({
        label: `${folder}::hdrtrack_warp_float_report`,
        cliArgs: ['hdrtrack-warp-float', '--in', fNpy, '--geometry', path('geom.json'), '--out', join(dir, `wf-r-${out.length}.npy`)],
        wasm: (mod) => {
          const d = loadF();
          return mod.hdrtrack_warp_float(d.values, d.w, d.h, d.c, gJson).report;
        },
      });
      out.push({
        label: `${folder}::hdrtrack_warp_float_bytes`,
        cliArgs: ['hdrtrack-warp-float', '--in', fNpy, '--geometry', path('geom.json'), '--out', join(dir, `wf-${out.length}.npy`)],
        wasm: (mod) => {
          const d = loadF();
          const res = mod.hdrtrack_warp_float(d.values, d.w, d.h, d.c, gJson);
          return framed(res.height, res.width, res.bytes());
        },
      });
      if (has('bad_nums')) {
        for (const bad of BAD_NUMS) {
          for (const flag of ['w', 'h', 'channels']) {
            out.push({
              label: `${folder}::hdrtrack_warp_float_${flag}_${String(bad)}`,
              cliArgs: ['hdrtrack-warp-float', '--in', fNpy, `--${flag}`, String(bad), '--geometry', path('geom.json'), '--out', join(dir, `wf-bad-${out.length}.npy`)],
              wasm: (mod) => {
                const d = loadF();
                const w = flag === 'w' ? bad : d.w;
                const h = flag === 'h' ? bad : d.h;
                const c = flag === 'channels' ? bad : d.c;
                return mod.hdrtrack_warp_float(d.values, w, h, c, gJson).report;
              },
            });
          }
        }
        for (const [hw, hh] of HUGE_DIMS) {
          out.push({
            label: `${folder}::hdrtrack_warp_float_huge_${hw}x${hh}`,
            cliArgs: ['hdrtrack-warp-float', '--in', fNpy, '--w', String(hw), '--h', String(hh), '--geometry', path('geom.json'), '--out', join(dir, `wf-huge-${out.length}.npy`)],
            wasm: (mod) => mod.hdrtrack_warp_float(loadF().values, hw, hh, 2, gJson).report,
          });
        }
      }
    }

    if (has('float_layer.npy') && has('rect.json')) {
      const fNpy = path('float_layer.npy');
      const rJson = readFileSync(path('rect.json'), 'utf8');
      const loadF = () => {
        const d = readNpy(fNpy);
        return { values: d.values, w: d.shape[1], h: d.shape[0], c: d.shape.length >= 3 ? d.shape[2] : 1 };
      };
      out.push({
        label: `${folder}::hdrtrack_crop_valid_report`,
        cliArgs: ['hdrtrack-crop-valid', '--in', fNpy, '--rect', path('rect.json'), '--out', join(dir, `cv-r-${out.length}.npy`)],
        wasm: (mod) => {
          const d = loadF();
          return mod.hdrtrack_crop_valid(d.values, d.w, d.h, d.c, rJson).report;
        },
      });
      out.push({
        label: `${folder}::hdrtrack_crop_valid_bytes`,
        cliArgs: ['hdrtrack-crop-valid', '--in', fNpy, '--rect', path('rect.json'), '--out', join(dir, `cv-${out.length}.npy`)],
        wasm: (mod) => {
          const d = loadF();
          const res = mod.hdrtrack_crop_valid(d.values, d.w, d.h, d.c, rJson);
          return framed(res.height, res.width, res.bytes());
        },
      });
      if (has('bad_nums')) {
        for (const bad of BAD_NUMS) {
          for (const flag of ['w', 'h', 'channels']) {
            out.push({
              label: `${folder}::hdrtrack_crop_valid_${flag}_${String(bad)}`,
              cliArgs: ['hdrtrack-crop-valid', '--in', fNpy, `--${flag}`, String(bad), '--rect', path('rect.json'), '--out', join(dir, `cv-bad-${out.length}.npy`)],
              wasm: (mod) => {
                const d = loadF();
                const w = flag === 'w' ? bad : d.w;
                const h = flag === 'h' ? bad : d.h;
                const c = flag === 'channels' ? bad : d.c;
                return mod.hdrtrack_crop_valid(d.values, w, h, c, rJson).report;
              },
            });
          }
        }
      }
    }

    if (has('rgb.npy')) {
      const rgbPath = path('rgb.npy');
      const hasDng = has('input.dng');
      const hasJpg = has('input.jpg');
      const paramsJson = has('params.json') ? readFileSync(path('params.json'), 'utf8') : '';
      const baseArgs = ['hdrtrack-begin', '--rgb', rgbPath];
      if (hasDng) baseArgs.push('--dng', path('input.dng'));
      if (hasJpg) baseArgs.push('--jpeg', path('input.jpg'));
      if (has('params.json')) baseArgs.push('--params', path('params.json'));
      const runBegin = (mod, wOverride, hOverride) => {
        const r = readNpy(rgbPath);
        const w = wOverride ?? r.shape[1];
        const h = hOverride ?? r.shape[0];
        const dngBytes = hasDng ? new Uint8Array(readFileSync(path('input.dng'))) : new Uint8Array(0);
        const jpgBytes = hasJpg ? new Uint8Array(readFileSync(path('input.jpg'))) : new Uint8Array(0);
        return mod.hdrtrack_begin(r.values, w, h, hasDng, dngBytes, '<bytes>', hasJpg, jpgBytes, paramsJson);
      };
      out.push({
        label: `${folder}::hdrtrack_begin_state`,
        cliArgs: [...baseArgs, '--out', join(dir, `begin-s-${out.length}.npy`)],
        wasm: (mod) => runBegin(mod).state,
      });
      out.push({
        label: `${folder}::hdrtrack_begin_layer`,
        cliArgs: [...baseArgs, '--out', join(dir, `begin-l-${out.length}.npy`)],
        wasm: (mod) => {
          const res = runBegin(mod);
          return framed(res.height, res.width, res.layer_bytes());
        },
      });
      if (has('bad_nums')) {
        for (const bad of BAD_NUMS) {
          for (const flag of ['w', 'h']) {
            out.push({
              label: `${folder}::hdrtrack_begin_${flag}_${String(bad)}`,
              cliArgs: ['hdrtrack-begin', '--rgb', rgbPath, `--${flag}`, String(bad), '--out', join(dir, `begin-bad-${out.length}.npy`)],
              wasm: (mod) => {
                const r = readNpy(rgbPath);
                const w = flag === 'w' ? bad : r.shape[1];
                const h = flag === 'h' ? bad : r.shape[0];
                return mod.hdrtrack_begin(r.values, w, h, false, new Uint8Array(0), '<bytes>', false, new Uint8Array(0), '').state;
              },
            });
          }
        }
        for (const [hw, hh] of HUGE_DIMS) {
          out.push({
            label: `${folder}::hdrtrack_begin_huge_${hw}x${hh}`,
            cliArgs: ['hdrtrack-begin', '--rgb', rgbPath, '--w', String(hw), '--h', String(hh), '--out', join(dir, `begin-huge-${out.length}.npy`)],
            wasm: (mod) => mod.hdrtrack_begin(readNpy(rgbPath).values, hw, hh, false, new Uint8Array(0), '<bytes>', false, new Uint8Array(0), '').state,
          });
        }
      }
    }

    if (has('layer.npy') && has('state.json')) {
      const lPath = path('layer.npy');
      const sPath = path('state.json');
      const sJson = readFileSync(sPath, 'utf8');

      if (has('geom.json')) {
        const gJson = readFileSync(path('geom.json'), 'utf8');
        const args = ['hdrtrack-warp', '--layer', lPath, '--state', sPath, '--geometry', path('geom.json')];
        out.push({
          label: `${folder}::hdrtrack_warp_state`,
          cliArgs: [...args, '--out', join(dir, `tw-s-${out.length}.npy`)],
          wasm: (mod) => mod.hdrtrack_warp(readNpy(lPath).values, sJson, gJson).state,
        });
        out.push({
          label: `${folder}::hdrtrack_warp_layer`,
          cliArgs: [...args, '--out', join(dir, `tw-l-${out.length}.npy`)],
          wasm: (mod) => {
            const res = mod.hdrtrack_warp(readNpy(lPath).values, sJson, gJson);
            return framed(res.height, res.width, res.layer_bytes());
          },
        });
      }

      if (has('box.json')) {
        const b = JSON.parse(readFileSync(path('box.json'), 'utf8'));
        const args = ['hdrtrack-box', '--layer', lPath, '--state', sPath, '--x', String(b.x), '--y', String(b.y), '--w', String(b.w), '--h', String(b.h)];
        out.push({
          label: `${folder}::hdrtrack_box_state`,
          cliArgs: [...args, '--out', join(dir, `tb-s-${out.length}.npy`)],
          wasm: (mod) => mod.hdrtrack_box(readNpy(lPath).values, sJson, b.x, b.y, b.w, b.h).state,
        });
        out.push({
          label: `${folder}::hdrtrack_box_layer`,
          cliArgs: [...args, '--out', join(dir, `tb-l-${out.length}.npy`)],
          wasm: (mod) => {
            const res = mod.hdrtrack_box(readNpy(lPath).values, sJson, b.x, b.y, b.w, b.h);
            return framed(res.height, res.width, res.layer_bytes());
          },
        });
        if (has('bad_nums')) {
          for (const bad of BAD_NUMS) {
            for (const flag of ['x', 'y', 'w', 'h']) {
              const bx = flag === 'x' ? bad : b.x;
              const by = flag === 'y' ? bad : b.y;
              const bw = flag === 'w' ? bad : b.w;
              const bh = flag === 'h' ? bad : b.h;
              out.push({
                label: `${folder}::hdrtrack_box_${flag}_${String(bad)}`,
                cliArgs: ['hdrtrack-box', '--layer', lPath, '--state', sPath, '--x', String(bx), '--y', String(by), '--w', String(bw), '--h', String(bh), '--out', join(dir, `tb-bad-${out.length}.npy`)],
                wasm: (mod) => mod.hdrtrack_box(readNpy(lPath).values, sJson, bx, by, bw, bh).state,
              });
            }
          }
        }
      }

      if (has('rect.json')) {
        const rJson = readFileSync(path('rect.json'), 'utf8');
        const args = ['hdrtrack-crop', '--layer', lPath, '--state', sPath, '--rect', path('rect.json')];
        out.push({
          label: `${folder}::hdrtrack_crop_state`,
          cliArgs: [...args, '--out', join(dir, `tc-s-${out.length}.npy`)],
          wasm: (mod) => mod.hdrtrack_crop(readNpy(lPath).values, sJson, rJson).state,
        });
        out.push({
          label: `${folder}::hdrtrack_crop_layer`,
          cliArgs: [...args, '--out', join(dir, `tc-l-${out.length}.npy`)],
          wasm: (mod) => {
            const res = mod.hdrtrack_crop(readNpy(lPath).values, sJson, rJson);
            return framed(res.height, res.width, res.layer_bytes());
          },
        });
      }

      if (has('mask.npy')) {
        const mPath = path('mask.npy');
        const args = ['hdrtrack-stamp', '--layer', lPath, '--state', sPath, '--mask', mPath];
        out.push({
          label: `${folder}::hdrtrack_stamp_state`,
          cliArgs: [...args, '--out', join(dir, `ts-s-${out.length}.npy`)],
          wasm: (mod) => {
            const m = readNpy(mPath);
            return mod.hdrtrack_stamp(readNpy(lPath).values, sJson, m.values, m.shape[1], m.shape[0]).state;
          },
        });
        out.push({
          label: `${folder}::hdrtrack_stamp_layer`,
          cliArgs: [...args, '--out', join(dir, `ts-l-${out.length}.npy`)],
          wasm: (mod) => {
            const m = readNpy(mPath);
            const res = mod.hdrtrack_stamp(readNpy(lPath).values, sJson, m.values, m.shape[1], m.shape[0]);
            return framed(res.height, res.width, res.layer_bytes());
          },
        });
        if (has('bad_nums')) {
          for (const bad of BAD_NUMS) {
            for (const flag of ['w', 'h']) {
              out.push({
                label: `${folder}::hdrtrack_stamp_${flag}_${String(bad)}`,
                cliArgs: ['hdrtrack-stamp', '--layer', lPath, '--state', sPath, '--mask', mPath, `--${flag}`, String(bad), '--out', join(dir, `ts-bad-${out.length}.npy`)],
                wasm: (mod) => {
                  const m = readNpy(mPath);
                  const w = flag === 'w' ? bad : m.shape[1];
                  const h = flag === 'h' ? bad : m.shape[0];
                  return mod.hdrtrack_stamp(readNpy(lPath).values, sJson, m.values, w, h).state;
                },
              });
            }
          }
        }
      }

      if (has('layout.json')) {
        const layJson = readFileSync(path('layout.json'), 'utf8');
        const args = ['hdrtrack-frame', '--layer', lPath, '--state', sPath, '--layout', path('layout.json')];
        out.push({
          label: `${folder}::hdrtrack_frame_state`,
          cliArgs: [...args, '--out', join(dir, `tf-s-${out.length}.npy`)],
          wasm: (mod) => mod.hdrtrack_frame(readNpy(lPath).values, sJson, layJson).state,
        });
        out.push({
          label: `${folder}::hdrtrack_frame_layer`,
          cliArgs: [...args, '--out', join(dir, `tf-l-${out.length}.npy`)],
          wasm: (mod) => {
            const res = mod.hdrtrack_frame(readNpy(lPath).values, sJson, layJson);
            return framed(res.height, res.width, res.layer_bytes());
          },
        });
      }

      if (has('finish_rgb.npy')) {
        const rgbPath = path('finish_rgb.npy');
        const fmt = has('fmt.txt') ? readFileSync(path('fmt.txt'), 'utf8').trim() : 'jpeg';
        const paramsJson = has('finish_params.json') ? readFileSync(path('finish_params.json'), 'utf8') : '';
        const args = ['hdrtrack-finish-prepare', '--layer', lPath, '--state', sPath, '--fmt', fmt, '--rgb', rgbPath];
        if (has('finish_params.json')) args.push('--params', path('finish_params.json'));
        const runPrep = (mod, wOverride, hOverride) => {
          const r = readNpy(rgbPath);
          const w = wOverride ?? r.shape[1];
          const h = hOverride ?? r.shape[0];
          return mod.hdrtrack_finish_prepare(readNpy(lPath).values, sJson, fmt, r.values, w, h, paramsJson);
        };
        out.push({
          label: `${folder}::hdrtrack_finish_prepare_report`,
          cliArgs: [...args, '--out', join(dir, `fp-r-${out.length}.npy`)],
          wasm: (mod) => runPrep(mod).report,
        });
        out.push({
          label: `${folder}::hdrtrack_finish_prepare_codes`,
          cliArgs: [...args, '--out', join(dir, `fp-c-${out.length}.npy`)],
          wasm: (mod) => {
            const res = runPrep(mod);
            return framed(res.height, res.width, res.codes());
          },
        });
        if (has('bad_nums')) {
          for (const bad of BAD_NUMS) {
            for (const flag of ['w', 'h']) {
              out.push({
                label: `${folder}::hdrtrack_finish_prepare_${flag}_${String(bad)}`,
                cliArgs: ['hdrtrack-finish-prepare', '--layer', lPath, '--state', sPath, '--fmt', fmt, '--rgb', rgbPath, `--${flag}`, String(bad), '--out', join(dir, `fp-bad-${out.length}.npy`)],
                wasm: (mod) => {
                  const r = readNpy(rgbPath);
                  const w = flag === 'w' ? bad : r.shape[1];
                  const h = flag === 'h' ? bad : r.shape[0];
                  return runPrep(mod, w, h).report;
                },
              });
            }
          }
        }
      }

      if (has('json_depth')) {
        for (const [name, text] of Object.entries(deepJsonCases())) {
          const deepPath = join(dir, `deep-${name}-${out.length}.json`);
          writeFileSync(deepPath, text);
          out.push({
            label: `${folder}::hdrtrack_state_json_${name}`,
            cliArgs: ['hdrtrack-warp', '--layer', lPath, '--state', deepPath, '--geometry-json', '{}', '--out', join(dir, `d-st-${out.length}.npy`)],
            wasm: (mod) => mod.hdrtrack_warp(readNpy(lPath).values, text, '{}').state,
          });
          out.push({
            label: `${folder}::hdrtrack_geom_json_${name}`,
            cliArgs: ['hdrtrack-warp', '--layer', lPath, '--state', sPath, '--geometry', deepPath, '--out', join(dir, `d-gm-${out.length}.npy`)],
            wasm: (mod) => mod.hdrtrack_warp(readNpy(lPath).values, sJson, text).state,
          });
          out.push({
            label: `${folder}::hdrtrack_rect_json_${name}`,
            cliArgs: ['hdrtrack-crop', '--layer', lPath, '--state', sPath, '--rect', deepPath, '--out', join(dir, `d-rc-${out.length}.npy`)],
            wasm: (mod) => mod.hdrtrack_crop(readNpy(lPath).values, sJson, text).state,
          });
          out.push({
            label: `${folder}::hdrtrack_layout_json_${name}`,
            cliArgs: ['hdrtrack-frame', '--layer', lPath, '--state', sPath, '--layout', deepPath, '--out', join(dir, `d-ly-${out.length}.npy`)],
            wasm: (mod) => mod.hdrtrack_frame(readNpy(lPath).values, sJson, text).state,
          });
        }
      }
    }

    if (has('to_grid.json') && has('layer.npy')) {
      const lPath = path('layer.npy');
      const tg = JSON.parse(readFileSync(path('to_grid.json'), 'utf8'));
      const args = ['hdrtrack-to-grid', '--layer', lPath, '--out-w', String(tg.out_w), '--out-h', String(tg.out_h), '--factor', String(tg.factor)];
      out.push({
        label: `${folder}::hdrtrack_to_grid_report`,
        cliArgs: [...args, '--out', join(dir, `tg-r-${out.length}.npy`), '--clip-out', join(dir, `tg-c-${out.length}.npy`)],
        wasm: (mod) => {
          const l = readNpy(lPath);
          return mod.hdrtrack_to_grid(l.values, l.shape[1], l.shape[0], tg.out_w, tg.out_h, tg.factor).report;
        },
      });
      out.push({
        label: `${folder}::hdrtrack_to_grid_bytes`,
        cliArgs: [...args, '--out', join(dir, `tg-y-${out.length}.npy`), '--clip-out', join(dir, `tg-yc-${out.length}.npy`)],
        wasm: (mod) => {
          const l = readNpy(lPath);
          const res = mod.hdrtrack_to_grid(l.values, l.shape[1], l.shape[0], tg.out_w, tg.out_h, tg.factor);
          return framed(res.gh, res.gw, res.raw_y_bytes());
        },
      });
      if (has('bad_nums')) {
        for (const bad of BAD_NUMS) {
          for (const flag of ['w', 'h', 'out-w', 'out-h', 'factor']) {
            const l = readNpy(lPath);
            const lw = flag === 'w' ? bad : l.shape[1];
            const lh = flag === 'h' ? bad : l.shape[0];
            const ow = flag === 'out-w' ? bad : tg.out_w;
            const oh = flag === 'out-h' ? bad : tg.out_h;
            const f = flag === 'factor' ? bad : tg.factor;
            out.push({
              label: `${folder}::hdrtrack_to_grid_${flag}_${String(bad)}`,
              cliArgs: ['hdrtrack-to-grid', '--layer', lPath, '--w', String(lw), '--h', String(lh), '--out-w', String(ow), '--out-h', String(oh), '--factor', String(f), '--out', join(dir, `tg-bad-${out.length}.npy`), '--clip-out', join(dir, `tg-badc-${out.length}.npy`)],
              wasm: (mod) => mod.hdrtrack_to_grid(readNpy(lPath).values, lw, lh, ow, oh, f).report,
            });
          }
        }
      }
    }

    if (has('payload.jpg') && has('meta.json')) {
      const pPath = path('payload.jpg');
      const mPath = path('meta.json');
      const mJson = readFileSync(mPath, 'utf8');
      const hasGain = has('gain.jpg');
      const stops = has('stops.txt') ? Number(readFileSync(path('stops.txt'), 'utf8').trim()) : 1.5;
      const args = ['hdrtrack-finish-wrap', '--payload', pPath, '--meta', mPath, '--gain-max-stops', String(stops)];
      if (hasGain) args.push('--gain', path('gain.jpg'));
      const runWrap = (mod, sOverride) => {
        const pBytes = new Uint8Array(readFileSync(pPath));
        const gBytes = hasGain ? new Uint8Array(readFileSync(path('gain.jpg'))) : new Uint8Array(0);
        return mod.hdrtrack_finish_wrap(pBytes, gBytes, mJson, sOverride ?? stops);
      };
      out.push({
        label: `${folder}::hdrtrack_finish_wrap_report`,
        cliArgs: [...args, '--out', join(dir, `fw-r-${out.length}.npy`)],
        wasm: (mod) => runWrap(mod).report,
      });
      out.push({
        label: `${folder}::hdrtrack_finish_wrap_bytes`,
        cliArgs: [...args, '--out', join(dir, `fw-b-${out.length}.npy`)],
        wasm: (mod) => {
          const res = runWrap(mod);
          const bytes = res.bytes();
          return framed(1, bytes.length, bytes);
        },
      });
      if (has('bad_nums')) {
        for (const bad of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
          out.push({
            label: `${folder}::hdrtrack_finish_wrap_stops_${String(bad)}`,
            cliArgs: ['hdrtrack-finish-wrap', '--payload', pPath, '--meta', mPath, '--gain-max-stops', String(bad), '--out', join(dir, `fw-bad-${out.length}.npy`)],
            wasm: (mod) => runWrap(mod, bad).report,
          });
        }
      }
      if (has('json_depth')) {
        for (const [name, text] of Object.entries(deepJsonCases())) {
          const deepPath = join(dir, `deep-meta-${name}-${out.length}.json`);
          writeFileSync(deepPath, text);
          out.push({
            label: `${folder}::hdrtrack_meta_json_${name}`,
            cliArgs: ['hdrtrack-finish-wrap', '--payload', pPath, '--meta', deepPath, '--gain-max-stops', '1.0', '--out', join(dir, `d-mt-${out.length}.npy`)],
            wasm: (mod) => mod.hdrtrack_finish_wrap(new Uint8Array(readFileSync(pPath)), new Uint8Array(0), text, 1.0).report,
          });
        }
      }
    }
  }
  return out;
}
