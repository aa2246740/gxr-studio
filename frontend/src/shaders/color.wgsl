// 第一遍：源图（RAW 码值或相机 JPG）→ 颜色层，未拉直、未裁切。rgba8unorm 存整数码值。
@group(1) @binding(0) var proxy: texture_2d<f32>;
@group(1) @binding(1) var jpgTex: texture_2d<f32>;

@vertex fn vs(@builtin(vertex_index) i: u32) -> @builtin(position) vec4f {
  let xy = vec2f(f32((i << 1u) & 2u), f32(i & 2u));
  return vec4f(xy * 2.0 - 1.0, 0.0, 1.0);
}

@fragment fn fs(@builtin(position) pos: vec4f) -> @location(0) vec4f {
  let pix = vec2i(pos.xy);
  let mode = u32(p.flags.y);
  var codes = vec3f(0.0);
  var jpg = vec3f(0.0);
  if (mode == 3u || mode == 4u) {
    jpg = floor(textureLoad(jpgTex, pix, 0).xyz * 255.0 + 0.5);
  } else {
    codes = textureLoad(proxy, pix, 0).xyz;
  }
  return vec4f(colorChain(codes, jpg, vec2f(pix)) / 255.0, 1.0);
}
