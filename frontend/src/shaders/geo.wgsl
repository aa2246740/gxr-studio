// 第二遍：纠正（拉直，或透视 / 畸变）→ 90° 旋转 / 水平翻转 → 裁切框，1:1 写进成片纹理（读回和 Python 逐像素比较的就是它）。
// finish/geometry.py：_straighten_coords、_expanded_coords + _sample（双线性后 +0.5 取整，FILL = 1B1C1F），_rot_flip（np.rot90 逆时针、fliplr）。
struct G {
  src: vec4f,   // 颜色层宽、高；旋转后画布宽、高
  geo: vec4f,   // cos、sin、90° 次数（0..3）、翻转
  box: vec4f,   // 裁切框（旋转后画布的像素）：x、y、w、h
  lens: vec4f,  // 径向系数、垂直系数、水平系数、半对角线（_lens）
  org: vec4f,   // 透视 / 畸变的画布原点 x、y，是否透视 / 畸变（1 = 是）
};
@group(0) @binding(0) var<uniform> g: G;
@group(0) @binding(1) var colorTex: texture_2d<f32>;

@vertex fn vs(@builtin(vertex_index) i: u32) -> @builtin(position) vec4f {
  let xy = vec2f(f32((i << 1u) & 2u), f32(i & 2u));
  return vec4f(xy * 2.0 - 1.0, 0.0, 1.0);
}

fn texel(x: i32, y: i32) -> vec3f { return textureLoad(colorTex, vec2i(x, y), 0).xyz * 255.0; }

// _sample：取样位置 (sx, sy) 在颜色层里双线性取样；超出有效区（边缘留 1e-3 像素）是 FILL
fn sampleAt(sx: f32, sy: f32) -> vec4f {
  let W = g.src.x;
  let H = g.src.y;
  // f32 坐标：落在有效区边界 1e-4 像素以内的个别像素，可能与 Python（float64）判断不同。NaN（分母为零）也落到这里
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

// 透视 / 畸变：输出画布上的点 (X, Y) → 颜色层里的取样位置（_expanded_coords：先逆透视，再用 8 步牛顿法逆径向畸变）
fn expanded(X: f32, Y: f32) -> vec4f {
  let W = g.src.x;
  let H = g.src.y;
  let ox = g.org.x + X + 0.5;
  let oy = g.org.y + Y + 0.5;
  let rx = g.geo.x * ox + g.geo.y * oy;
  let ry = -g.geo.y * ox + g.geo.x * oy;
  var nx = rx / (W / 2.0);
  var ny = ry / (H / 2.0);
  let denom = 1.0 - g.lens.z * nx - g.lens.y * ny;
  nx = nx / denom;
  ny = ny / denom;
  let cx = nx * (W / 2.0);
  let cy = ny * (H / 2.0);
  let rOut = length(vec2f(cx, cy)) / g.lens.w;
  var rSrc = rOut;
  if (abs(g.lens.x) >= 1e-8) {
    for (var i = 0; i < 8; i++) {
      let f = g.lens.x * rSrc * rSrc * rSrc + rSrc - rOut;
      let df = 3.0 * g.lens.x * rSrc * rSrc + 1.0;
      rSrc = rSrc - f / select(df, 1.0, abs(df) < 1e-6);
    }
  }
  let scale = select(1.0, rSrc / rOut, rOut > 1e-8);
  return sampleAt(cx * scale + (W - 1.0) / 2.0, cy * scale + (H - 1.0) / 2.0);
}

// 拉直：输出画布上的点 (X, Y) → 在颜色层里取样
fn straightened(X: f32, Y: f32) -> vec4f {
  let W = g.src.x;
  let H = g.src.y;
  if (g.org.z > 0.5) { return expanded(X, Y); }
  if (g.geo.x == 1.0 && g.geo.y == 0.0) {
    return vec4f(texel(i32(X), i32(Y)), 1.0);
  }
  let dx = X - (W - 1.0) / 2.0;
  let dy = Y - (H - 1.0) / 2.0;
  return sampleAt(g.geo.x * dx + g.geo.y * dy + (W - 1.0) / 2.0, -g.geo.y * dx + g.geo.x * dy + (H - 1.0) / 2.0);
}

@fragment fn fs(@builtin(position) pos: vec4f) -> @location(0) vec4f {
  // 成片像素 → 旋转后画布的整数像素
  var cx = g.box.x + floor(pos.x);
  let cy = g.box.y + floor(pos.y);
  if (g.geo.w > 0.5) { cx = g.src.z - 1.0 - cx; }
  // np.rot90(a, k) 的逆映射：输出 [i, j] ← 输入（W、H 是旋转之前画布的宽高；没有透视、畸变时就是颜色层的宽高）
  let k = u32(g.geo.z);
  let odd = (k & 1u) == 1u;
  let W = select(g.src.z, g.src.w, odd);
  let H = select(g.src.w, g.src.z, odd);
  var X = cx;
  var Y = cy;
  if (k == 1u) { X = W - 1.0 - cy; Y = cx; }
  else if (k == 2u) { X = W - 1.0 - cx; Y = H - 1.0 - cy; }
  else if (k == 3u) { X = cy; Y = H - 1.0 - cx; }
  return vec4f(straightened(X, Y).xyz / 255.0, 1.0);
}
