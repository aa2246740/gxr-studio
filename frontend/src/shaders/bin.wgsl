// RAW 条带字节 → 半分辨率白平衡码值（raw/codes.py half_res_codes：裁切窗口内每个 2×2 同色取平均 / AsShotNeutral × 4095，不去马赛克），
// 再按 Orientation 转正（raw/codes.py orient_array）。输出 rgba32float。
struct R {
  crop: vec4f,    // 裁切窗口：y0、x0、高、宽（RAW 平面坐标）
  wb: vec4f,      // 4095 / AsShotNeutral（R、G、B）
  info: vec4f,    // 每行字节数、位深（12 / 16）、大端（1）、方向（1..8）
  phase: vec4f,   // 裁切窗口第一个 2×2 的颜色（行优先）
  black: vec4f,   // 黑电平：相对 ActiveArea 的 (y%2, x%2) 四个值（行优先）
  level: vec4f,   // 白电平、ActiveArea 顶、ActiveArea 左
};
@group(0) @binding(0) var<uniform> r: R;
@group(0) @binding(1) var<storage, read> raw: array<u32>;
@group(0) @binding(2) var outTex: texture_storage_2d<rgba32float, write>;

fn byteAt(i: u32) -> u32 { return (raw[i >> 2u] >> ((i & 3u) * 8u)) & 255u; }

fn sample(y: u32, x: u32) -> f32 {
  let rb = u32(r.info.x);
  var v: u32;
  if (r.info.y == 16.0) {
    let b = y * rb + x * 2u;
    if (r.info.z > 0.5) { v = (byteAt(b) << 8u) | byteAt(b + 1u); } else { v = byteAt(b) | (byteAt(b + 1u) << 8u); }
  } else {
    let b = y * rb + (x >> 1u) * 3u;
    if ((x & 1u) == 0u) { v = (byteAt(b) << 4u) | (byteAt(b + 1u) >> 4u); } else { v = ((byteAt(b + 1u) & 15u) << 8u) | byteAt(b + 2u); }
  }
  let ry = (y - u32(r.level.y)) & 1u;
  let rx = (x - u32(r.level.z)) & 1u;
  let black = r.black[ry * 2u + rx];
  return (f32(v) - black) / (r.level.x - black);
}

@compute @workgroup_size(16, 16) fn main(@builtin(global_invocation_id) id: vec3u) {
  let w2 = u32(r.crop.w) / 2u;
  let h2 = u32(r.crop.z) / 2u;
  let o = u32(r.info.w);
  let swap = o >= 5u;
  let wo = select(w2, h2, swap);
  let ho = select(h2, w2, swap);
  if (id.x >= wo || id.y >= ho) { return; }
  let i = id.y;
  let j = id.x;
  // 输出 [i, j] ← 存储 [rr, cc]（与 orient_array 的 numpy 写法一一对应）
  var rr = i; var cc = j;
  switch o {
    case 2u: { cc = w2 - 1u - j; }
    case 3u: { rr = h2 - 1u - i; cc = w2 - 1u - j; }
    case 4u: { rr = h2 - 1u - i; }
    case 5u: { rr = j; cc = i; }
    case 6u: { rr = h2 - 1u - j; cc = i; }
    case 7u: { rr = h2 - 1u - j; cc = w2 - 1u - i; }
    case 8u: { rr = j; cc = w2 - 1u - i; }
    default: {}
  }
  let y = u32(r.crop.x) + rr * 2u;
  let x = u32(r.crop.y) + cc * 2u;
  var sum = vec3f(0.0);
  var cnt = vec3f(0.0);
  for (var a = 0u; a < 2u; a++) {
    for (var b = 0u; b < 2u; b++) {
      let c = u32(r.phase[a * 2u + b]);
      sum[c] += sample(y + a, x + b);
      cnt[c] += 1.0;
    }
  }
  textureStore(outTex, vec2i(i32(j), i32(i)), vec4f(sum / cnt * r.wb.xyz, 0.0));
}
