// src/shaders/color.wgsl?raw
var e = .35, t = .25, n = .13, r = 1021;
function i(n) {
	let r = n.exposure_ev ?? 0, i = (n.wb?.warm ?? 0) / 100, a = (n.wb?.tint ?? 0) / 100;
	return r === 0 && i === 0 && a === 0 ? null : [
		2 ** (r + e * i + t * a),
		2 ** r,
		2 ** (r - e * i + t * a)
	];
}
function a(e) {
	return !e.contrast && !e.highlight && !e.shadow;
}
function o(e, t) {
	let r = e;
	if (t.contrast) {
		let e = t.contrast / 100;
		r += e * (r * r * (3 - 2 * r) - r);
	}
	return t.highlight && (r += t.highlight / 100 * n * 6.75 * r * r * (1 - r)), t.shadow && (r += t.shadow / 100 * n * 6.75 * r * (1 - r) ** 2), Math.min(1, Math.max(0, r));
}
function s(e, t, n, r) {
	let i = ((2 * e + t) * n - e * r) / (e + t);
	return Math.sign(i) === Math.sign(n) ? Math.sign(n) !== Math.sign(r) && Math.abs(i) > Math.abs(3 * n) && (i = 3 * n) : i = 0, i;
}
function c(e, t) {
	let n = e.length, r = [], i = [];
	for (let a = 0; a < n - 1; a++) r.push(e[a + 1] - e[a]), i.push((t[a + 1] - t[a]) / r[a]);
	let a = Array(n).fill(0);
	if (n === 2) a[0] = i[0], a[1] = i[0];
	else {
		for (let e = 1; e < n - 1; e++) {
			if (i[e - 1] * i[e] <= 0) continue;
			let t = 2 * r[e] + r[e - 1], n = r[e] + 2 * r[e - 1];
			a[e] = (t + n) / (t / i[e - 1] + n / i[e]);
		}
		a[0] = s(r[0], r[1], i[0], i[1]), a[n - 1] = s(r[n - 2], r[n - 3], i[n - 2], i[n - 3]);
	}
	return (i) => {
		let o = 0;
		for (; o < n - 2 && i > e[o + 1];) o++;
		let s = (i - e[o]) / r[o], c = s * s, l = c * s;
		return (2 * l - 3 * c + 1) * t[o] + (l - 2 * c + s) * r[o] * a[o] + (-2 * l + 3 * c) * t[o + 1] + (l - c) * r[o] * a[o + 1];
	};
}
function l(e) {
	return !e || e.length < 2 || e.every(([e, t]) => e === t) ? null : e;
}
function u(e, t) {
	let n = l(t);
	if (a(e) && !n) return null;
	let i = new Float32Array(r), s = n ? n.map((e) => e[0]) : [], u = n ? c(s, n.map((e) => e[1])) : null;
	for (let t = 0; t < r; t++) {
		let n = o(255 * t / 1020 / 255, e) * 255;
		u && (n = Math.min(255, Math.max(0, u(Math.min(s[s.length - 1], Math.max(s[0], n)))))), i[t] = n;
	}
	return i;
}
// src/engine.ts
var d = 42 / 255;
function f(e, t) {
	return e.map((e) => [
		0,
		1,
		2
	].map((n) => e[0] * t[0][n] + e[1] * t[1][n] + e[2] * t[2][n]));
}
var p = class e {
	device;
	info;
	sources = /* @__PURE__ */ new Map();
	bakes = /* @__PURE__ */ new Map();
	luts = /* @__PURE__ */ new Map();
	canvas = null;
	context = null;
	format;
	colorPipe;
	geoPipe;
	presentPipe;
	binPipe;
	uniform;
	geoUniform;
	presentUniform;
	toneBuf;
	dummyLut;
	dummy2d;
	sampler;
	colorTex = null;
	finalTex = null;
	current = null;
	look = {
		recipe: null,
		lut: null,
		lutStrength: 1,
		adjust: {},
		curve: null
	};
	geometry = {
		straighten: 0,
		rotate90: 0,
		flip: !1,
		box: null
	};
	dirtyColor = !0;
	dirtyGeo = !0;
	raf = 0;
	lost = !1;
	onFrame = null;
	onLost = null;
	static async create() {
		if (!("gpu" in navigator) || !navigator.gpu) return null;
		let t = await navigator.gpu.requestAdapter({ powerPreference: "high-performance" });
		if (!t) return null;
		let n = await t.requestDevice({ requiredLimits: {
			maxStorageBufferBindingSize: t.limits.maxStorageBufferBindingSize,
			maxBufferSize: t.limits.maxBufferSize
		} }), r = new e(n, t);
		for (let e of await r.compileMessages()) if (e.type === "error") throw Error(`WGSL ${e.lineNum}: ${e.message}`);
		return r;
	}
	modules;
	constructor(e, t) {
		this.device = e;
		let n = t.info;
		this.info = {
			vendor: n.vendor,
			architecture: n.architecture,
			description: n.description,
			fallback: !!n.isFallbackAdapter
		}, this.format = navigator.gpu.getPreferredCanvasFormat(), e.lost.then((e) => {
			this.lost = !0, this.onLost?.(e.message || e.reason || "lost");
		});
		let i = (t) => e.createShaderModule({ code: t }), a = i("// 颜色链：逐项对应 src/gxr_studio 里的 Python 实现（文件名:函数写在注释里），不另发明调整。\n// 数值约定：链内颜色是 0..255 的浮点；Python 里落成 uint8 的地方，这里都做 floor(clamp(x)+0.5)。\n\nstruct P {\n  gains: vec4f,     // render/tones.py channel_gains：R、G、B 乘数（无调整时为 1）\n  flags: vec4f,     // x 有调子表；y 模式（0 配方、1 配方+显示类 LUT、2 Log LUT、3 相机 JPG、4 相机 JPG+显示类 LUT）；z apply_base 生效；w LUT 强度\n  extra: vec4f,     // finish/pixels.py apply_extra：黑点、饱和度、自然饱和度、暗角\n  dims: vec4f,      // 颜色层（未裁切、未拉直）宽、高\n  bake: vec4f,      // render/chain.py ChromaBake：origin cb、origin cr、1/step、nx\n  bake2: vec4f,     // ny\n  lutA: vec4f,      // 输入空间 id、输出空间 id、边长 n、输入 range（1 = legal）\n  lutDom0: vec4f,   // domain_min\n  lutDom1: vec4f,   // domain_max\n  lutRng: vec4f,    // range3 lo、hi；z 输出 range（1 = legal）\n  inM0: vec4f, inM1: vec4f, inM2: vec4f,     // 进 LUT 前的 3×3 + 偏移\n  outM0: vec4f, outM1: vec4f, outM2: vec4f,  // LUT 输出线性 → sRGB 线性\n};\n@group(0) @binding(0) var<uniform> p: P;\n@group(0) @binding(1) var<storage, read> rec: array<f32>;   // pre(4096×3) | matrix(9) | offset(3) | gain(ny×nx×2)\n@group(0) @binding(2) var<storage, read> tone: array<f32>;  // render/tones.py display_table（1021 点）\n@group(0) @binding(3) var lut3: texture_3d<f32>;             // .cube 3D 表，x=R、y=G、z=B\n\nconst MAT_AT: u32 = 12288u;\nconst OFF_AT: u32 = 12297u;\nconst GAIN_AT: u32 = 12300u;\n\nfn q8(v: vec3f) -> vec3f { return floor(clamp(v, vec3f(0.0), vec3f(255.0)) + 0.5); }\n\n// render/tones.py apply_table\nfn applyTable(v: vec3f) -> vec3f {\n  var o: vec3f;\n  for (var c = 0u; c < 3u; c++) {\n    let pos = clamp(v[c], 0.0, 255.0) * 4.0;\n    let i = min(u32(pos), 1019u);\n    let lo = tone[i];\n    o[c] = lo + (tone[i + 1u] - lo) * (pos - f32(i));\n  }\n  return o;\n}\n\n// render/chain.py _chroma_chunk + quantize\nfn recipeChain(codesIn: vec3f, gains: vec3f, useTable: bool) -> vec3f {\n  let x = clamp(codesIn * gains, vec3f(0.0), vec3f(4095.0));\n  var pc: vec3f;\n  for (var c = 0u; c < 3u; c++) {\n    let i = min(u32(x[c]), 4094u);\n    let v0 = rec[i * 3u + c];\n    pc[c] = v0 + (rec[(i + 1u) * 3u + c] - v0) * (x[c] - f32(i));\n  }\n  let y = rec[MAT_AT] * pc.x + rec[MAT_AT + 1u] * pc.y + rec[MAT_AT + 2u] * pc.z + rec[OFF_AT];\n  let cb = rec[MAT_AT + 3u] * pc.x + rec[MAT_AT + 4u] * pc.y + rec[MAT_AT + 5u] * pc.z + rec[OFF_AT + 1u];\n  let cr = rec[MAT_AT + 6u] * pc.x + rec[MAT_AT + 7u] * pc.y + rec[MAT_AT + 8u] * pc.z + rec[OFF_AT + 2u];\n  let nx = i32(p.bake.w);\n  let ny = i32(p.bake2.x);\n  let u = (cb - p.bake.x) * p.bake.z;\n  let v = (cr - p.bake.y) * p.bake.z;\n  let iu = clamp(i32(clamp(u, -1e6, 1e6)), 0, nx - 2);   // numpy astype(int32)：向零取整，再夹\n  let iv = clamp(i32(clamp(v, -1e6, 1e6)), 0, ny - 2);\n  let fu = u - f32(iu);\n  let fv = v - f32(iv);\n  let at = u32(iv * nx + iu);\n  let un = u32(nx);\n  let a00 = vec2f(rec[GAIN_AT + at * 2u], rec[GAIN_AT + at * 2u + 1u]);\n  let a01 = vec2f(rec[GAIN_AT + (at + 1u) * 2u], rec[GAIN_AT + (at + 1u) * 2u + 1u]);\n  let a10 = vec2f(rec[GAIN_AT + (at + un) * 2u], rec[GAIN_AT + (at + un) * 2u + 1u]);\n  let a11 = vec2f(rec[GAIN_AT + (at + un + 1u) * 2u], rec[GAIN_AT + (at + un + 1u) * 2u + 1u]);\n  let gg = (a00 * (1.0 - fu) + a01 * fu) * (1.0 - fv) + (a10 * (1.0 - fu) + a11 * fu) * fv;\n  let cbo = cb * gg.x - cr * gg.y;\n  let cro = cb * gg.y + cr * gg.x;\n  var rgb = clamp(vec3f(y + 1.402 * cro, y - 0.344136 * cbo - 0.714136 * cro, y + 1.772 * cbo), vec3f(0.0), vec3f(255.0));\n  if (useTable) { rgb = applyTable(rgb); }\n  return q8(rgb);\n}\n\n// lut/spaces.py encode / decode：srgb=0 rec709=1 gamma24=2 llog=3 linear=4 flog=5 flog2=6 vlog=7 slog3=8 applelog=9\nfn log10f(x: f32) -> f32 { return log2(max(x, 1e-30)) * 0.30102999566398120; }\nfn enc1(x: f32, s: u32) -> f32 {\n  switch s {\n    case 0u: { if (x <= 0.0031308) { return 12.92 * x; } return 1.055 * pow(max(x, 0.0), 1.0 / 2.4) - 0.055; }\n    case 1u: { if (x < 0.018) { return 4.5 * x; } return 1.099 * pow(max(x, 0.0), 0.45) - 0.099; }\n    case 2u: { return sign(x) * pow(abs(x), 1.0 / 2.4); }\n    case 3u: { if (x <= 0.006) { return 8.0 * x + 0.09; } return 0.27 * log10f(1.3 * x + 0.0115) + 0.6; }\n    case 5u: { if (x < 0.00089) { return 8.735631 * x + 0.092864; } return 0.344676 * log10f(0.555556 * x + 0.009468) + 0.790453; }\n    case 6u: { if (x < 0.000889) { return 8.799461 * x + 0.092864; } return 0.245281 * log10f(5.555556 * x + 0.064829) + 0.384316; }\n    case 7u: { if (x < 0.01) { return 5.6 * x + 0.125; } return 0.241514 * log10f(x + 0.00873) + 0.598206; }\n    case 8u: { if (x < 0.01125) { return (95.0 + x * (171.2102946929 - 95.0) / 0.01125) / 1023.0; } return (420.0 + 261.5 * log10f((x + 0.01) / 0.19)) / 1023.0; }\n    case 9u: { if (x >= 0.01) { return 0.08550479 * log2(max(x + 0.00964052, 1e-30)) + 0.69336945; } return 47.28711236 * pow(max(x + 0.05641088, 0.0), 2.0); }\n    default: { return x; }\n  }\n}\nfn dec1(x: f32, s: u32) -> f32 {\n  switch s {\n    case 0u: { if (x <= 0.04045) { return x / 12.92; } return pow(max((x + 0.055) / 1.055, 0.0), 2.4); }\n    case 1u: { if (x < 0.081) { return x / 4.5; } return pow(max((x + 0.099) / 1.099, 0.0), 1.0 / 0.45); }\n    case 2u: { return sign(x) * pow(abs(x), 2.4); }\n    default: { return x; }\n  }\n}\nfn enc3(v: vec3f, s: u32) -> vec3f { return vec3f(enc1(v.x, s), enc1(v.y, s), enc1(v.z, s)); }\nfn dec3(v: vec3f, s: u32) -> vec3f { return vec3f(dec1(v.x, s), dec1(v.y, s), dec1(v.z, s)); }\nfn mat3x(v: vec3f, a: vec4f, b: vec4f, c: vec4f) -> vec3f {\n  return vec3f(dot(a.xyz, v) + a.w, dot(b.xyz, v) + b.w, dot(c.xyz, v) + c.w);\n}\n\n// lut/cube.py Cube.tetrahedral：小数部分从大到小（相等时通道序号小的在前）\nfn lutAt(c: vec3i) -> vec3f { return textureLoad(lut3, c, 0).xyz; }\nfn tetra(x: vec3f) -> vec3f {\n  let n = i32(p.lutA.z);\n  let pos = x * f32(n - 1);\n  let base = min(vec3i(pos), vec3i(n - 2));\n  let f = pos - vec3f(base);\n  var o0 = 0; var o1 = 1; var o2 = 2;\n  if (f[o1] > f[o0]) { let t = o0; o0 = o1; o1 = t; }\n  if (f[o2] > f[o1]) { let t = o1; o1 = o2; o2 = t; }\n  if (f[o1] > f[o0]) { let t = o0; o0 = o1; o1 = t; }\n  var b = base;\n  let v0 = lutAt(b);\n  b[o0] += 1; let v1 = lutAt(b);\n  b[o1] += 1; let v2 = lutAt(b);\n  b[o2] += 1; let v3 = lutAt(b);\n  return v0 + f[o0] * (v1 - v0) + f[o1] * (v2 - v1) + f[o2] * (v3 - v2);\n}\n\n// lut/spaces.py Target.render（+ lut/photo.py _apply 的量化在调用处）\nfn lutRender(lin_in: vec3f) -> vec3f {\n  var x = enc3(lin_in, u32(p.lutA.x));\n  if (p.lutA.w > 0.5) { x = (64.0 + 876.0 * x) / 1023.0; }\n  x = (x - p.lutDom0.xyz) / (p.lutDom1.xyz - p.lutDom0.xyz);\n  x = (x - vec3f(p.lutRng.x)) / (p.lutRng.y - p.lutRng.x);\n  var o = tetra(clamp(x, vec3f(0.0), vec3f(1.0)));\n  if (p.lutRng.z > 0.5) { o = (1023.0 * o - 64.0) / 876.0; }\n  let srgb = enc3(mat3x(dec3(o, u32(p.lutA.y)), p.outM0, p.outM1, p.outM2), 0u);\n  return clamp(srgb, vec3f(0.0), vec3f(1.0)) * 255.0;\n}\n\n// finish/pixels.py _srgb_to_linear / apply_base（LUT 后和相机 JPG 上的调整）\nfn srgbToLin(u: vec3f) -> vec3f {\n  let x = u / 255.0;\n  return select(pow((x + 0.055) / 1.055, vec3f(2.4)), x / 12.92, x <= vec3f(0.04045));\n}\nfn applyBase(u: vec3f) -> vec3f {\n  let x = max(srgbToLin(u) * p.gains.xyz, vec3f(0.0));\n  let e = select(1.055 * pow(x, vec3f(1.0 / 2.4)) - 0.055, x * 12.92, x <= vec3f(0.0031308));\n  var o = floor(clamp(e * 255.0 + 0.5, vec3f(0.0), vec3f(255.0)));\n  if (p.flags.x > 0.5) { o = floor(clamp(applyTable(o) + 0.5, vec3f(0.0), vec3f(255.0))); }\n  return o;\n}\n\n// finish/pixels.py apply_extra：黑点 → 饱和度 → 自然饱和度 → 暗角，每步落成 uint8\nfn applyExtra(u0: vec3f, pix: vec2f, wh: vec2f) -> vec3f {\n  var u = u0;\n  let bp = p.extra.x;\n  if (bp != 0.0) {\n    var y = u / 255.0;\n    let k = abs(bp) / 100.0 * 0.35;\n    if (bp > 0.0) { y = clamp((y - k) / max(1e-4, 1.0 - k), vec3f(0.0), vec3f(1.0)); } else { y = y * (1.0 - k) + k; }\n    u = floor(clamp(y * 255.0 + 0.5, vec3f(0.0), vec3f(255.0)));\n  }\n  if (p.extra.y != 0.0) {\n    let gray = u.x * 0.2126 + u.y * 0.7152 + u.z * 0.0722;\n    u = floor(clamp(gray + (u - gray) * (1.0 + p.extra.y / 100.0) + 0.5, vec3f(0.0), vec3f(255.0)));\n  }\n  if (p.extra.z != 0.0) {\n    let gray = u.x * 0.2126 + u.y * 0.7152 + u.z * 0.0722;\n    let dull = 1.0 - clamp((max(u.x, max(u.y, u.z)) - min(u.x, min(u.y, u.z))) / 255.0, 0.0, 1.0);\n    u = floor(clamp(gray + (u - gray) * (1.0 + p.extra.z / 100.0 * dull) + 0.5, vec3f(0.0), vec3f(255.0)));\n  }\n  if (p.extra.w != 0.0) {\n    let hx = max((wh.x - 1.0) / 2.0, 1.0);\n    let hy = max((wh.y - 1.0) / 2.0, 1.0);\n    let r = clamp(length(vec2f((pix.x - (wh.x - 1.0) / 2.0) / hx, (pix.y - (wh.y - 1.0) / 2.0) / hy)), 0.0, 1.0);\n    let g = 1.0 + p.extra.w / 100.0 * 0.65 * (r * r * (3.0 - 2.0 * r));\n    u = floor(clamp(u * g + 0.5, vec3f(0.0), vec3f(255.0)));\n  }\n  return u;\n}\n\n// 一个像素的整条颜色链。codes：白平衡码值（RAW 模式）；jpg：相机 JPG 的 0..255 整数（JPG 模式）\nfn colorChain(codes: vec3f, jpg: vec3f, pix: vec2f) -> vec3f {\n  let mode = u32(p.flags.y);\n  var u: vec3f;\n  if (mode == 0u) {\n    u = recipeChain(codes, p.gains.xyz, p.flags.x > 0.5);\n  } else if (mode == 3u) {\n    u = jpg;\n    if (p.flags.z > 0.5) { u = applyBase(u); }\n  } else {\n    var base: vec3f;\n    var lin_in: vec3f;\n    if (mode == 2u) {\n      base = q8(enc3(clamp(codes / 4095.0, vec3f(0.0), vec3f(1.0)), 0u) * 255.0);\n      lin_in = mat3x(codes, p.inM0, p.inM1, p.inM2);          // 码值 → 场景 XYZ → LUT 输入色域\n    } else {\n      base = select(recipeChain(codes, vec3f(1.0), false), jpg, mode == 4u);\n      lin_in = mat3x(srgbToLin(base), p.inM0, p.inM1, p.inM2);\n    }\n    u = q8(mix(base, lutRender(lin_in), p.flags.w));\n    if (p.flags.z > 0.5) { u = applyBase(u); }\n  }\n  return applyExtra(u, pix, p.dims.xy);\n}\n// 第一遍：源图（RAW 码值或相机 JPG）→ 颜色层，未拉直、未裁切。rgba8unorm 存整数码值。\n@group(1) @binding(0) var proxy: texture_2d<f32>;\n@group(1) @binding(1) var jpgTex: texture_2d<f32>;\n\n@vertex fn vs(@builtin(vertex_index) i: u32) -> @builtin(position) vec4f {\n  let xy = vec2f(f32((i << 1u) & 2u), f32(i & 2u));\n  return vec4f(xy * 2.0 - 1.0, 0.0, 1.0);\n}\n\n@fragment fn fs(@builtin(position) pos: vec4f) -> @location(0) vec4f {\n  let pix = vec2i(pos.xy);\n  let mode = u32(p.flags.y);\n  var codes = vec3f(0.0);\n  var jpg = vec3f(0.0);\n  if (mode == 3u || mode == 4u) {\n    jpg = floor(textureLoad(jpgTex, pix, 0).xyz * 255.0 + 0.5);\n  } else {\n    codes = textureLoad(proxy, pix, 0).xyz;\n  }\n  return vec4f(colorChain(codes, jpg, vec2f(pix)) / 255.0, 1.0);\n}\n"), o = i("// 第二遍：拉直 → 90° 旋转 / 水平翻转 → 裁切框，1:1 写进成片纹理（读回和 Python 逐像素比较的就是它）。\n// finish/geometry.py：_straighten_coords + _sample（双线性后 +0.5 取整，FILL = 1B1C1F），_rot_flip（np.rot90 逆时针、fliplr）。\nstruct G {\n  src: vec4f,   // 颜色层宽、高；旋转后画布宽、高\n  geo: vec4f,   // cos、sin、90° 次数（0..3）、翻转\n  box: vec4f,   // 裁切框（旋转后画布的像素）：x、y、w、h\n  pad: vec4f,\n};\n@group(0) @binding(0) var<uniform> g: G;\n@group(0) @binding(1) var colorTex: texture_2d<f32>;\n\n@vertex fn vs(@builtin(vertex_index) i: u32) -> @builtin(position) vec4f {\n  let xy = vec2f(f32((i << 1u) & 2u), f32(i & 2u));\n  return vec4f(xy * 2.0 - 1.0, 0.0, 1.0);\n}\n\nfn texel(x: i32, y: i32) -> vec3f { return textureLoad(colorTex, vec2i(x, y), 0).xyz * 255.0; }\n\n// 拉直：输出画布上的点 (X, Y) → 在颜色层里双线性取样（与 _sample 相同的有效区和取整）\nfn straightened(X: f32, Y: f32) -> vec4f {\n  let W = g.src.x;\n  let H = g.src.y;\n  if (g.geo.x == 1.0 && g.geo.y == 0.0) {\n    return vec4f(texel(i32(X), i32(Y)), 1.0);\n  }\n  // f32 坐标：落在有效区边界 1e-4 像素以内的个别像素，可能与 Python（float64）判断不同\n  let dx = X - (W - 1.0) / 2.0;\n  let dy = Y - (H - 1.0) / 2.0;\n  let sx = g.geo.x * dx + g.geo.y * dy + (W - 1.0) / 2.0;\n  let sy = -g.geo.y * dx + g.geo.x * dy + (H - 1.0) / 2.0;\n  if (!(sx >= -1e-3 && sy >= -1e-3 && sx <= W - 1.0 + 1e-3 && sy <= H - 1.0 + 1e-3)) {\n    return vec4f(27.0, 28.0, 31.0, 1.0);\n  }\n  let fx = floor(sx);\n  let fy = floor(sy);\n  let x0 = clamp(i32(fx), 0, i32(W) - 1);\n  let y0 = clamp(i32(fy), 0, i32(H) - 1);\n  let x1 = min(x0 + 1, i32(W) - 1);\n  let y1 = min(y0 + 1, i32(H) - 1);\n  let tx = sx - fx;\n  let ty = sy - fy;\n  let s = texel(x0, y0) * ((1.0 - tx) * (1.0 - ty)) + texel(x1, y0) * (tx * (1.0 - ty)) + texel(x0, y1) * ((1.0 - tx) * ty) + texel(x1, y1) * (tx * ty);\n  return vec4f(floor(clamp(s + 0.5, vec3f(0.0), vec3f(255.0))), 1.0);\n}\n\n@fragment fn fs(@builtin(position) pos: vec4f) -> @location(0) vec4f {\n  // 成片像素 → 旋转后画布的整数像素\n  var cx = g.box.x + floor(pos.x);\n  let cy = g.box.y + floor(pos.y);\n  if (g.geo.w > 0.5) { cx = g.src.z - 1.0 - cx; }\n  // np.rot90(a, k) 的逆映射：输出 [i, j] ← 输入（W、H 是颜色层的宽高）\n  let W = g.src.x;\n  let H = g.src.y;\n  var X = cx;\n  var Y = cy;\n  let k = u32(g.geo.z);\n  if (k == 1u) { X = W - 1.0 - cy; Y = cx; }\n  else if (k == 2u) { X = W - 1.0 - cx; Y = H - 1.0 - cy; }\n  else if (k == 3u) { X = cy; Y = H - 1.0 - cx; }\n  return vec4f(straightened(X, Y).xyz / 255.0, 1.0);\n}\n"), s = i("// 第三遍：成片纹理 → 画布。按显示比例缩放；缩小时 2×2 超采样，避免锯齿。只影响显示，不影响读回。\nstruct V {\n  map: vec4f,   // 画布像素 → 成片像素：缩放、偏移 x、偏移 y、背景灰度\n  size: vec4f,  // 成片宽、高\n};\n@group(0) @binding(0) var<uniform> v: V;\n@group(0) @binding(1) var finalTex: texture_2d<f32>;\n@group(0) @binding(2) var smp: sampler;\n\n@vertex fn vs(@builtin(vertex_index) i: u32) -> @builtin(position) vec4f {\n  let xy = vec2f(f32((i << 1u) & 2u), f32(i & 2u));\n  return vec4f(xy * 2.0 - 1.0, 0.0, 1.0);\n}\n\n@fragment fn fs(@builtin(position) pos: vec4f) -> @location(0) vec4f {\n  let s = v.map.x;\n  let q = (pos.xy - v.map.yz) * s;  // 成片像素坐标（左上角为 0）\n  if (q.x < 0.0 || q.y < 0.0 || q.x > v.size.x || q.y > v.size.y) { return vec4f(vec3f(v.map.w), 1.0); }\n  let inv = 1.0 / v.size.xy;\n  var c: vec3f;\n  if (s > 1.25) {\n    let d = s * 0.25;\n    c = (textureSampleLevel(finalTex, smp, (q + vec2f(-d, -d)) * inv, 0.0).xyz\n       + textureSampleLevel(finalTex, smp, (q + vec2f(d, -d)) * inv, 0.0).xyz\n       + textureSampleLevel(finalTex, smp, (q + vec2f(-d, d)) * inv, 0.0).xyz\n       + textureSampleLevel(finalTex, smp, (q + vec2f(d, d)) * inv, 0.0).xyz) * 0.25;\n  } else {\n    c = textureSampleLevel(finalTex, smp, q * inv, 0.0).xyz;\n  }\n  return vec4f(c, 1.0);\n}\n"), c = i("// RAW 条带字节 → 半分辨率白平衡码值（raw/codes.py half_res_codes：裁切窗口内每个 2×2 同色取平均 / AsShotNeutral × 4095，不去马赛克），\n// 再按 Orientation 转正（raw/codes.py orient_array）。输出 rgba32float。\nstruct R {\n  crop: vec4f,    // 裁切窗口：y0、x0、高、宽（RAW 平面坐标）\n  wb: vec4f,      // 4095 / AsShotNeutral（R、G、B）\n  info: vec4f,    // 每行字节数、位深（12 / 16）、大端（1）、方向（1..8）\n  phase: vec4f,   // 裁切窗口第一个 2×2 的颜色（行优先）\n  black: vec4f,   // 黑电平：相对 ActiveArea 的 (y%2, x%2) 四个值（行优先）\n  level: vec4f,   // 白电平、ActiveArea 顶、ActiveArea 左\n};\n@group(0) @binding(0) var<uniform> r: R;\n@group(0) @binding(1) var<storage, read> raw: array<u32>;\n@group(0) @binding(2) var outTex: texture_storage_2d<rgba32float, write>;\n\nfn byteAt(i: u32) -> u32 { return (raw[i >> 2u] >> ((i & 3u) * 8u)) & 255u; }\n\nfn sample(y: u32, x: u32) -> f32 {\n  let rb = u32(r.info.x);\n  var v: u32;\n  if (r.info.y == 16.0) {\n    let b = y * rb + x * 2u;\n    if (r.info.z > 0.5) { v = (byteAt(b) << 8u) | byteAt(b + 1u); } else { v = byteAt(b) | (byteAt(b + 1u) << 8u); }\n  } else {\n    let b = y * rb + (x >> 1u) * 3u;\n    if ((x & 1u) == 0u) { v = (byteAt(b) << 4u) | (byteAt(b + 1u) >> 4u); } else { v = ((byteAt(b + 1u) & 15u) << 8u) | byteAt(b + 2u); }\n  }\n  let ry = (y - u32(r.level.y)) & 1u;\n  let rx = (x - u32(r.level.z)) & 1u;\n  let black = r.black[ry * 2u + rx];\n  return (f32(v) - black) / (r.level.x - black);\n}\n\n@compute @workgroup_size(16, 16) fn main(@builtin(global_invocation_id) id: vec3u) {\n  let w2 = u32(r.crop.w) / 2u;\n  let h2 = u32(r.crop.z) / 2u;\n  let o = u32(r.info.w);\n  let swap = o >= 5u;\n  let wo = select(w2, h2, swap);\n  let ho = select(h2, w2, swap);\n  if (id.x >= wo || id.y >= ho) { return; }\n  let i = id.y;\n  let j = id.x;\n  // 输出 [i, j] ← 存储 [rr, cc]（与 orient_array 的 numpy 写法一一对应）\n  var rr = i; var cc = j;\n  switch o {\n    case 2u: { cc = w2 - 1u - j; }\n    case 3u: { rr = h2 - 1u - i; cc = w2 - 1u - j; }\n    case 4u: { rr = h2 - 1u - i; }\n    case 5u: { rr = j; cc = i; }\n    case 6u: { rr = h2 - 1u - j; cc = i; }\n    case 7u: { rr = h2 - 1u - j; cc = w2 - 1u - i; }\n    case 8u: { rr = j; cc = w2 - 1u - i; }\n    default: {}\n  }\n  let y = u32(r.crop.x) + rr * 2u;\n  let x = u32(r.crop.y) + cc * 2u;\n  var sum = vec3f(0.0);\n  var cnt = vec3f(0.0);\n  for (var a = 0u; a < 2u; a++) {\n    for (var b = 0u; b < 2u; b++) {\n      let c = u32(r.phase[a * 2u + b]);\n      sum[c] += sample(y + a, x + b);\n      cnt[c] += 1.0;\n    }\n  }\n  textureStore(outTex, vec2i(i32(j), i32(i)), vec4f(sum / cnt * r.wb.xyz, 0.0));\n}\n");
		this.modules = [
			a,
			o,
			s,
			c
		], this.colorPipe = e.createRenderPipeline({
			layout: "auto",
			vertex: {
				module: a,
				entryPoint: "vs"
			},
			fragment: {
				module: a,
				entryPoint: "fs",
				targets: [{ format: "rgba8unorm" }]
			}
		}), this.geoPipe = e.createRenderPipeline({
			layout: "auto",
			vertex: {
				module: o,
				entryPoint: "vs"
			},
			fragment: {
				module: o,
				entryPoint: "fs",
				targets: [{ format: "rgba8unorm" }]
			}
		}), this.presentPipe = e.createRenderPipeline({
			layout: "auto",
			vertex: {
				module: s,
				entryPoint: "vs"
			},
			fragment: {
				module: s,
				entryPoint: "fs",
				targets: [{ format: this.format }]
			}
		}), this.binPipe = e.createComputePipeline({
			layout: "auto",
			compute: {
				module: c,
				entryPoint: "main"
			}
		});
		let l = (t) => e.createBuffer({
			size: t,
			usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST
		});
		this.uniform = l(288), this.geoUniform = l(64), this.presentUniform = l(32), this.toneBuf = e.createBuffer({
			size: r * 4,
			usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_DST
		}), this.dummyLut = e.createTexture({
			size: [
				2,
				2,
				2
			],
			dimension: "3d",
			format: "rgba32float",
			usage: GPUTextureUsage.TEXTURE_BINDING
		}), this.dummy2d = e.createTexture({
			size: [1, 1],
			format: "rgba8unorm",
			usage: GPUTextureUsage.TEXTURE_BINDING
		}), this.sampler = e.createSampler({
			magFilter: "linear",
			minFilter: "linear"
		});
	}
	async compileMessages() {
		let e = [];
		for (let t of this.modules) e.push(...(await t.getCompilationInfo()).messages);
		return e;
	}
	get isLost() {
		return this.lost;
	}
	hasSource(e) {
		return this.sources.has(e);
	}
	sourceSize(e) {
		let t = this.sources.get(e);
		return t ? {
			w: t.w,
			h: t.h
		} : null;
	}
	hasBake(e) {
		return this.bakes.has(e);
	}
	hasLut(e) {
		return this.luts.has(e);
	}
	async addRaw(e, t, n, r) {
		let i = this.device, a = t.strips.reduce((e, [, t]) => e + t, 0), o = new Uint8Array(Math.ceil(a / 4) * 4), s = 0;
		for (let [e, r] of t.strips) o.set(new Uint8Array(n, e, r), s), s += r;
		let c = i.createBuffer({
			size: o.byteLength,
			usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_DST
		});
		i.queue.writeBuffer(c, 0, o);
		let l = Math.floor(t.crop[3] / 2), u = Math.floor(t.crop[2] / 2), d = t.orientation >= 5, f = d ? u : l, p = d ? l : u, m = i.createTexture({
			size: [f, p],
			format: "rgba32float",
			usage: GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.STORAGE_BINDING
		}), h = /* @__PURE__ */ new Float32Array(24);
		h.set(t.crop, 0), h.set(t.wb, 4), h.set([
			t.row_bytes,
			t.bps,
			+!!t.big_endian,
			t.orientation
		], 8), h.set(t.phase, 12), h.set(t.black, 16), h.set([
			t.white,
			t.active[0],
			t.active[1],
			0
		], 20);
		let g = i.createBuffer({
			size: h.byteLength,
			usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST
		});
		i.queue.writeBuffer(g, 0, h);
		let _ = i.createCommandEncoder(), v = _.beginComputePass();
		v.setPipeline(this.binPipe), v.setBindGroup(0, i.createBindGroup({
			layout: this.binPipe.getBindGroupLayout(0),
			entries: [
				{
					binding: 0,
					resource: { buffer: g }
				},
				{
					binding: 1,
					resource: { buffer: c }
				},
				{
					binding: 2,
					resource: m.createView()
				}
			]
		})), v.dispatchWorkgroups(Math.ceil(f / 16), Math.ceil(p / 16)), v.end(), i.queue.submit([_.finish()]), await i.queue.onSubmittedWorkDone(), c.destroy(), g.destroy(), this.putSource({
			key: e,
			kind: "raw",
			tex: m,
			w: f,
			h: p,
			bytes: f * p * 16,
			scene: r
		});
	}
	addJpg(e, t) {
		let n = this.device.createTexture({
			size: [t.width, t.height],
			format: "rgba8unorm",
			usage: GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.COPY_DST | GPUTextureUsage.RENDER_ATTACHMENT
		});
		this.device.queue.copyExternalImageToTexture({ source: t }, { texture: n }, [t.width, t.height]), this.putSource({
			key: e,
			kind: "jpg",
			tex: n,
			w: t.width,
			h: t.height,
			bytes: t.width * t.height * 4,
			scene: null
		});
	}
	putSource(e) {
		this.sources.get(e.key)?.tex.destroy(), this.sources.delete(e.key), this.sources.set(e.key, e);
		let t = 0;
		for (let e of this.sources.values()) t += e.bytes;
		for (let [n, r] of this.sources) t <= 671088640 || r === e || r === this.current || (r.tex.destroy(), this.sources.delete(n), t -= r.bytes);
	}
	addBake(e, t, n) {
		let r = this.device.createBuffer({
			size: n.byteLength,
			usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_DST
		});
		this.device.queue.writeBuffer(r, 0, n), this.bakes.set(e, {
			meta: t,
			buf: r
		}), this.trim(this.bakes, 24, (e) => e.buf.destroy());
	}
	addLut(e, t, n) {
		let r = t.n, i = this.device.createTexture({
			size: [
				r,
				r,
				r
			],
			dimension: "3d",
			format: "rgba32float",
			usage: GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.COPY_DST
		});
		this.device.queue.writeTexture({ texture: i }, n, {
			bytesPerRow: r * 16,
			rowsPerImage: r
		}, [
			r,
			r,
			r
		]), this.luts.set(e, {
			meta: t,
			tex: i
		}), this.trim(this.luts, 8, (e) => e.tex.destroy());
	}
	trim(e, t, n) {
		for (; e.size > t;) {
			let t = e.entries().next().value;
			if (!t) break;
			n(t[1]), e.delete(t[0]);
		}
	}
	attach(e) {
		this.canvas !== e && (this.canvas = e, this.context = e.getContext("webgpu"), this.context?.configure({
			device: this.device,
			format: this.format,
			alphaMode: "opaque"
		}), this.dirtyGeo = !0, this.schedule());
	}
	detach() {
		this.raf && cancelAnimationFrame(this.raf), this.raf = 0, this.context?.unconfigure(), this.canvas = null, this.context = null;
	}
	show(e) {
		let t = this.sources.get(e);
		return t ? (this.sources.delete(e), this.sources.set(e, t), this.current !== t && (this.current = t, this.dirtyColor = !0, this.dirtyGeo = !0, this.schedule()), !0) : !1;
	}
	setLook(e) {
		Object.assign(this.look, e), this.dirtyColor = !0, this.dirtyGeo = !0, this.schedule();
	}
	setGeometry(e) {
		Object.assign(this.geometry, e), this.dirtyGeo = !0, this.schedule();
	}
	currentKey() {
		return this.current ? this.current.key : null;
	}
	layerSize() {
		return this.current ? {
			w: this.current.w,
			h: this.current.h
		} : null;
	}
	finalSize() {
		let e = this.current;
		if (!e) return null;
		let t = this.geometry.box;
		return t ? {
			w: t[2],
			h: t[3]
		} : (this.geometry.rotate90 % 4 + 4) % 4 % 2 ? {
			w: e.h,
			h: e.w
		} : {
			w: e.w,
			h: e.h
		};
	}
	schedule() {
		this.raf || !this.canvas || this.lost || (this.raf = requestAnimationFrame(() => {
			this.raf = 0, this.frame();
		}));
	}
	lutState(e) {
		let t = this.look.lut ? this.luts.get(this.look.lut) ?? null : null;
		return e.kind === "jpg" ? {
			mode: t ? 4 : 3,
			lut: t
		} : t ? {
			mode: t.meta.display ? 1 : 2,
			lut: t
		} : {
			mode: 0,
			lut: null
		};
	}
	writeUniform(e, t, n, r) {
		let a = this.look.adjust, o = /* @__PURE__ */ new Float32Array(72), s = i(a) ?? [
			1,
			1,
			1
		];
		o.set([...s, 0], 0);
		let c = u(a, this.look.curve);
		c && this.device.queue.writeBuffer(this.toneBuf, 0, c);
		let l = !!(a.exposure_ev || a.wb?.warm || a.wb?.tint || c);
		if (o.set([
			+!!c,
			r,
			+!!l,
			this.look.lutStrength
		], 4), o.set([
			a.black_point ?? 0,
			a.saturation ?? 0,
			a.vibrance ?? 0,
			a.vignette ?? 0
		], 8), o.set([
			e.w,
			e.h,
			0,
			0
		], 12), t && (o.set([
			t.meta.origin[0],
			t.meta.origin[1],
			1 / t.meta.step,
			t.meta.nx
		], 16), o.set([
			t.meta.ny,
			0,
			0,
			0
		], 20)), n) {
			let t = n.meta;
			o.set([
				t.input_space_id,
				t.output_space_id,
				t.n,
				+!!t.input_legal
			], 24), o.set([...t.domain_min, 0], 28), o.set([...t.domain_max, 0], 32), o.set([
				t.range3[0],
				t.range3[1],
				+!!t.output_legal,
				0
			], 36);
			let r = t.display_to_input, i = [
				0,
				0,
				0
			];
			if (!t.display && e.scene) {
				r = f(t.xyz_to_input, e.scene.A);
				let n = e.scene.b;
				i = t.xyz_to_input.map((e) => e[0] * n[0] + e[1] * n[1] + e[2] * n[2]);
			}
			for (let e = 0; e < 3; e++) o.set([...r[e], i[e]], 40 + e * 4);
			for (let e = 0; e < 3; e++) o.set([...t.out_to_srgb[e], 0], 52 + e * 4);
		}
		this.device.queue.writeBuffer(this.uniform, 0, o);
	}
	ensureTextures(e, t, n) {
		let r = this.device;
		this.colorTex && this.colorTex.width === e.w && this.colorTex.height === e.h || (this.colorTex?.destroy(), this.colorTex = r.createTexture({
			size: [e.w, e.h],
			format: "rgba8unorm",
			usage: GPUTextureUsage.RENDER_ATTACHMENT | GPUTextureUsage.TEXTURE_BINDING
		}), this.dirtyColor = !0), this.finalTex && this.finalTex.width === t && this.finalTex.height === n || (this.finalTex?.destroy(), this.finalTex = r.createTexture({
			size: [t, n],
			format: "rgba8unorm",
			usage: GPUTextureUsage.RENDER_ATTACHMENT | GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.COPY_SRC
		}), this.dirtyGeo = !0);
	}
	encodeColor(e, t) {
		let { mode: n, lut: r } = this.lutState(t), i = n === 0 || n === 1, a = this.look.recipe ? this.bakes.get(this.look.recipe) ?? null : null;
		if (i && !a || this.look.lut && !r || n === 2 && !t.scene) return !1;
		this.writeUniform(t, a, r, n);
		let o = this.device, s = a ?? this.bakes.values().next().value, c = s ? s.buf : this.toneBuf, l = o.createBindGroup({
			layout: this.colorPipe.getBindGroupLayout(0),
			entries: [
				{
					binding: 0,
					resource: { buffer: this.uniform }
				},
				{
					binding: 1,
					resource: { buffer: c }
				},
				{
					binding: 2,
					resource: { buffer: this.toneBuf }
				},
				{
					binding: 3,
					resource: (r ? r.tex : this.dummyLut).createView({ dimension: "3d" })
				}
			]
		}), u = t.kind === "raw" ? t.tex.createView() : this.dummyRawView(), d = t.kind === "jpg" ? t.tex.createView() : this.dummy2d.createView(), f = o.createBindGroup({
			layout: this.colorPipe.getBindGroupLayout(1),
			entries: [{
				binding: 0,
				resource: u
			}, {
				binding: 1,
				resource: d
			}]
		}), p = e.beginRenderPass({ colorAttachments: [{
			view: this.colorTex.createView(),
			loadOp: "clear",
			storeOp: "store",
			clearValue: [
				0,
				0,
				0,
				1
			]
		}] });
		return p.setPipeline(this.colorPipe), p.setBindGroup(0, l), p.setBindGroup(1, f), p.draw(3), p.end(), !0;
	}
	dummyRaw = null;
	dummyRawView() {
		return this.dummyRaw ??= this.device.createTexture({
			size: [1, 1],
			format: "rgba32float",
			usage: GPUTextureUsage.TEXTURE_BINDING
		}), this.dummyRaw.createView();
	}
	encodeGeo(e, t, n, r) {
		let i = this.geometry, a = i.straighten * Math.PI / 180, o = (i.rotate90 % 4 + 4) % 4, s = o % 2 ? t.h : t.w, c = o % 2 ? t.w : t.h, l = i.box ?? [
			0,
			0,
			s,
			c
		], u = Math.abs(i.straighten) < 1e-9;
		this.device.queue.writeBuffer(this.geoUniform, 0, new Float32Array([
			t.w,
			t.h,
			s,
			c,
			u ? 1 : Math.cos(a),
			u ? 0 : Math.sin(a),
			o,
			+!!i.flip,
			l[0],
			l[1],
			l[2],
			l[3],
			0,
			0,
			0,
			0
		]));
		let d = e.beginRenderPass({ colorAttachments: [{
			view: this.finalTex.createView(),
			loadOp: "clear",
			storeOp: "store",
			clearValue: [
				0,
				0,
				0,
				1
			]
		}] });
		d.setPipeline(this.geoPipe), d.setBindGroup(0, this.device.createBindGroup({
			layout: this.geoPipe.getBindGroupLayout(0),
			entries: [{
				binding: 0,
				resource: { buffer: this.geoUniform }
			}, {
				binding: 1,
				resource: this.colorTex.createView()
			}]
		})), d.draw(3), d.end();
	}
	encodePresent(e, t, n) {
		let r = this.canvas, i = window.devicePixelRatio || 1, a = Math.max(1, Math.round(r.clientWidth * i)), o = Math.max(1, Math.round(r.clientHeight * i));
		(r.width !== a || r.height !== o) && (r.width = a, r.height = o);
		let s = Math.max(t / a, n / o), c = (a - t / s) / 2, l = (o - n / s) / 2;
		this.device.queue.writeBuffer(this.presentUniform, 0, new Float32Array([
			s,
			c,
			l,
			d,
			t,
			n,
			0,
			0
		]));
		let u = e.beginRenderPass({ colorAttachments: [{
			view: this.context.getCurrentTexture().createView(),
			loadOp: "clear",
			storeOp: "store",
			clearValue: [
				d,
				d,
				d,
				1
			]
		}] });
		u.setPipeline(this.presentPipe), u.setBindGroup(0, this.device.createBindGroup({
			layout: this.presentPipe.getBindGroupLayout(0),
			entries: [
				{
					binding: 0,
					resource: { buffer: this.presentUniform }
				},
				{
					binding: 1,
					resource: this.finalTex.createView()
				},
				{
					binding: 2,
					resource: this.sampler
				}
			]
		})), u.draw(3), u.end();
	}
	frame() {
		let e = this.current;
		if (!e || !this.context || this.lost) return null;
		let t = performance.now(), n = this.finalSize();
		this.ensureTextures(e, n.w, n.h);
		let r = this.device.createCommandEncoder(), i = this.dirtyColor;
		if (this.dirtyColor) {
			if (!this.encodeColor(r, e)) return null;
			this.dirtyColor = !1, this.dirtyGeo = !0;
		}
		this.dirtyGeo &&= (this.encodeGeo(r, e, n.w, n.h), !1), this.encodePresent(r, n.w, n.h), this.device.queue.submit([r.finish()]);
		let a = {
			cpuMs: performance.now() - t,
			color: i,
			submitted: performance.now()
		};
		return this.onFrame?.(a), a;
	}
	redraw() {
		this.schedule();
	}
	async readback() {
		let e = this.current;
		if (!e) throw Error("没有照片");
		let t = this.finalSize();
		this.ensureTextures(e, t.w, t.h);
		let n = this.device, r = n.createCommandEncoder();
		if (!this.encodeColor(r, e)) throw Error("配方表或 LUT 还没到");
		this.encodeGeo(r, e, t.w, t.h);
		let i = Math.ceil(t.w * 4 / 256) * 256, a = n.createBuffer({
			size: i * t.h,
			usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ
		});
		r.copyTextureToBuffer({ texture: this.finalTex }, {
			buffer: a,
			bytesPerRow: i
		}, [t.w, t.h]), n.queue.submit([r.finish()]), await a.mapAsync(GPUMapMode.READ);
		let o = new Uint8Array(a.getMappedRange()), s = new Uint8Array(t.w * t.h * 4);
		for (let e = 0; e < t.h; e++) s.set(o.subarray(e * i, e * i + t.w * 4), e * t.w * 4);
		return a.unmap(), a.destroy(), this.dirtyColor = !0, {
			w: t.w,
			h: t.h,
			data: s
		};
	}
	async bench(e = 30) {
		let t = this.current;
		if (!t) return NaN;
		let n = this.finalSize();
		this.ensureTextures(t, n.w, n.h), await this.device.queue.onSubmittedWorkDone();
		let r = performance.now();
		for (let r = 0; r < e; r++) {
			let e = this.device.createCommandEncoder();
			this.encodeColor(e, t), this.encodeGeo(e, t, n.w, n.h), this.device.queue.submit([e.finish()]);
		}
		return await this.device.queue.onSubmittedWorkDone(), this.dirtyColor = !0, (performance.now() - r) / e;
	}
	stats() {
		let e = 0;
		for (let t of this.sources.values()) e += t.bytes;
		return {
			sources: this.sources.size,
			sourceBytes: e,
			bakes: this.bakes.size,
			luts: this.luts.size
		};
	}
	dispose() {
		this.detach();
		for (let e of this.sources.values()) e.tex.destroy();
		for (let e of this.bakes.values()) e.buf.destroy();
		for (let e of this.luts.values()) e.tex.destroy();
		this.sources.clear(), this.bakes.clear(), this.luts.clear(), this.device.destroy();
	}
};
export { p as Engine, r as TABLE_SIZE, i as channelGains, u as displayTable, c as pchip };
