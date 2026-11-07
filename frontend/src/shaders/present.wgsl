// 第三遍：成片纹理 → 画布。按显示比例缩放；缩小时 2×2 超采样，避免锯齿。只影响显示，不影响读回。
// 溢出提示（J）：把暗部压死的点盖成蓝色、高光溢出的点盖成红色。判定与 histogram_math.js 的 isCrushed / isBlown 相同
// （码值 ≤ clip.x 或 ≥ clip.y），颜色与 SHADOW_RGB / HIGHLIGHT_RGB 相同。
struct V {
  map: vec4f,   // 画布像素 → 成片像素：缩放、偏移 x、偏移 y、背景灰度
  size: vec4f,  // 成片宽、高
  clip: vec4f,  // 暗部上限 + 0.5、高光下限 − 0.5（码值）、盖暗部（1）、盖高光（1）
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
  let code = c * 255.0;
  if (v.clip.z > 0.5 && max(code.x, max(code.y, code.z)) < v.clip.x) { return vec4f(40.0 / 255.0, 110.0 / 255.0, 1.0, 1.0); }
  if (v.clip.w > 0.5 && min(code.x, min(code.y, code.z)) > v.clip.y) { return vec4f(1.0, 50.0 / 255.0, 40.0 / 255.0, 1.0); }
  return vec4f(c, 1.0);
}
