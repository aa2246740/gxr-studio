// 第三遍：成片纹理 → 画布。按显示比例缩放；缩小时 2×2 超采样，避免锯齿。只影响显示，不影响读回。
struct V {
  map: vec4f,   // 画布像素 → 成片像素：缩放、偏移 x、偏移 y、背景灰度
  size: vec4f,  // 成片宽、高
};
@group(0) @binding(0) var<uniform> v: V;
@group(0) @binding(1) var finalTex: texture_2d<f32>;
@group(0) @binding(2) var smp: sampler;

@vertex fn vs(@builtin(vertex_index) i: u32) -> @builtin(position) vec4f {
  let xy = vec2f(f32((i << 1u) & 2u), f32(i & 2u));
  return vec4f(xy * 2.0 - 1.0, 0.0, 1.0);
}

@fragment fn fs(@builtin(position) pos: vec4f) -> @location(0) vec4f {
  let s = v.map.x;
  let q = (pos.xy - v.map.yz) * s;  // 成片像素坐标（左上角为 0）
  if (q.x < 0.0 || q.y < 0.0 || q.x > v.size.x || q.y > v.size.y) { return vec4f(vec3f(v.map.w), 1.0); }
  let inv = 1.0 / v.size.xy;
  var c: vec3f;
  if (s > 1.25) {
    let d = s * 0.25;
    c = (textureSampleLevel(finalTex, smp, (q + vec2f(-d, -d)) * inv, 0.0).xyz
       + textureSampleLevel(finalTex, smp, (q + vec2f(d, -d)) * inv, 0.0).xyz
       + textureSampleLevel(finalTex, smp, (q + vec2f(-d, d)) * inv, 0.0).xyz
       + textureSampleLevel(finalTex, smp, (q + vec2f(d, d)) * inv, 0.0).xyz) * 0.25;
  } else {
    c = textureSampleLevel(finalTex, smp, q * inv, 0.0).xyz;
  }
  return vec4f(c, 1.0);
}
