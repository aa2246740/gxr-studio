// 半格一对的第一遍：两张传感器坐标半格条带纹理（tex1/tex2）按共享方向和接缝位置实时合成到转正后的颜色层。
// 与 common.wgsl 拼接后使用；单张照片仍走 color.wgsl，不受影响。
struct PairP {
  geo: vec4f,   // sensor_w (w2), sensor_h (h2), half_w, right_x0
  cfg: vec4f,   // orientation (1/3/6/8), first_left (1 = left, 0 = right), sameBake (1 = 共用 rec, 0 = rec2), 0
  bake: vec4f,  // 第二半烘焙表：origin cb, origin cr, 1/step, nx
  bake2: vec4f, // 第二半烘焙表：ny, 0, 0, 0
  inM0: vec4f,  // 第二半 Log LUT 进表 3×3 + 偏移
  inM1: vec4f,
  inM2: vec4f,
};
@group(1) @binding(0) var<uniform> pp: PairP;
@group(1) @binding(1) var tex1: texture_2d<f32>;
@group(1) @binding(2) var tex2: texture_2d<f32>;
@group(1) @binding(3) var<storage, read> rec2: array<f32>;

// recipeChain2 由 pair_wgsl.ts 从 common.wgsl 的 recipeChain 自动派生（仅重命名 rec→rec2、p.bake→pp.bake），不手工抄一份

@vertex fn vs(@builtin(vertex_index) i: u32) -> @builtin(position) vec4f {
  let xy = vec2f(f32((i << 1u) & 2u), f32(i & 2u));
  return vec4f(xy * 2.0 - 1.0, 0.0, 1.0);
}

@fragment fn fs(@builtin(position) pos: vec4f) -> @location(0) vec4f {
  let pix = vec2i(pos.xy);
  let j = u32(pix.x);
  let i = u32(pix.y);
  let w2 = u32(pp.geo.x);
  let h2 = u32(pp.geo.y);
  let ori = u32(pp.cfg.x);
  var rr = i;
  var cc = j;
  switch ori {
    case 3u: { rr = h2 - 1u - i; cc = w2 - 1u - j; }
    case 6u: { rr = h2 - 1u - j; cc = i; }
    case 8u: { rr = j; cc = w2 - 1u - i; }
    default: {}
  }
  let halfW = u32(pp.geo.z);
  let rightX0 = u32(pp.geo.w);
  let firstLeft = pp.cfg.y > 0.5;
  var isSeam = true;
  var useSecond = false;
  var lx = 0u;
  if (cc < halfW) {
    isSeam = false;
    useSecond = !firstLeft;
    lx = cc;
  } else if (cc >= rightX0) {
    isSeam = false;
    useSecond = firstLeft;
    lx = cc - rightX0;
  }
  let mode = u32(p.flags.y);
  let sameBake = pp.cfg.z > 0.5;
  var u = vec3f(0.0);
  if (mode == 0u) {
    if (!isSeam) {
      var codes: vec3f;
      if (!useSecond) {
        codes = textureLoad(tex1, vec2i(i32(lx), i32(rr)), 0).xyz;
      } else {
        codes = textureLoad(tex2, vec2i(i32(lx), i32(rr)), 0).xyz;
      }
      if (!useSecond || sameBake) {
        u = recipeChain(codes, p.gains.xyz, p.flags.x > 0.5);
      } else {
        u = recipeChain2(codes, p.gains.xyz, p.flags.x > 0.5);
      }
    }
  } else if (mode == 1u) {
    var base = vec3f(0.0);
    if (!isSeam) {
      var codes: vec3f;
      if (!useSecond) {
        codes = textureLoad(tex1, vec2i(i32(lx), i32(rr)), 0).xyz;
      } else {
        codes = textureLoad(tex2, vec2i(i32(lx), i32(rr)), 0).xyz;
      }
      if (!useSecond || sameBake) {
        base = recipeChain(codes, vec3f(1.0), false);
      } else {
        base = recipeChain2(codes, vec3f(1.0), false);
      }
    }
    let looked = floor(textureLoad(lut8, vec3i(base), 0).xyz * 255.0 + 0.5);
    u = q8(mix(base, looked, p.flags.w));
    if (p.flags.z > 0.5) { u = applyBase(u); }
  } else {
    if (!isSeam) {
      var codes: vec3f;
      var linIn: vec3f;
      if (!useSecond) {
        codes = textureLoad(tex1, vec2i(i32(lx), i32(rr)), 0).xyz;
        linIn = mat3x(codes, p.inM0, p.inM1, p.inM2);
      } else {
        codes = textureLoad(tex2, vec2i(i32(lx), i32(rr)), 0).xyz;
        linIn = mat3x(codes, pp.inM0, pp.inM1, pp.inM2);
      }
      let base = q8(enc3(clamp(codes / 4095.0, vec3f(0.0), vec3f(1.0)), 0u) * 255.0);
      let looked = lutRender(linIn);
      u = q8(mix(base, looked, p.flags.w));
    }
    if (p.flags.z > 0.5) { u = applyBase(u); }
  }
  return vec4f(applyExtra(u, vec2f(pix), p.dims.xy) / 255.0, 1.0);
}
