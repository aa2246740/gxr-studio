// 导出底图：source_plan 和 source_render。每个输入是一份 JSON，列出这一例要用的文件（测试现场生成）。
// plan:   {"kind": "plan", "shot", "edit", "dat"?, "library"?, "entry"?, "calib"?, "binding"?}
// render: {"kind": "render", "dng", "entry", "stock", "unit", "calib", "iso", "adjust"?, "out"}
// calib 是一个文件夹：CLI 读里面每个文件，WASM 按文件名排序后拼成一段字节加清单。
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { basename, join } from 'node:path';

const bytes = (path) => new Uint8Array(readFileSync(path));
const empty = new Uint8Array(0);
const text = (value) => new TextEncoder().encode(value);

function pack(dir) {
  if (!dir) return { index: text('[]'), blob: empty };
  const names = readdirSync(dir)
    .filter((name) => statSync(join(dir, name)).isFile())
    .sort((a, b) => (Buffer.from(a) < Buffer.from(b) ? -1 : Buffer.from(a) > Buffer.from(b) ? 1 : 0));
  const parts = names.map((name) => readFileSync(join(dir, name)));
  const index = names.map((name, i) => [name, parts[i].length]);
  return { index: text(JSON.stringify(index)), blob: new Uint8Array(Buffer.concat(parts)) };
}

export function cases(inputs) {
  const out = [];
  for (const file of inputs) {
    const spec = JSON.parse(readFileSync(file, 'utf8'));
    if (spec.kind === 'plan') {
      const cliArgs = ['source-plan', '--shot', spec.shot, '--edit', spec.edit];
      if (spec.dat) cliArgs.push('--dat', spec.dat);
      if (spec.library) cliArgs.push('--library', spec.library);
      if (spec.entry) cliArgs.push('--entry', spec.entry);
      if (spec.calib) cliArgs.push('--calib', spec.calib);
      if (spec.binding !== undefined) cliArgs.push('--binding', spec.binding);
      out.push({
        label: `${file}::source_plan`,
        cliArgs,
        wasm: (mod) => {
          const { index, blob } = pack(spec.calib);
          return mod.source_plan(
            bytes(spec.shot),
            bytes(spec.edit),
            spec.dat ? bytes(spec.dat) : empty,
            spec.library ? bytes(spec.library) : empty,
            spec.entry ? bytes(spec.entry) : empty,
            index,
            blob,
            spec.binding ?? '',
          );
        },
      });
    } else if (spec.kind === 'render') {
      const cliArgs = ['source-render', '--dng', spec.dng, '--entry', spec.entry, '--stock', spec.stock];
      cliArgs.push('--unit', spec.unit, '--calib', spec.calib, '--iso', String(spec.iso));
      if (spec.adjust) cliArgs.push('--adjust', spec.adjust);
      cliArgs.push('--out', spec.out);
      out.push({
        label: `${file}::source_render`,
        cliArgs,
        wasm: (mod) => {
          const { index, blob } = pack(spec.calib);
          const started = performance.now();
          const framed = mod.source_render(
            bytes(spec.dng),
            basename(spec.dng),
            bytes(spec.entry),
            bytes(spec.stock),
            spec.unit,
            index,
            blob,
            spec.iso,
            spec.adjust ? bytes(spec.adjust) : empty,
          );
          if (spec.timing) console.log(`source_render ${basename(spec.dng)} ${(performance.now() - started).toFixed(0)} ms`);
          return framed;
        },
      });
    } else {
      throw new Error(`unknown case kind ${spec.kind}`);
    }
  }
  return out;
}
