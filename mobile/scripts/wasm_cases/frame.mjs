// Owner: wave-1 unit frame.
// 边框和日期戳的版面：同一份请求、规格和字体，WASM 的输出和 gxr-cli 的 stdout 逐字节相同，错误也一样。
// 输入是一个清单 JSON：[{label, kind: "frame" | "stamp", meta, spec, fonts: [[名字, 路径], ...]}, ...]。
import { readFileSync } from 'node:fs';

function pack(fonts) {
  const index = [];
  const parts = [];
  for (const [name, path] of fonts) {
    const bytes = readFileSync(path);
    index.push([name, bytes.length]);
    parts.push(bytes);
  }
  return { index: JSON.stringify(index), bytes: new Uint8Array(Buffer.concat(parts)) };
}

export function cases(inputs) {
  const out = [];
  for (const manifest of inputs) {
    const list = JSON.parse(readFileSync(manifest, 'utf8'));
    for (const c of list) {
      const fontArgs = c.fonts.flatMap(([name, path]) => ['--font', `${name}=${path}`]);
      const command = c.kind === 'stamp' ? 'stamp-layout' : 'frame-layout';
      out.push({
        label: `${manifest}::${c.label}`,
        cliArgs: [command, '--meta', c.meta, '--spec', c.spec, ...fontArgs],
        wasm: (mod) => {
          const faces = pack(c.fonts);
          const meta = readFileSync(c.meta, 'utf8');
          const spec = readFileSync(c.spec, 'utf8');
          return c.kind === 'stamp'
            ? mod.stamp_layout(meta, spec, faces.index, faces.bytes)
            : mod.frame_layout(meta, spec, faces.index, faces.bytes);
        },
      });
    }
  }
  return out;
}
