// 颜色链：逐项对应 src/gxr_studio 里的 Python 实现（文件名:函数写在注释里），不另发明调整。
// 数值约定：链内颜色是 0..255 的浮点；Python 里落成 uint8 的地方，这里都做 floor(clamp(x)+0.5)。

struct P {
  gains: vec4f,     // render/tones.py channel_gains：R、G、B 乘数（无调整时为 1）
  flags: vec4f,     // x 有调子表；y 模式（0 配方、1 配方+显示类 LUT、2 Log LUT、3 相机 JPG、4 相机 JPG+显示类 LUT）；z apply_base 生效；w LUT 强度
  extra: vec4f,     // finish/pixels.py apply_extra：黑点、饱和度、自然饱和度、暗角
  dims: vec4f,      // 颜色层（未裁切、未拉直）宽、高
  bake: vec4f,      // render/chain.py ChromaBake：origin cb、origin cr、1/step、nx
  bake2: vec4f,     // ny
  lutA: vec4f,      // 输入空间 id、输出空间 id、边长 n、输入 range（1 = legal）
  lutDom0: vec4f,   // domain_min
  lutDom1: vec4f,   // domain_max
  lutRng: vec4f,    // range3 lo、hi；z 输出 range（1 = legal）
  inM0: vec4f, inM1: vec4f, inM2: vec4f,     // 进 LUT 前的 3×3 + 偏移
  outM0: vec4f, outM1: vec4f, outM2: vec4f,  // LUT 输出线性 → sRGB 线性
};
@group(0) @binding(0) var<uniform> p: P;
@group(0) @binding(1) var<storage, read> rec: array<f32>;   // pre(4096×3) | matrix(9) | offset(3) | gain(ny×nx×2)
@group(0) @binding(2) var<storage, read> tone: array<f32>;  // render/tones.py display_table（1021 点）
@group(0) @binding(3) var lut3: texture_3d<f32>;             // .cube 3D 表，x=R、y=G、z=B

const MAT_AT: u32 = 12288u;
const OFF_AT: u32 = 12297u;
const GAIN_AT: u32 = 12300u;

fn q8(v: vec3f) -> vec3f { return floor(clamp(v, vec3f(0.0), vec3f(255.0)) + 0.5); }

// render/tones.py apply_table
fn applyTable(v: vec3f) -> vec3f {
  var o: vec3f;
  for (var c = 0u; c < 3u; c++) {
    let pos = clamp(v[c], 0.0, 255.0) * 4.0;
    let i = min(u32(pos), 1019u);
    let lo = tone[i];
    o[c] = lo + (tone[i + 1u] - lo) * (pos - f32(i));
  }
  return o;
}

// render/chain.py _chroma_chunk + quantize
fn recipeChain(codesIn: vec3f, gains: vec3f, useTable: bool) -> vec3f {
  let x = clamp(codesIn * gains, vec3f(0.0), vec3f(4095.0));
  var pc: vec3f;
  for (var c = 0u; c < 3u; c++) {
    let i = min(u32(x[c]), 4094u);
    let v0 = rec[i * 3u + c];
    pc[c] = v0 + (rec[(i + 1u) * 3u + c] - v0) * (x[c] - f32(i));
  }
  let y = rec[MAT_AT] * pc.x + rec[MAT_AT + 1u] * pc.y + rec[MAT_AT + 2u] * pc.z + rec[OFF_AT];
  let cb = rec[MAT_AT + 3u] * pc.x + rec[MAT_AT + 4u] * pc.y + rec[MAT_AT + 5u] * pc.z + rec[OFF_AT + 1u];
  let cr = rec[MAT_AT + 6u] * pc.x + rec[MAT_AT + 7u] * pc.y + rec[MAT_AT + 8u] * pc.z + rec[OFF_AT + 2u];
  let nx = i32(p.bake.w);
  let ny = i32(p.bake2.x);
  let u = (cb - p.bake.x) * p.bake.z;
  let v = (cr - p.bake.y) * p.bake.z;
  let iu = clamp(i32(clamp(u, -1e6, 1e6)), 0, nx - 2);   // numpy astype(int32)：向零取整，再夹
  let iv = clamp(i32(clamp(v, -1e6, 1e6)), 0, ny - 2);
  let fu = u - f32(iu);
  let fv = v - f32(iv);
  let at = u32(iv * nx + iu);
  let un = u32(nx);
  let a00 = vec2f(rec[GAIN_AT + at * 2u], rec[GAIN_AT + at * 2u + 1u]);
  let a01 = vec2f(rec[GAIN_AT + (at + 1u) * 2u], rec[GAIN_AT + (at + 1u) * 2u + 1u]);
  let a10 = vec2f(rec[GAIN_AT + (at + un) * 2u], rec[GAIN_AT + (at + un) * 2u + 1u]);
  let a11 = vec2f(rec[GAIN_AT + (at + un + 1u) * 2u], rec[GAIN_AT + (at + un + 1u) * 2u + 1u]);
  let gg = (a00 * (1.0 - fu) + a01 * fu) * (1.0 - fv) + (a10 * (1.0 - fu) + a11 * fu) * fv;
  let cbo = cb * gg.x - cr * gg.y;
  let cro = cb * gg.y + cr * gg.x;
  var rgb = clamp(vec3f(y + 1.402 * cro, y - 0.344136 * cbo - 0.714136 * cro, y + 1.772 * cbo), vec3f(0.0), vec3f(255.0));
  if (useTable) { rgb = applyTable(rgb); }
  return q8(rgb);
}

// lut/spaces.py encode / decode：srgb=0 rec709=1 gamma24=2 llog=3 linear=4 flog=5 flog2=6 vlog=7 slog3=8 applelog=9
fn log10f(x: f32) -> f32 { return log2(max(x, 1e-30)) * 0.30102999566398120; }
fn enc1(x: f32, s: u32) -> f32 {
  switch s {
    case 0u: { if (x <= 0.0031308) { return 12.92 * x; } return 1.055 * pow(max(x, 0.0), 1.0 / 2.4) - 0.055; }
    case 1u: { if (x < 0.018) { return 4.5 * x; } return 1.099 * pow(max(x, 0.0), 0.45) - 0.099; }
    case 2u: { return sign(x) * pow(abs(x), 1.0 / 2.4); }
    case 3u: { if (x <= 0.006) { return 8.0 * x + 0.09; } return 0.27 * log10f(1.3 * x + 0.0115) + 0.6; }
    case 5u: { if (x < 0.00089) { return 8.735631 * x + 0.092864; } return 0.344676 * log10f(0.555556 * x + 0.009468) + 0.790453; }
    case 6u: { if (x < 0.000889) { return 8.799461 * x + 0.092864; } return 0.245281 * log10f(5.555556 * x + 0.064829) + 0.384316; }
    case 7u: { if (x < 0.01) { return 5.6 * x + 0.125; } return 0.241514 * log10f(x + 0.00873) + 0.598206; }
    case 8u: { if (x < 0.01125) { return (95.0 + x * (171.2102946929 - 95.0) / 0.01125) / 1023.0; } return (420.0 + 261.5 * log10f((x + 0.01) / 0.19)) / 1023.0; }
    case 9u: { if (x >= 0.01) { return 0.08550479 * log2(max(x + 0.00964052, 1e-30)) + 0.69336945; } return 47.28711236 * pow(max(x + 0.05641088, 0.0), 2.0); }
    default: { return x; }
  }
}
fn dec1(x: f32, s: u32) -> f32 {
  switch s {
    case 0u: { if (x <= 0.04045) { return x / 12.92; } return pow(max((x + 0.055) / 1.055, 0.0), 2.4); }
    case 1u: { if (x < 0.081) { return x / 4.5; } return pow(max((x + 0.099) / 1.099, 0.0), 1.0 / 0.45); }
    case 2u: { return sign(x) * pow(abs(x), 2.4); }
    default: { return x; }
  }
}
fn enc3(v: vec3f, s: u32) -> vec3f { return vec3f(enc1(v.x, s), enc1(v.y, s), enc1(v.z, s)); }
fn dec3(v: vec3f, s: u32) -> vec3f { return vec3f(dec1(v.x, s), dec1(v.y, s), dec1(v.z, s)); }
fn mat3x(v: vec3f, a: vec4f, b: vec4f, c: vec4f) -> vec3f {
  return vec3f(dot(a.xyz, v) + a.w, dot(b.xyz, v) + b.w, dot(c.xyz, v) + c.w);
}

// lut/cube.py Cube.tetrahedral：小数部分从大到小（相等时通道序号小的在前）
fn lutAt(c: vec3i) -> vec3f { return textureLoad(lut3, c, 0).xyz; }
fn tetra(x: vec3f) -> vec3f {
  let n = i32(p.lutA.z);
  let pos = x * f32(n - 1);
  let base = min(vec3i(pos), vec3i(n - 2));
  let f = pos - vec3f(base);
  var o0 = 0; var o1 = 1; var o2 = 2;
  if (f[o1] > f[o0]) { let t = o0; o0 = o1; o1 = t; }
  if (f[o2] > f[o1]) { let t = o1; o1 = o2; o2 = t; }
  if (f[o1] > f[o0]) { let t = o0; o0 = o1; o1 = t; }
  var b = base;
  let v0 = lutAt(b);
  b[o0] += 1; let v1 = lutAt(b);
  b[o1] += 1; let v2 = lutAt(b);
  b[o2] += 1; let v3 = lutAt(b);
  return v0 + f[o0] * (v1 - v0) + f[o1] * (v2 - v1) + f[o2] * (v3 - v2);
}

// lut/spaces.py Target.render（+ lut/photo.py _apply 的量化在调用处）
fn lutRender(lin_in: vec3f) -> vec3f {
  var x = enc3(lin_in, u32(p.lutA.x));
  if (p.lutA.w > 0.5) { x = (64.0 + 876.0 * x) / 1023.0; }
  x = (x - p.lutDom0.xyz) / (p.lutDom1.xyz - p.lutDom0.xyz);
  x = (x - vec3f(p.lutRng.x)) / (p.lutRng.y - p.lutRng.x);
  var o = tetra(clamp(x, vec3f(0.0), vec3f(1.0)));
  if (p.lutRng.z > 0.5) { o = (1023.0 * o - 64.0) / 876.0; }
  let srgb = enc3(mat3x(dec3(o, u32(p.lutA.y)), p.outM0, p.outM1, p.outM2), 0u);
  return clamp(srgb, vec3f(0.0), vec3f(1.0)) * 255.0;
}

// finish/pixels.py _srgb_to_linear / apply_base（LUT 后和相机 JPG 上的调整）
fn srgbToLin(u: vec3f) -> vec3f {
  let x = u / 255.0;
  return select(pow((x + 0.055) / 1.055, vec3f(2.4)), x / 12.92, x <= vec3f(0.04045));
}
fn applyBase(u: vec3f) -> vec3f {
  let x = max(srgbToLin(u) * p.gains.xyz, vec3f(0.0));
  let e = select(1.055 * pow(x, vec3f(1.0 / 2.4)) - 0.055, x * 12.92, x <= vec3f(0.0031308));
  var o = floor(clamp(e * 255.0 + 0.5, vec3f(0.0), vec3f(255.0)));
  if (p.flags.x > 0.5) { o = floor(clamp(applyTable(o) + 0.5, vec3f(0.0), vec3f(255.0))); }
  return o;
}

// finish/pixels.py apply_extra：黑点 → 饱和度 → 自然饱和度 → 暗角，每步落成 uint8
fn applyExtra(u0: vec3f, pix: vec2f, wh: vec2f) -> vec3f {
  var u = u0;
  let bp = p.extra.x;
  if (bp != 0.0) {
    var y = u / 255.0;
    let k = abs(bp) / 100.0 * 0.35;
    if (bp > 0.0) { y = clamp((y - k) / max(1e-4, 1.0 - k), vec3f(0.0), vec3f(1.0)); } else { y = y * (1.0 - k) + k; }
    u = floor(clamp(y * 255.0 + 0.5, vec3f(0.0), vec3f(255.0)));
  }
  if (p.extra.y != 0.0) {
    let gray = u.x * 0.2126 + u.y * 0.7152 + u.z * 0.0722;
    u = floor(clamp(gray + (u - gray) * (1.0 + p.extra.y / 100.0) + 0.5, vec3f(0.0), vec3f(255.0)));
  }
  if (p.extra.z != 0.0) {
    let gray = u.x * 0.2126 + u.y * 0.7152 + u.z * 0.0722;
    let dull = 1.0 - clamp((max(u.x, max(u.y, u.z)) - min(u.x, min(u.y, u.z))) / 255.0, 0.0, 1.0);
    u = floor(clamp(gray + (u - gray) * (1.0 + p.extra.z / 100.0 * dull) + 0.5, vec3f(0.0), vec3f(255.0)));
  }
  if (p.extra.w != 0.0) {
    let hx = max((wh.x - 1.0) / 2.0, 1.0);
    let hy = max((wh.y - 1.0) / 2.0, 1.0);
    let r = clamp(length(vec2f((pix.x - (wh.x - 1.0) / 2.0) / hx, (pix.y - (wh.y - 1.0) / 2.0) / hy)), 0.0, 1.0);
    let g = 1.0 + p.extra.w / 100.0 * 0.65 * (r * r * (3.0 - 2.0 * r));
    u = floor(clamp(u * g + 0.5, vec3f(0.0), vec3f(255.0)));
  }
  return u;
}

// 一个像素的整条颜色链。codes：白平衡码值（RAW 模式）；jpg：相机 JPG 的 0..255 整数（JPG 模式）
fn colorChain(codes: vec3f, jpg: vec3f, pix: vec2f) -> vec3f {
  let mode = u32(p.flags.y);
  var u: vec3f;
  if (mode == 0u) {
    u = recipeChain(codes, p.gains.xyz, p.flags.x > 0.5);
  } else if (mode == 3u) {
    u = jpg;
    if (p.flags.z > 0.5) { u = applyBase(u); }
  } else {
    var base: vec3f;
    var lin_in: vec3f;
    if (mode == 2u) {
      base = q8(enc3(clamp(codes / 4095.0, vec3f(0.0), vec3f(1.0)), 0u) * 255.0);
      lin_in = mat3x(codes, p.inM0, p.inM1, p.inM2);          // 码值 → 场景 XYZ → LUT 输入色域
    } else {
      base = select(recipeChain(codes, vec3f(1.0), false), jpg, mode == 4u);
      lin_in = mat3x(srgbToLin(base), p.inM0, p.inM1, p.inM2);
    }
    u = q8(mix(base, lutRender(lin_in), p.flags.w));
    if (p.flags.z > 0.5) { u = applyBase(u); }
  }
  return applyExtra(u, pix, p.dims.xy);
}
