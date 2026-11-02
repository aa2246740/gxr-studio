// 第二遍：拉直 → 90° 旋转 / 水平翻转 → 裁切框，1:1 写进成片纹理（读回和 Python 逐像素比较的就是它）。
// finish/geometry.py：_straighten_coords + _sample（双线性后 +0.5 取整，FILL = 1B1C1F），_rot_flip（np.rot90 逆时针、fliplr）。
struct G {
  src: vec4f,   // 颜色层宽、高；旋转后画布宽、高
  geo: vec4f,   // cos、sin、90° 次数（0..3）、翻转
  box: vec4f,   // 裁切框（旋转后画布的像素）：x、y、w、h
  pad: vec4f,
};
@group(0) @binding(0) var<uniform> g: G;
@group(0) @binding(1) var colorTex: texture_2d<f32>;

@vertex fn vs(@builtin(vertex_index) i: u32) -> @builtin(position) vec4f {
  let xy = vec2f(f32((i << 1u) & 2u), f32(i & 2u));
  return vec4f(xy * 2.0 - 1.0, 0.0, 1.0);
}

fn texel(x: i32, y: i32) -> vec3f { return textureLoad(colorTex, vec2i(x, y), 0).xyz * 255.0; }

// 拉直：输出画布上的点 (X, Y) → 在颜色层里双线性取样（与 _sample 相同的有效区和取整）
fn straightened(X: f32, Y: f32) -> vec4f {
  let W = g.src.x;
  let H = g.src.y;
  if (g.geo.x == 1.0 && g.geo.y == 0.0) {
    return vec4f(texel(i32(X), i32(Y)), 1.0);
  }
  // f32 坐标：落在有效区边界 1e-4 像素以内的个别像素，可能与 Python（float64）判断不同
  let dx = X - (W - 1.0) / 2.0;
  let dy = Y - (H - 1.0) / 2.0;
  let sx = g.geo.x * dx + g.geo.y * dy + (W - 1.0) / 2.0;
  let sy = -g.geo.y * dx + g.geo.x * dy + (H - 1.0) / 2.0;
  if (!(sx >= -1e-3 && sy >= -1e-3 && sx <= W - 1.0 + 1e-3 && sy <= H - 1.0 + 1e-3)) {
    return vec4f(27.0, 28.0, 31.0, 1.0);
  }
  let fx = floor(sx);
  let fy = floor(sy);
  let x0 = clamp(i32(fx), 0, i32(W) - 1);
  let y0 = clamp(i32(fy), 0, i32(H) - 1);
  let x1 = min(x0 + 1, i32(W) - 1);
  let y1 = min(y0 + 1, i32(H) - 1);
  let tx = sx - fx;
  let ty = sy - fy;
  let s = texel(x0, y0) * ((1.0 - tx) * (1.0 - ty)) + texel(x1, y0) * (tx * (1.0 - ty)) + texel(x0, y1) * ((1.0 - tx) * ty) + texel(x1, y1) * (tx * ty);
  return vec4f(floor(clamp(s + 0.5, vec3f(0.0), vec3f(255.0))), 1.0);
}

@fragment fn fs(@builtin(position) pos: vec4f) -> @location(0) vec4f {
  // 成片像素 → 旋转后画布的整数像素
  var cx = g.box.x + floor(pos.x);
  let cy = g.box.y + floor(pos.y);
  if (g.geo.w > 0.5) { cx = g.src.z - 1.0 - cx; }
  // np.rot90(a, k) 的逆映射：输出 [i, j] ← 输入（W、H 是颜色层的宽高）
  let W = g.src.x;
  let H = g.src.y;
  var X = cx;
  var Y = cy;
  let k = u32(g.geo.z);
  if (k == 1u) { X = W - 1.0 - cy; Y = cx; }
  else if (k == 2u) { X = W - 1.0 - cx; Y = H - 1.0 - cy; }
  else if (k == 3u) { X = cy; Y = H - 1.0 - cx; }
  return vec4f(straightened(X, Y).xyz / 255.0, 1.0);
}
