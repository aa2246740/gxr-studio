// HDR 显示遍。只在 setOutput({hdr:true}) 之后使用。SDR 的 present.wgsl 不走这里。
// 成片纹理仍是 8 位 sRGB 码值。这里先解成线性，再按 ISO 21496-1 / Ultra HDR 乘增益：
//   shaped = pow(G, 1/gamma)
//   log2Gain = mix(gainMin, gainMax, shaped)
//   factor = exp2(log2Gain * weight)
//   hdr = (linear + offsetSdr) * factor - offsetHdr
// weight 由 CPU 用显示 headroom（没有就用 cap_max）算好。
// 画布是 rgba16float + colorSpace 'srgb'：合成器把写进去的值当扩展 sRGB（sRGB 传递函数，延伸到 1 以上）解码，
// 和 SDR 画布一样。所以输出前再按 sRGB 编码回去：增益为 1 的地方和 SDR 预览一模一样，编码值 1.0 仍是 SDR 白，
// 大于 1 的是更亮的高光。舞台灰和溢出提示的颜色也直接写 SDR 的码值。
struct V {
  map: vec4f,
  size: vec4f,
  clip: vec4f,
  gain: vec4f, // gainMin, gainMax, 1/gamma, weight
  off: vec4f,  // offsetSdr, offsetHdr
};
@group(0) @binding(0) var<uniform> v: V;
@group(0) @binding(1) var finalTex: texture_2d<f32>;
@group(0) @binding(2) var smp: sampler;
@group(0) @binding(3) var gainTex: texture_2d<f32>;

fn srgb_eotf(c: f32) -> f32 {
  if (c <= 0.04045) { return c / 12.92; }
  return pow((c + 0.055) / 1.055, 2.4);
}

fn srgb_oetf(l: f32) -> f32 {
  if (l <= 0.0031308) { return l * 12.92; }
  return 1.055 * pow(l, 1.0 / 2.4) - 0.055;
}

@vertex fn vs(@builtin(vertex_index) i: u32) -> @builtin(position) vec4f {
  let xy = vec2f(f32((i << 1u) & 2u), f32(i & 2u));
  return vec4f(xy * 2.0 - 1.0, 0.0, 1.0);
}

@fragment fn fs(@builtin(position) pos: vec4f) -> @location(0) vec4f {
  let sc = vec2f(v.map.x, v.size.z);
  let s = max(sc.x, sc.y);
  let q = (pos.xy - v.map.yz) * sc;
  if (q.x < 0.0 || q.y < 0.0 || q.x > v.size.x || q.y > v.size.y) {
    return vec4f(vec3f(v.map.w), 1.0);
  }
  let inv = 1.0 / v.size.xy;
  var c: vec3f;
  if (s > 1.25) {
    let d = sc * 0.25;
    c = (textureSampleLevel(finalTex, smp, (q + vec2f(-d.x, -d.y)) * inv, 0.0).xyz
       + textureSampleLevel(finalTex, smp, (q + vec2f(d.x, -d.y)) * inv, 0.0).xyz
       + textureSampleLevel(finalTex, smp, (q + vec2f(-d.x, d.y)) * inv, 0.0).xyz
       + textureSampleLevel(finalTex, smp, (q + d) * inv, 0.0).xyz) * 0.25;
  } else {
    c = textureSampleLevel(finalTex, smp, q * inv, 0.0).xyz;
  }
  let code = c * 255.0;
  if (v.clip.z > 0.5 && max(code.x, max(code.y, code.z)) < v.clip.x) { return vec4f(40.0 / 255.0, 110.0 / 255.0, 1.0, 1.0); }
  if (v.clip.w > 0.5 && min(code.x, min(code.y, code.z)) > v.clip.y) { return vec4f(1.0, 50.0 / 255.0, 40.0 / 255.0, 1.0); }
  let uv = q * inv;
  let g = textureSampleLevel(gainTex, smp, uv, 0.0).r;
  let shaped = pow(max(g, 0.0), v.gain.z);
  let log2_gain = mix(v.gain.x, v.gain.y, shaped);
  let factor = exp2(log2_gain * v.gain.w);
  let lin = vec3f(srgb_eotf(c.x), srgb_eotf(c.y), srgb_eotf(c.z));
  let hdr = max((lin + v.off.x) * factor - v.off.y, vec3f(0.0));
  return vec4f(srgb_oetf(hdr.x), srgb_oetf(hdr.y), srgb_oetf(hdr.z), 1.0);
}
