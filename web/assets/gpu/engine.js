var e=.35,t=.25,n=.13,r=1021;function i(n){let r=n.exposure_ev??0,i=(n.wb?.warm??0)/100,a=(n.wb?.tint??0)/100;return r===0&&i===0&&a===0?null:[2**(r+e*i+t*a),2**r,2**(r-e*i+t*a)]}function a(e){return!e.contrast&&!e.highlight&&!e.shadow}function o(e,t){let r=e;if(t.contrast){let e=t.contrast/100;r+=e*(r*r*(3-2*r)-r)}return t.highlight&&(r+=t.highlight/100*n*6.75*r*r*(1-r)),t.shadow&&(r+=t.shadow/100*n*6.75*r*(1-r)**2),Math.min(1,Math.max(0,r))}function s(e,t,n,r){let i=((2*e+t)*n-e*r)/(e+t);return Math.sign(i)===Math.sign(n)?Math.sign(n)!==Math.sign(r)&&Math.abs(i)>Math.abs(3*n)&&(i=3*n):i=0,i}function c(e,t){let n=e.length,r=[],i=[];for(let a=0;a<n-1;a++)r.push(e[a+1]-e[a]),i.push((t[a+1]-t[a])/r[a]);let a=Array(n).fill(0);if(n===2)a[0]=i[0],a[1]=i[0];else{for(let e=1;e<n-1;e++){if(i[e-1]*i[e]<=0)continue;let t=2*r[e]+r[e-1],n=r[e]+2*r[e-1];a[e]=(t+n)/(t/i[e-1]+n/i[e])}a[0]=s(r[0],r[1],i[0],i[1]),a[n-1]=s(r[n-2],r[n-3],i[n-2],i[n-3])}return i=>{let o=0;for(;o<n-2&&i>e[o+1];)o++;let s=(i-e[o])/r[o],c=s*s,l=c*s;return(2*l-3*c+1)*t[o]+(l-2*c+s)*r[o]*a[o]+(-2*l+3*c)*t[o+1]+(l-c)*r[o]*a[o+1]}}function l(e){return!e||e.length<2||e.every(([e,t])=>e===t)?null:e}function u(e,t){let n=l(t);if(a(e)&&!n)return null;let i=new Float32Array(r),s=n?n.map(e=>e[0]):[],u=n?c(s,n.map(e=>e[1])):null;for(let t=0;t<r;t++){let n=o(255*t/1020/255,e)*255;u&&(n=Math.min(255,Math.max(0,u(Math.min(s[s.length-1],Math.max(s[0],n)))))),i[t]=n}return i}function d(e){return e.vertical!==0||e.horizontal!==0||e.distortion!==0}var f=Math.PI/180;function p(e){let t=1/Math.tan(13.5*f);return Math.tan(e/100*(30*f))/t}function m(e,t,n){let r=e.straighten*f;return{radial:-(e.distortion/100)*.35,sv:p(e.vertical),sh:-p(e.horizontal),cos:Math.cos(r),sin:Math.sin(r),halfDiag:Math.hypot(t,n)/2}}function h(e,t){let n=e/(t-1),r=Array.from({length:t},(e,t)=>t*n);return r[t-1]=e,r}function g(e,t,n){let{radial:r,sv:i,sh:a,cos:o,sin:s,halfDiag:c}=m(e,t,n),l=[...h(t-1,80),...h(t-1,80),...Array(40).fill(0),...Array(40).fill(t-1)],u=[...Array(80).fill(0),...Array(80).fill(n-1),...h(n-1,40),...h(n-1,40)],d=1/0,f=1/0,p=-1/0,g=-1/0;for(let e=0;e<l.length;e++){let m=l[e]-(t-1)/2,h=u[e]-(n-1)/2,_=Math.hypot(m,h)/c,v=1+r*_*_;m*=v,h*=v;let y=m/(t/2),b=h/(n/2),x=1+i*b+a*y;y/=x,b/=x,m=t/2*y,h=n/2*b;let S=o*m-s*h,C=s*m+o*h;S<d&&(d=S),S>p&&(p=S),C<f&&(f=C),C>g&&(g=C)}let _=Math.floor(d-1.5),v=Math.floor(f-1.5);return{minx:_,miny:v,w:Math.max(1,Math.ceil(p+1.5)-_),h:Math.max(1,Math.ceil(g+1.5)-v)}}function _(e,t,n){return d(e)?{...g(e,t,n),expanded:!0}:{minx:0,miny:0,w:t,h:n,expanded:!1}}function v(e,t,n,r,i,a,o=m(e,t,n)){let s=r.minx+i+.5,c=r.miny+a+.5,l=o.cos*s+o.sin*c,u=-o.sin*s+o.cos*c,d=l/(t/2),f=u/(n/2),p=1-o.sh*d-o.sv*f;d/=p,f/=p;let h=t/2*d,g=n/2*f,_=Math.hypot(h,g)/o.halfDiag,v=_;if(Math.abs(o.radial)>=1e-8)for(let e=0;e<8;e++){let e=o.radial*v**3+v-_,t=3*o.radial*v**2+1;v-=e/(Math.abs(t)<1e-6?1:t)}let y=_>1e-8?v/_:1;return[h*y+(t-1)/2,g*y+(n-1)/2]}function y(e,t,n,r){return n>=-.001&&r>=-.001&&n<=e-1+.001&&r<=t-1+.001}function b(e,t){return t%2?{w:e.h,h:e.w}:{w:e.w,h:e.h}}var x=240;function S(e){let t=Math.floor(e),n=e-t;return n<.5?t:n>.5?t+1:t%2==0?t:t+1}function C(e,t){return h(e-1,t).map(e=>Math.trunc(e))}function w(e,t,n){let r=new Int32Array(t),i=[0,0,0,0],a=0,o=[];for(let s=0;s<n;s++){for(let n=0;n<t;n++)r[n]=e[s*t+n]?r[n]+1:0;o.length=0,o.push(-1);for(let e=0;e<=t;e++){let n=e<t?r[e]:0;for(;o[o.length-1]!==-1&&r[o[o.length-1]]>n;){let t=r[o.pop()],n=o[o.length-1],c=e-n-1;t*c>a&&(a=t*c,i=[n+1,s-t+1,c,t])}o.push(e)}}return i}function T(e,t,n){let r=(e.rotate90%4+4)%4,i=_(e,t,n),{w:a,h:o}=b(i,r),s=m(e,t,n),c=(o,c)=>{let l=e.flip?a-1-c:c,u=l,d=o;r===1?(u=i.w-1-o,d=l):r===2?(u=i.w-1-l,d=i.h-1-o):r===3&&(u=o,d=i.h-1-l);let[f,p]=i.expanded?v(e,t,n,i,u,d,s):E(e,t,n,u,d);return y(t,n,f,p)},l=Math.max(o,a);if(l<=x){let e=new Uint8Array(a*o),t=!0;for(let n=0;n<o;n++)for(let r=0;r<a;r++)t=(e[n*a+r]=+!!c(n,r))!=0&&t;if(t)return[0,0,1,1];let[n,r,i,s]=w(e,a,o);return i<=0||s<=0?[0,0,1,1]:[n/a,r/o,i/a,s/o]}let u=x/l,d=Math.max(2,S(o*u)),f=Math.max(2,S(a*u)),p=C(o,d),h=C(a,f),g=new Uint8Array(f*d);for(let e=0;e<d;e++)for(let t=0;t<f;t++)g[e*f+t]=+!!c(p[e],h[t]);let[T,D,O,k]=w(g,f,d);if(O<=0||k<=0)return[0,0,1,1];let A=Math.trunc(T/f*a)+1,j=Math.trunc(D/d*o)+1,M=Math.max(A+1,Math.trunc((T+O)/f*a)-1),N=Math.max(j+1,Math.trunc((D+k)/d*o)-1);return[A/a,j/o,(M-A)/a,(N-j)/o]}function E(e,t,n,r,i){if(Math.abs(e.straighten)<1e-9)return[r,i];let a=e.straighten*f,o=Math.cos(a),s=Math.sin(a),c=(t-1)/2,l=(n-1)/2,u=r-c,d=i-l;return[o*u+s*d+c,-s*u+o*d+l]}var D={shadowMax:2,highlightMin:253},O=262144,k=1026;function A(e,t,n=O){let r=Math.max(1,Math.floor(Math.sqrt(e*t/n)));for(;Math.ceil(e/r)*Math.ceil(t/r)>n;)r+=1;return r}function j(e,t,n){if(!n)return[0,0,e,t];let r=(e,t,n)=>Math.min(n,Math.max(t,e)),i=r(Math.round(n[0]*e),0,e-1),a=r(Math.round(n[1]*t),0,t-1);return[i,a,r(Math.round((n[0]+n[2])*e),i+1,e),r(Math.round((n[1]+n[3])*t),a+1,t)]}var M=72,N=640<<20,P=42/255;function F(e,t){return b(_(e,t.w,t.h),(e.rotate90%4+4)%4)}function I(e,t){return e.box?{w:e.box[2],h:e.box[3]}:F(e,t)}function L(e,t){return e.map(e=>[0,1,2].map(n=>e[0]*t[0][n]+e[1]*t[1][n]+e[2]*t[2][n]))}var R=class e{device;info;sources=/* @__PURE__ */ new Map;bakes=/* @__PURE__ */ new Map;luts=/* @__PURE__ */ new Map;canvas=null;context=null;format;colorPipe;geoPipe;presentPipe;binPipe;histPipe;uniform;geoUniform;presentUniform;histUniform;histTotal;histStage;toneBuf;dummyLut;dummyLut8;dummy2d;sampler;colorTex=null;finalTex=null;current=null;look={recipe:null,lut:null,lutStrength:1,adjust:{},curve:null};geometry={straighten:0,rotate90:0,flip:!1,vertical:0,horizontal:0,distortion:0,box:null};dirtyColor=!0;dirtyGeo=!0;raf=0;histBusy=!1;clipOverlay={shadow:!1,highlight:!1};displayAspect=null;version=0;lost=!1;onFrame=null;onLost=null;static async create(){if(!(`gpu`in navigator)||!navigator.gpu)return null;let t=await navigator.gpu.requestAdapter({powerPreference:`high-performance`});if(!t)return null;let n=await t.requestDevice({requiredLimits:{maxStorageBufferBindingSize:t.limits.maxStorageBufferBindingSize,maxBufferSize:t.limits.maxBufferSize}}),r=new e(n,t);for(let e of await r.compileMessages())if(e.type===`error`)throw Error(`WGSL ${e.lineNum}: ${e.message}`);return r}modules;constructor(e,t){this.device=e;let n=t.info;this.info={vendor:n.vendor,architecture:n.architecture,description:n.description,fallback:!!n.isFallbackAdapter},this.format=navigator.gpu.getPreferredCanvasFormat(),e.lost.then(e=>{this.lost=!0,this.onLost?.(e.message||e.reason||`lost`)});let i=t=>e.createShaderModule({code:t}),a=i(`// 颜色链：逐项对应 src/gxr_studio 里的 Python 实现（文件名:函数写在注释里），不另发明调整。
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
@group(0) @binding(3) var lut3: texture_3d<f32>;             // Log 类 LUT：.cube 3D 表，x=R、y=G、z=B
@group(0) @binding(4) var lut8: texture_3d<f32>;             // 显示类 LUT：256³ 精确表（服务器用 lut.photo._apply 逐个算好），rgba8unorm

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
    var looked: vec3f;
    if (mode == 2u) {
      base = q8(enc3(clamp(codes / 4095.0, vec3f(0.0), vec3f(1.0)), 0u) * 255.0);
      looked = lutRender(mat3x(codes, p.inM0, p.inM1, p.inM2));  // 码值 → 场景 XYZ → LUT 输入色域
    } else {
      // 显示类：底图是 8 位，查精确表（与 _apply 逐像素相同，包括取整平局）
      base = select(recipeChain(codes, vec3f(1.0), false), jpg, mode == 4u);
      looked = floor(textureLoad(lut8, vec3i(base), 0).xyz * 255.0 + 0.5);
    }
    u = q8(mix(base, looked, p.flags.w));
    if (p.flags.z > 0.5) { u = applyBase(u); }
  }
  return applyExtra(u, pix, p.dims.xy);
}
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
`),o=i(`// 第二遍：纠正（拉直，或透视 / 畸变）→ 90° 旋转 / 水平翻转 → 裁切框，1:1 写进成片纹理（读回和 Python 逐像素比较的就是它）。
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
`),s=i(`// 第三遍：成片纹理 → 画布。按显示比例缩放；缩小时 2×2 超采样，避免锯齿。只影响显示，不影响读回。
// 溢出提示（J）：把暗部压死的点盖成蓝色、高光溢出的点盖成红色。判定与 histogram_math.js 的 isCrushed / isBlown 相同
// （码值 ≤ clip.x 或 ≥ clip.y），颜色与 SHADOW_RGB / HIGHLIGHT_RGB 相同。
struct V {
  map: vec4f,   // 画布像素 → 成片像素：横向缩放、偏移 x、偏移 y、背景灰度
  size: vec4f,  // 成片宽、高、纵向缩放（和横向相同，除非成片按另一个宽高比显示）
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
  let sc = vec2f(v.map.x, v.size.z);
  let s = max(sc.x, sc.y);
  let q = (pos.xy - v.map.yz) * sc;  // 成片像素坐标（左上角为 0）
  if (q.x < 0.0 || q.y < 0.0 || q.x > v.size.x || q.y > v.size.y) { return vec4f(vec3f(v.map.w), 1.0); }
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
  return vec4f(c, 1.0);
}
`),c=i(`// RAW 条带字节 → 半分辨率白平衡码值（raw/codes.py half_res_codes：裁切窗口内每个 2×2 同色取平均 / AsShotNeutral × 4095，不去马赛克），
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
`),l=i(`// 直方图：在成片纹理上按网格取样，数红、绿、蓝、亮度各 256 档，再数"暗部压死""高光溢出"的点。
// 取样点 = 矩形 (x0, y0) 起，横竖每隔 stride 个像素一个，共 nx × ny 个；取法、亮度公式和溢出判定
// 与 web/assets/histogram_math.js 的 countRGBA 完全一致（tests/test_histogram_gpu.py 用 numpy 逐个核对）。
// 每个工作组先在组内数一份，最后再并进全局，全局原子操作不会成为瓶颈。
struct H {
  grid: vec4u,   // x0、y0、nx、ny
  rule: vec4u,   // 间隔、暗部上限、高光下限、0
};
@group(0) @binding(0) var<uniform> u: H;
@group(0) @binding(1) var src: texture_2d<f32>;
@group(0) @binding(2) var<storage, read_write> total: array<atomic<u32>, 1026>;
var<workgroup> part: array<atomic<u32>, 1026>;

// 派发 8 × 8 个工作组、每组 16 × 16 个线程 = 128 × 128 个线程，每个线程隔 128 个格点取一个
const THREADS: u32 = 128u;

@compute @workgroup_size(16, 16) fn main(
  @builtin(local_invocation_index) li: u32,
  @builtin(local_invocation_id) lid: vec3u,
  @builtin(workgroup_id) wg: vec3u,
) {
  for (var i = li; i < 1026u; i += 256u) { atomicStore(&part[i], 0u); }
  workgroupBarrier();
  let stride = u.rule.x;
  for (var j = wg.y * 16u + lid.y; j < u.grid.w; j += THREADS) {
    for (var i = wg.x * 16u + lid.x; i < u.grid.z; i += THREADS) {
      let p = vec2i(vec2u(u.grid.x + i * stride, u.grid.y + j * stride));
      let c = vec3u(round(textureLoad(src, p, 0).xyz * 255.0));
      atomicAdd(&part[c.x], 1u);
      atomicAdd(&part[256u + c.y], 1u);
      atomicAdd(&part[512u + c.z], 1u);
      atomicAdd(&part[768u + (2126u * c.x + 7152u * c.y + 722u * c.z + 5000u) / 10000u], 1u);
      if (max(c.x, max(c.y, c.z)) <= u.rule.y) { atomicAdd(&part[1024], 1u); }
      if (min(c.x, min(c.y, c.z)) >= u.rule.z) { atomicAdd(&part[1025], 1u); }
    }
  }
  workgroupBarrier();
  for (var i = li; i < 1026u; i += 256u) {
    let n = atomicLoad(&part[i]);
    if (n != 0u) { atomicAdd(&total[i], n); }
  }
}
`);this.modules=[a,o,s,c,l],this.colorPipe=e.createRenderPipeline({layout:`auto`,vertex:{module:a,entryPoint:`vs`},fragment:{module:a,entryPoint:`fs`,targets:[{format:`rgba8unorm`}]}}),this.geoPipe=e.createRenderPipeline({layout:`auto`,vertex:{module:o,entryPoint:`vs`},fragment:{module:o,entryPoint:`fs`,targets:[{format:`rgba8unorm`}]}}),this.presentPipe=e.createRenderPipeline({layout:`auto`,vertex:{module:s,entryPoint:`vs`},fragment:{module:s,entryPoint:`fs`,targets:[{format:this.format}]}}),this.binPipe=e.createComputePipeline({layout:`auto`,compute:{module:c,entryPoint:`main`}}),this.histPipe=e.createComputePipeline({layout:`auto`,compute:{module:l,entryPoint:`main`}});let u=t=>e.createBuffer({size:t,usage:GPUBufferUsage.UNIFORM|GPUBufferUsage.COPY_DST});this.uniform=u(288),this.geoUniform=u(80),this.presentUniform=u(48),this.histUniform=u(32),this.histTotal=e.createBuffer({size:k*4,usage:GPUBufferUsage.STORAGE|GPUBufferUsage.COPY_SRC|GPUBufferUsage.COPY_DST}),this.histStage=e.createBuffer({size:k*4,usage:GPUBufferUsage.MAP_READ|GPUBufferUsage.COPY_DST}),this.toneBuf=e.createBuffer({size:r*4,usage:GPUBufferUsage.STORAGE|GPUBufferUsage.COPY_DST}),this.dummyLut=e.createTexture({size:[2,2,2],dimension:`3d`,format:`rgba32float`,usage:GPUTextureUsage.TEXTURE_BINDING}),this.dummyLut8=e.createTexture({size:[1,1,1],dimension:`3d`,format:`rgba8unorm`,usage:GPUTextureUsage.TEXTURE_BINDING}),this.dummy2d=e.createTexture({size:[1,1],format:`rgba8unorm`,usage:GPUTextureUsage.TEXTURE_BINDING}),this.sampler=e.createSampler({magFilter:`linear`,minFilter:`linear`})}async compileMessages(){let e=[];for(let t of this.modules)e.push(...(await t.getCompilationInfo()).messages);return e}get isLost(){return this.lost}hasSource(e){return this.sources.has(e)}sourceSize(e){let t=this.sources.get(e);return t?{w:t.w,h:t.h}:null}hasBake(e){return this.bakes.has(e)}hasLut(e){return this.luts.has(e)}async addRaw(e,t,n,r){let i=this.device,a=t.strips.reduce((e,[,t])=>e+t,0),o=new Uint8Array(Math.ceil(a/4)*4),s=0;for(let[e,r]of t.strips)o.set(new Uint8Array(n,e,r),s),s+=r;let c=i.createBuffer({size:o.byteLength,usage:GPUBufferUsage.STORAGE|GPUBufferUsage.COPY_DST});i.queue.writeBuffer(c,0,o);let l=Math.floor(t.crop[3]/2),u=Math.floor(t.crop[2]/2),d=t.orientation>=5,f=d?u:l,p=d?l:u,m=i.createTexture({size:[f,p],format:`rgba32float`,usage:GPUTextureUsage.TEXTURE_BINDING|GPUTextureUsage.STORAGE_BINDING}),h=/* @__PURE__ */ new Float32Array(24);h.set(t.crop,0),h.set(t.wb,4),h.set([t.row_bytes,t.bps,+!!t.big_endian,t.orientation],8),h.set(t.phase,12),h.set(t.black,16),h.set([t.white,t.active[0],t.active[1],0],20);let g=i.createBuffer({size:h.byteLength,usage:GPUBufferUsage.UNIFORM|GPUBufferUsage.COPY_DST});i.queue.writeBuffer(g,0,h);let _=i.createCommandEncoder(),v=_.beginComputePass();v.setPipeline(this.binPipe),v.setBindGroup(0,i.createBindGroup({layout:this.binPipe.getBindGroupLayout(0),entries:[{binding:0,resource:{buffer:g}},{binding:1,resource:{buffer:c}},{binding:2,resource:m.createView()}]})),v.dispatchWorkgroups(Math.ceil(f/16),Math.ceil(p/16)),v.end(),i.queue.submit([_.finish()]),await i.queue.onSubmittedWorkDone(),c.destroy(),g.destroy(),this.putSource({key:e,kind:`raw`,tex:m,w:f,h:p,bytes:f*p*16,scene:r})}addJpg(e,t){let n=this.device.createTexture({size:[t.width,t.height],format:`rgba8unorm`,usage:GPUTextureUsage.TEXTURE_BINDING|GPUTextureUsage.COPY_DST|GPUTextureUsage.RENDER_ATTACHMENT});this.device.queue.copyExternalImageToTexture({source:t},{texture:n},[t.width,t.height]),this.putSource({key:e,kind:`jpg`,tex:n,w:t.width,h:t.height,bytes:t.width*t.height*4,scene:null})}putSource(e){this.sources.get(e.key)?.tex.destroy(),this.sources.delete(e.key),this.sources.set(e.key,e);let t=0;for(let e of this.sources.values())t+=e.bytes;for(let[n,r]of this.sources)t<=N||r===e||r===this.current||(r.tex.destroy(),this.sources.delete(n),t-=r.bytes)}addBake(e,t,n){let r=this.bakes.get(e);r&&(r.buf.destroy(),this.bakes.delete(e));let i=this.device.createBuffer({size:n.byteLength,usage:GPUBufferUsage.STORAGE|GPUBufferUsage.COPY_DST});this.device.queue.writeBuffer(i,0,n),this.bakes.set(e,{meta:t,buf:i}),this.trim(this.bakes,64,e=>e.buf.destroy(),this.look.recipe)}addLut(e,t,n){let r=this.luts.get(e);r&&(r.tex.destroy(),this.luts.delete(e));let i=t.n,a=!!t.table8,o=this.device.createTexture({size:[i,i,i],dimension:`3d`,format:a?`rgba8unorm`:`rgba32float`,usage:GPUTextureUsage.TEXTURE_BINDING|GPUTextureUsage.COPY_DST});this.device.queue.writeTexture({texture:o},n,{bytesPerRow:i*(a?4:16),rowsPerImage:i},[i,i,i]),this.luts.set(e,{meta:t,tex:o}),this.trim(this.luts,3,e=>e.tex.destroy())}trim(e,t,n,r=null){for(let[i,a]of e){if(e.size<=t)break;i!==r&&(n(a),e.delete(i))}}attach(e){this.canvas!==e&&(this.canvas=e,this.context=e.getContext(`webgpu`),this.context?.configure({device:this.device,format:this.format,alphaMode:`opaque`}),this.dirtyGeo=!0,this.schedule())}detach(){this.raf&&cancelAnimationFrame(this.raf),this.raf=0,this.context?.unconfigure(),this.canvas=null,this.context=null}show(e){let t=this.sources.get(e);return t?(this.sources.delete(e),this.sources.set(e,t),this.current!==t&&(this.current=t,this.dirtyColor=!0,this.dirtyGeo=!0,this.schedule()),!0):!1}setLook(e){Object.assign(this.look,e),this.dirtyColor=!0,this.dirtyGeo=!0,this.schedule()}setGeometry(e){Object.assign(this.geometry,e),this.dirtyGeo=!0,this.schedule()}setDisplayAspect(e){let t=e!==null&&Number.isFinite(e)&&e>0?e:null;t!==this.displayAspect&&(this.displayAspect=t,this.schedule())}currentKey(){return this.current?this.current.key:null}layerSize(){return this.current?{w:this.current.w,h:this.current.h}:null}canvasSize(){let e=this.current;return e?F(this.geometry,e):null}finalSize(){let e=this.current;return e?I(this.geometry,e):null}schedule(){this.raf||!this.canvas||this.lost||(this.raf=requestAnimationFrame(()=>{this.raf=0,this.frame()}))}lutState(e,t=this.look){let n=t.lut?this.luts.get(t.lut)??null:null;return e.kind===`jpg`?{mode:n?4:3,lut:n}:n?{mode:n.meta.display?1:2,lut:n}:{mode:0,lut:null}}writeUniform(e,t,n,r,a=this.look){let o=a.adjust,s=new Float32Array(M),c=i(o)??[1,1,1];s.set([...c,0],0);let l=u(o,a.curve);l&&this.device.queue.writeBuffer(this.toneBuf,0,l);let d=!!(o.exposure_ev||o.wb?.warm||o.wb?.tint||l);if(s.set([+!!l,r,+!!d,a.lutStrength],4),s.set([o.black_point??0,o.saturation??0,o.vibrance??0,o.vignette??0],8),s.set([e.w,e.h,0,0],12),t&&(s.set([t.meta.origin[0],t.meta.origin[1],1/t.meta.step,t.meta.nx],16),s.set([t.meta.ny,0,0,0],20)),n&&!n.meta.table8){let t=n.meta;s.set([t.input_space_id,t.output_space_id,t.n,+!!t.input_legal],24),s.set([...t.domain_min,0],28),s.set([...t.domain_max,0],32),s.set([t.range3[0],t.range3[1],+!!t.output_legal,0],36);let r=[[0,0,0],[0,0,0],[0,0,0]],i=[0,0,0];if(e.scene){r=L(t.xyz_to_input,e.scene.A);let n=e.scene.b;i=t.xyz_to_input.map(e=>e[0]*n[0]+e[1]*n[1]+e[2]*n[2])}for(let e=0;e<3;e++)s.set([...r[e],i[e]],40+e*4);for(let e=0;e<3;e++)s.set([...t.out_to_srgb[e],0],52+e*4)}this.device.queue.writeBuffer(this.uniform,0,s)}ensureTextures(e,t,n){let r=this.device;this.colorTex&&this.colorTex.width===e.w&&this.colorTex.height===e.h||(this.colorTex?.destroy(),this.colorTex=r.createTexture({size:[e.w,e.h],format:`rgba8unorm`,usage:GPUTextureUsage.RENDER_ATTACHMENT|GPUTextureUsage.TEXTURE_BINDING}),this.dirtyColor=!0),this.finalTex&&this.finalTex.width===t&&this.finalTex.height===n||(this.finalTex?.destroy(),this.finalTex=r.createTexture({size:[t,n],format:`rgba8unorm`,usage:GPUTextureUsage.RENDER_ATTACHMENT|GPUTextureUsage.TEXTURE_BINDING|GPUTextureUsage.COPY_SRC}),this.dirtyGeo=!0)}encodeColor(e,t,n=this.look,r=this.colorTex){let{mode:i,lut:a}=this.lutState(t,n),o=i===0||i===1,s=n.recipe?this.bakes.get(n.recipe)??null:null;if(o&&!s||n.lut&&!a||i===2&&!t.scene)return!1;this.writeUniform(t,s,a,i,n);let c=this.device,l=s??this.bakes.values().next().value,u=l?l.buf:this.toneBuf,d=c.createBindGroup({layout:this.colorPipe.getBindGroupLayout(0),entries:[{binding:0,resource:{buffer:this.uniform}},{binding:1,resource:{buffer:u}},{binding:2,resource:{buffer:this.toneBuf}},{binding:3,resource:(a&&!a.meta.table8?a.tex:this.dummyLut).createView({dimension:`3d`})},{binding:4,resource:(a&&a.meta.table8?a.tex:this.dummyLut8).createView({dimension:`3d`})}]}),f=t.kind===`raw`?t.tex.createView():this.dummyRawView(),p=t.kind===`jpg`?t.tex.createView():this.dummy2d.createView(),m=c.createBindGroup({layout:this.colorPipe.getBindGroupLayout(1),entries:[{binding:0,resource:f},{binding:1,resource:p}]}),h=e.beginRenderPass({colorAttachments:[{view:r.createView(),loadOp:`clear`,storeOp:`store`,clearValue:[0,0,0,1]}]});return h.setPipeline(this.colorPipe),h.setBindGroup(0,d),h.setBindGroup(1,m),h.draw(3),h.end(),!0}dummyRaw=null;dummyRawView(){return this.dummyRaw??=this.device.createTexture({size:[1,1],format:`rgba32float`,usage:GPUTextureUsage.TEXTURE_BINDING}),this.dummyRaw.createView()}encodeGeo(e,t,n,r,i=this.colorTex,a=this.finalTex,o=this.geometry){let s=o.straighten*Math.PI/180,c=(o.rotate90%4+4)%4,l=_(o,t.w,t.h),{w:u,h:d}=b(l,c),f=o.box??[0,0,u,d],p=Math.abs(o.straighten)<1e-9,h=m(o,t.w,t.h);this.device.queue.writeBuffer(this.geoUniform,0,new Float32Array([t.w,t.h,u,d,p?1:Math.cos(s),p?0:Math.sin(s),c,+!!o.flip,f[0],f[1],f[2],f[3],h.radial,h.sv,h.sh,h.halfDiag,l.minx,l.miny,+!!l.expanded,0]));let g=e.beginRenderPass({colorAttachments:[{view:a.createView(),loadOp:`clear`,storeOp:`store`,clearValue:[0,0,0,1]}]});g.setPipeline(this.geoPipe),g.setBindGroup(0,this.device.createBindGroup({layout:this.geoPipe.getBindGroupLayout(0),entries:[{binding:0,resource:{buffer:this.geoUniform}},{binding:1,resource:i.createView()}]})),g.draw(3),g.end()}encodePresent(e,t,n){let r=this.canvas,i=window.devicePixelRatio||1,a=Math.max(1,Math.round(r.clientWidth*i)),o=Math.max(1,Math.round(r.clientHeight*i));(r.width!==a||r.height!==o)&&(r.width=a,r.height=o),this.presentTo(e,this.context.getCurrentTexture().createView(),a,o,t,n)}presentTo(e,t,n,r,i,a,o=this.finalTex,s=this.clipOverlay,c=this.displayAspect){let l=Math.max(i/n,a/r),u=l,d=(n-i/l)/2,f=(r-a/u)/2;if(c!==null){let e=Math.min(n,r*c),t=e/c;l=i/e,u=a/t,d=(n-e)/2,f=(r-t)/2}this.device.queue.writeBuffer(this.presentUniform,0,new Float32Array([l,d,f,P,i,a,u,0,D.shadowMax+.5,D.highlightMin-.5,+!!s.shadow,+!!s.highlight]));let p=e.beginRenderPass({colorAttachments:[{view:t,loadOp:`clear`,storeOp:`store`,clearValue:[P,P,P,1]}]});p.setPipeline(this.presentPipe),p.setBindGroup(0,this.device.createBindGroup({layout:this.presentPipe.getBindGroupLayout(0),entries:[{binding:0,resource:{buffer:this.presentUniform}},{binding:1,resource:o.createView()},{binding:2,resource:this.sampler}]})),p.draw(3),p.end()}frame(){let e=this.current;if(!e||!this.context||this.lost)return null;let t=performance.now(),n=this.finalSize();this.ensureTextures(e,n.w,n.h);let r=this.device.createCommandEncoder(),i=this.dirtyColor;if(this.dirtyColor){if(!this.encodeColor(r,e))return null;this.dirtyColor=!1,this.dirtyGeo=!0}this.dirtyGeo&&(this.encodeGeo(r,e,n.w,n.h),this.dirtyGeo=!1,this.version+=1),this.encodePresent(r,n.w,n.h),this.device.queue.submit([r.finish()]);let a={cpuMs:performance.now()-t,color:i,submitted:performance.now()};return this.onFrame?.(a),a}redraw(){this.schedule()}async readback(){let e=this.current;if(!e)throw Error(`没有照片`);let t=this.finalSize();this.ensureTextures(e,t.w,t.h);let n=this.device,r=n.createCommandEncoder();if(!this.encodeColor(r,e))throw Error(`配方表或 LUT 还没到`);this.encodeGeo(r,e,t.w,t.h);let i=Math.ceil(t.w*4/256)*256,a=n.createBuffer({size:i*t.h,usage:GPUBufferUsage.COPY_DST|GPUBufferUsage.MAP_READ});r.copyTextureToBuffer({texture:this.finalTex},{buffer:a,bytesPerRow:i},[t.w,t.h]),n.queue.submit([r.finish()]),await a.mapAsync(GPUMapMode.READ);let o=new Uint8Array(a.getMappedRange()),s=new Uint8Array(t.w*t.h*4);for(let e=0;e<t.h;e++)s.set(o.subarray(e*i,e*i+t.w*4),e*t.w*4);return a.unmap(),a.destroy(),this.dirtyColor=!0,{w:t.w,h:t.h,data:s}}get contentVersion(){return this.version}get pending(){return this.raf!==0||this.dirtyColor||this.dirtyGeo}setClipOverlay(e){Object.assign(this.clipOverlay,e),this.schedule()}async histogram(e=null,t=O){let n=this.finalTex;if(!n||this.lost||this.histBusy)return null;this.histBusy=!0;try{let r=j(n.width,n.height,e),i=A(r[2]-r[0],r[3]-r[1],t),a=Math.ceil((r[2]-r[0])/i),o=Math.ceil((r[3]-r[1])/i),s=this.version,c=this.device;c.queue.writeBuffer(this.histUniform,0,new Uint32Array([r[0],r[1],a,o,i,D.shadowMax,D.highlightMin,0]));let l=c.createCommandEncoder();l.clearBuffer(this.histTotal);let u=l.beginComputePass();u.setPipeline(this.histPipe),u.setBindGroup(0,c.createBindGroup({layout:this.histPipe.getBindGroupLayout(0),entries:[{binding:0,resource:{buffer:this.histUniform}},{binding:1,resource:n.createView()},{binding:2,resource:{buffer:this.histTotal}}]})),u.dispatchWorkgroups(8,8),u.end(),l.copyBufferToBuffer(this.histTotal,0,this.histStage,0,k*4),c.queue.submit([l.finish()]),await this.histStage.mapAsync(GPUMapMode.READ);let d=new Uint32Array(this.histStage.getMappedRange().slice(0));return this.histStage.unmap(),{counts:d,samples:a*o,stride:i,rect:r,version:s}}catch(e){if(this.lost)return null;throw e}finally{this.histBusy=!1}}async readPresent(e,t){let n=this.current;if(!n)throw Error(`没有照片`);let r=this.finalSize();this.ensureTextures(n,r.w,r.h);let i=this.device,a=i.createCommandEncoder();if(!this.encodeColor(a,n))throw Error(`配方表或 LUT 还没到`);this.encodeGeo(a,n,r.w,r.h);let o=i.createTexture({size:[e,t],format:this.format,usage:GPUTextureUsage.RENDER_ATTACHMENT|GPUTextureUsage.COPY_SRC});this.presentTo(a,o.createView(),e,t,r.w,r.h);let s=await this.readTarget(a,o,e,t);return this.dirtyColor=!0,this.schedule(),{w:e,h:t,data:s}}async readTarget(e,t,n,r){let i=this.device,a=Math.ceil(n*4/256)*256,o=i.createBuffer({size:a*r,usage:GPUBufferUsage.COPY_DST|GPUBufferUsage.MAP_READ});e.copyTextureToBuffer({texture:t},{buffer:o,bytesPerRow:a},[n,r]),i.queue.submit([e.finish()]),await o.mapAsync(GPUMapMode.READ);let s=new Uint8Array(o.getMappedRange()),c=this.format.startsWith(`bgra`),l=new Uint8Array(n*r*4);for(let e=0;e<r;e++)for(let t=0;t<n;t++){let r=e*a+t*4,i=(e*n+t)*4;l[i]=s[r+(c?2:0)],l[i+1]=s[r+1],l[i+2]=s[r+(c?0:2)],l[i+3]=s[r+3]}return o.unmap(),o.destroy(),t.destroy(),l}scratch(e,t,n){return e&&e.width===t&&e.height===n?e:(e?.destroy(),this.device.createTexture({size:[t,n],format:`rgba8unorm`,usage:GPUTextureUsage.RENDER_ATTACHMENT|GPUTextureUsage.TEXTURE_BINDING}))}thumbColor=null;thumbFinal=null;async thumb(e){let t=this.sources.get(e.source);if(!t||this.lost)return null;let n=I(e.geometry,t),r=e.maxEdge,i=this.device;this.thumbColor=this.scratch(this.thumbColor,t.w,t.h),this.thumbFinal=this.scratch(this.thumbFinal,n.w,n.h);let a=i.createCommandEncoder();if(!this.encodeColor(a,t,{...this.look,...e.look},this.thumbColor))return null;this.encodeGeo(a,t,n.w,n.h,this.thumbColor,this.thumbFinal,e.geometry);let o=e.aspect??n.w/n.h,s=Math.max(1,Math.round(o>=1?r:r*o)),c=Math.max(1,Math.round(o>=1?r/o:r)),l=i.createTexture({size:[s,c],format:this.format,usage:GPUTextureUsage.RENDER_ATTACHMENT|GPUTextureUsage.COPY_SRC});return this.presentTo(a,l.createView(),s,c,n.w,n.h,this.thumbFinal,{shadow:!1,highlight:!1},e.aspect),{w:s,h:c,data:await this.readTarget(a,l,s,c)}}async bench(e=30){let t=this.current;if(!t)return NaN;let n=this.finalSize();this.ensureTextures(t,n.w,n.h),await this.device.queue.onSubmittedWorkDone();let r=performance.now();for(let r=0;r<e;r++){let e=this.device.createCommandEncoder();this.encodeColor(e,t),this.encodeGeo(e,t,n.w,n.h),this.device.queue.submit([e.finish()])}return await this.device.queue.onSubmittedWorkDone(),this.dirtyColor=!0,(performance.now()-r)/e}stats(){let e=0;for(let t of this.sources.values())e+=t.bytes;return{sources:this.sources.size,sourceBytes:e,bakes:this.bakes.size,luts:this.luts.size}}dispose(){this.detach(),this.thumbColor?.destroy(),this.thumbFinal?.destroy();for(let e of this.sources.values())e.tex.destroy();for(let e of this.bakes.values())e.buf.destroy();for(let e of this.luts.values())e.tex.destroy();this.sources.clear(),this.bakes.clear(),this.luts.clear(),this.device.destroy()}};export{D as CLIP,R as Engine,O as HIST_MAX_SAMPLES,r as TABLE_SIZE,_ as canvasOf,i as channelGains,u as displayTable,d as expands,c as pchip,j as pixelRegion,b as rotatedSize,A as sampleStride,T as validRect};