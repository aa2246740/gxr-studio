// WebGPU 预览引擎。源图、配方表、LUT 各上传一次；拖动只改 uniform 与 1 或 3 张 1021 点调子表。
// 三遍：颜色（源图分辨率）→ 几何（1:1 成片纹理，读回和 Python 比的就是它）→ 显示（缩放到画布）。
import colorWgsl from './shaders/color.wgsl?raw';
import commonWgsl from './shaders/common.wgsl?raw';
import geoWgsl from './shaders/geo.wgsl?raw';
import binWgsl from './shaders/bin.wgsl?raw';
import presentWgsl from './shaders/present.wgsl?raw';
import histWgsl from './shaders/hist.wgsl?raw';
import { type Adjust, type Curves, channelGains, displayTable, TABLE_SIZE } from './tones';
import { canvasOf, lens, rotatedSize } from './warp';

type Mat3 = [number[], number[], number[]];

/** /api/photos/{id}/gpu 的 raw 部分（RAW 平面的布局，见 gpu/source.py）。 */
export interface RawLayout {
  bps: 12 | 16;
  big_endian: boolean;
  row_bytes: number;
  strips: Array<[number, number]>;
  crop: [number, number, number, number];
  active: [number, number];
  phase: [number, number, number, number];
  black: [number, number, number, number];
  white: number;
  wb: [number, number, number];
  orientation: number;
  width: number;
  height: number;
}

export interface BakeMeta {
  nx: number;
  ny: number;
  origin: [number, number];
  step: number;
}

export interface LutMeta {
  n: number;
  display: boolean;
  /** 显示类：256³ 精确 8 位表（RGBA）。Log 类：float32 3D 表 + 空间参数。 */
  table8?: boolean;
  input_space_id: number;
  output_space_id: number;
  input_legal: boolean;
  output_legal: boolean;
  domain_min: [number, number, number];
  domain_max: [number, number, number];
  range3: [number, number];
  xyz_to_input: Mat3;
  out_to_srgb: Mat3;
}

export interface Scene {
  A: Mat3;
  b: [number, number, number];
}

export interface Look {
  recipe: string | null;
  lut: string | null;
  lutStrength: number;
  adjust: Adjust;
  curve: Curves | null;
}

export interface Geometry {
  straighten: number;
  rotate90: number;
  flip: boolean;
  /** 垂直、水平、畸变的滑块值（−100..100）。任何一个不是 0，画布就按 finish/geometry.py 的 _expanded_bounds 扩大。 */
  vertical: number;
  horizontal: number;
  distortion: number;
  /** 旋转后画布上的裁切框（颜色层像素）。null = 整幅。 */
  box: [number, number, number, number] | null;
}

interface Source {
  key: string;
  kind: 'raw' | 'jpg';
  tex: GPUTexture;
  w: number;
  h: number;
  bytes: number;
  scene: Scene | null;
}

interface Bake {
  meta: BakeMeta;
  buf: GPUBuffer;
}

interface Lut {
  meta: LutMeta;
  tex: GPUTexture;
}

export interface FrameInfo {
  cpuMs: number;
  color: boolean;
  submitted: number;
}

/** 溢出提示要不要盖在画面上（J、点三角）。 */
export interface ClipOverlay {
  shadow: boolean;
  highlight: boolean;
}

/** 一次直方图的结果。bins 排列：红、绿、蓝、亮度各 256 档，后面是暗部压死、高光溢出的点数（共 1026 个）。 */
export interface HistogramData {
  counts: Uint32Array;
  samples: number;
  stride: number;
  /** 取样的像素矩形 [x0, y0, x1, y1)（成片纹理的像素）。 */
  rect: [number, number, number, number];
  /** 数的时候成片纹理的内容版本（Engine.contentVersion）。 */
  version: number;
}

/** 溢出判定（码值）：三个颜色都 ≤ shadowMax = 暗部压死，都 ≥ highlightMin = 高光溢出。与 web/assets/histogram_math.js 相同。 */
export const CLIP = { shadowMax: 2, highlightMin: 253 } as const;
/** 一次最多数多少个点（512 × 512）。与 histogram_math.js 的 MAX_SAMPLES 相同。 */
export const HIST_MAX_SAMPLES = 262144;
const HIST_SIZE = 1026;

/** 横竖每隔 s 个像素取一个，取到的点不超过 maxSamples（与 histogram_math.js 的 sampleStride 相同）。 */
export function sampleStride(width: number, height: number, maxSamples = HIST_MAX_SAMPLES): number {
  let s = Math.max(1, Math.floor(Math.sqrt((width * height) / maxSamples)));
  while (Math.ceil(width / s) * Math.ceil(height / s) > maxSamples) s += 1;
  return s;
}

/** 归一化矩形 [x, y, w, h]（0–1）→ 像素矩形 [x0, y0, x1, y1)，至少一个像素（与 histogram_math.js 的 pixelRegion 相同）。 */
export function pixelRegion(width: number, height: number, region: [number, number, number, number] | null): [number, number, number, number] {
  if (!region) return [0, 0, width, height];
  const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
  const x0 = clamp(Math.round(region[0] * width), 0, width - 1);
  const y0 = clamp(Math.round(region[1] * height), 0, height - 1);
  const x1 = clamp(Math.round((region[0] + region[2]) * width), x0 + 1, width);
  const y1 = clamp(Math.round((region[1] + region[3]) * height), y0 + 1, height);
  return [x0, y0, x1, y1];
}

const UNIFORM_FLOATS = 18 * 4;
const MAX_SOURCE_BYTES = 640 << 20;
// 画布外的空白：设计系统的舞台灰 --stage-dark #2a2a2a
const STAGE_GRAY = 0x2a / 255;

function canvasSizeOf(g: Geometry, src: { w: number; h: number }): { w: number; h: number } {
  return rotatedSize(canvasOf(g, src.w, src.h), ((g.rotate90 % 4) + 4) % 4);
}

function finalSizeOf(g: Geometry, src: { w: number; h: number }): { w: number; h: number } {
  return g.box ? { w: g.box[2], h: g.box[3] } : canvasSizeOf(g, src);
}

function mul3(a: Mat3, b: Mat3): Mat3 {
  return a.map((row) => [0, 1, 2].map((j) => row[0] * b[0][j] + row[1] * b[1][j] + row[2] * b[2][j])) as Mat3;
}

export class Engine {
  readonly device: GPUDevice;
  readonly info: { vendor: string; architecture: string; description: string; fallback: boolean };
  private readonly sources = new Map<string, Source>();
  private readonly bakes = new Map<string, Bake>();
  private readonly luts = new Map<string, Lut>();
  private canvas: HTMLCanvasElement | null = null;
  private context: GPUCanvasContext | null = null;
  private readonly format: GPUTextureFormat;
  private readonly colorPipe: GPURenderPipeline;
  private readonly geoPipe: GPURenderPipeline;
  private readonly presentPipe: GPURenderPipeline;
  private readonly binPipe: GPUComputePipeline;
  private readonly histPipe: GPUComputePipeline;
  private readonly uniform: GPUBuffer;
  private readonly geoUniform: GPUBuffer;
  private readonly presentUniform: GPUBuffer;
  private readonly histUniform: GPUBuffer;
  private readonly histTotal: GPUBuffer;
  private readonly histStage: GPUBuffer;
  private readonly toneBuf: GPUBuffer;
  private readonly dummyLut: GPUTexture;
  private readonly dummyLut8: GPUTexture;
  private readonly dummy2d: GPUTexture;
  private readonly sampler: GPUSampler;
  private colorTex: GPUTexture | null = null;
  private finalTex: GPUTexture | null = null;
  private current: Source | null = null;
  private look: Look = { recipe: null, lut: null, lutStrength: 1, adjust: {}, curve: null };
  private geometry: Geometry = { straighten: 0, rotate90: 0, flip: false, vertical: 0, horizontal: 0, distortion: 0, box: null };
  private dirtyColor = true;
  private dirtyGeo = true;
  private raf = 0;
  private histBusy = false;
  private clipOverlay: ClipOverlay = { shadow: false, highlight: false };
  private displayAspect: number | null = null;
  private version = 0;
  private lost = false;
  onFrame: ((f: FrameInfo) => void) | null = null;
  onLost: ((reason: string) => void) | null = null;

  static async create(): Promise<Engine | null> {
    if (!('gpu' in navigator) || !navigator.gpu) return null;
    const adapter = await navigator.gpu.requestAdapter({ powerPreference: 'high-performance' });
    if (!adapter) return null;
    const device = await adapter.requestDevice({
      requiredLimits: { maxStorageBufferBindingSize: adapter.limits.maxStorageBufferBindingSize, maxBufferSize: adapter.limits.maxBufferSize },
    });
    const engine = new Engine(device, adapter);
    for (const msg of await engine.compileMessages()) if (msg.type === 'error') throw new Error(`WGSL ${msg.lineNum}: ${msg.message}`);
    return engine;
  }

  private readonly modules: GPUShaderModule[];

  private constructor(device: GPUDevice, adapter: GPUAdapter) {
    this.device = device;
    const ai = adapter.info;
    this.info = { vendor: ai.vendor, architecture: ai.architecture, description: ai.description, fallback: Boolean((ai as { isFallbackAdapter?: boolean }).isFallbackAdapter) };
    this.format = navigator.gpu.getPreferredCanvasFormat();
    void device.lost.then((e) => {
      this.lost = true;
      this.onLost?.(e.message || e.reason || 'lost');
    });
    const mod = (code: string) => device.createShaderModule({ code });
    const colorMod = mod(commonWgsl + colorWgsl);
    const geoMod = mod(geoWgsl);
    const presentMod = mod(presentWgsl);
    const binMod = mod(binWgsl);
    const histMod = mod(histWgsl);
    this.modules = [colorMod, geoMod, presentMod, binMod, histMod];
    this.colorPipe = device.createRenderPipeline({
      layout: 'auto',
      vertex: { module: colorMod, entryPoint: 'vs' },
      fragment: { module: colorMod, entryPoint: 'fs', targets: [{ format: 'rgba8unorm' }] },
    });
    this.geoPipe = device.createRenderPipeline({
      layout: 'auto',
      vertex: { module: geoMod, entryPoint: 'vs' },
      fragment: { module: geoMod, entryPoint: 'fs', targets: [{ format: 'rgba8unorm' }] },
    });
    this.presentPipe = device.createRenderPipeline({
      layout: 'auto',
      vertex: { module: presentMod, entryPoint: 'vs' },
      fragment: { module: presentMod, entryPoint: 'fs', targets: [{ format: this.format }] },
    });
    this.binPipe = device.createComputePipeline({ layout: 'auto', compute: { module: binMod, entryPoint: 'main' } });
    this.histPipe = device.createComputePipeline({ layout: 'auto', compute: { module: histMod, entryPoint: 'main' } });
    const ub = (size: number) => device.createBuffer({ size, usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST });
    this.uniform = ub(UNIFORM_FLOATS * 4);
    this.geoUniform = ub(5 * 16);
    this.presentUniform = ub(3 * 16);
    this.histUniform = ub(2 * 16);
    this.histTotal = device.createBuffer({ size: HIST_SIZE * 4, usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_SRC | GPUBufferUsage.COPY_DST });
    this.histStage = device.createBuffer({ size: HIST_SIZE * 4, usage: GPUBufferUsage.MAP_READ | GPUBufferUsage.COPY_DST });
    this.toneBuf = device.createBuffer({ size: TABLE_SIZE * 3 * 4, usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_DST });
    this.dummyLut = device.createTexture({ size: [2, 2, 2], dimension: '3d', format: 'rgba32float', usage: GPUTextureUsage.TEXTURE_BINDING });
    this.dummyLut8 = device.createTexture({ size: [1, 1, 1], dimension: '3d', format: 'rgba8unorm', usage: GPUTextureUsage.TEXTURE_BINDING });
    this.dummy2d = device.createTexture({ size: [1, 1], format: 'rgba8unorm', usage: GPUTextureUsage.TEXTURE_BINDING });
    this.sampler = device.createSampler({ magFilter: 'linear', minFilter: 'linear' });
  }

  private async compileMessages(): Promise<GPUCompilationMessage[]> {
    const out: GPUCompilationMessage[] = [];
    for (const m of this.modules) out.push(...(await m.getCompilationInfo()).messages);
    return out;
  }

  get isLost(): boolean {
    return this.lost;
  }

  // ---- 数据 ----

  hasSource(key: string): boolean {
    return this.sources.has(key);
  }

  sourceSize(key: string): { w: number; h: number } | null {
    const s = this.sources.get(key);
    return s ? { w: s.w, h: s.h } : null;
  }

  hasBake(key: string): boolean {
    return this.bakes.has(key);
  }

  hasLut(key: string): boolean {
    return this.luts.has(key);
  }

  /** DNG 原字节 → 显卡上拆包、归一化、白平衡、2×2 合并、转正。 */
  async addRaw(key: string, layout: RawLayout, dng: ArrayBuffer, scene: Scene | null): Promise<void> {
    const d = this.device;
    const total = layout.strips.reduce((s, [, n]) => s + n, 0);
    const bytes = new Uint8Array(Math.ceil(total / 4) * 4);
    let at = 0;
    for (const [off, n] of layout.strips) {
      bytes.set(new Uint8Array(dng, off, n), at);
      at += n;
    }
    const rawBuf = d.createBuffer({ size: bytes.byteLength, usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_DST });
    d.queue.writeBuffer(rawBuf, 0, bytes);
    const w2 = Math.floor(layout.crop[3] / 2);
    const h2 = Math.floor(layout.crop[2] / 2);
    const swap = layout.orientation >= 5;
    const w = swap ? h2 : w2;
    const h = swap ? w2 : h2;
    const tex = d.createTexture({ size: [w, h], format: 'rgba32float', usage: GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.STORAGE_BINDING });
    const u = new Float32Array(24);
    u.set(layout.crop, 0);
    u.set(layout.wb, 4);
    u.set([layout.row_bytes, layout.bps, layout.big_endian ? 1 : 0, layout.orientation], 8);
    u.set(layout.phase, 12);
    u.set(layout.black, 16);
    u.set([layout.white, layout.active[0], layout.active[1], 0], 20);
    const ubuf = d.createBuffer({ size: u.byteLength, usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST });
    d.queue.writeBuffer(ubuf, 0, u);
    const enc = d.createCommandEncoder();
    const pass = enc.beginComputePass();
    pass.setPipeline(this.binPipe);
    pass.setBindGroup(0, d.createBindGroup({
      layout: this.binPipe.getBindGroupLayout(0),
      entries: [{ binding: 0, resource: { buffer: ubuf } }, { binding: 1, resource: { buffer: rawBuf } }, { binding: 2, resource: tex.createView() }],
    }));
    pass.dispatchWorkgroups(Math.ceil(w / 16), Math.ceil(h / 16));
    pass.end();
    d.queue.submit([enc.finish()]);
    await d.queue.onSubmittedWorkDone();
    rawBuf.destroy();
    ubuf.destroy();
    this.putSource({ key, kind: 'raw', tex, w, h, bytes: w * h * 16, scene });
  }

  /** 相机 JPG（浏览器按 EXIF 方向解码）→ rgba8 纹理。 */
  addJpg(key: string, bitmap: ImageBitmap): void {
    const tex = this.device.createTexture({
      size: [bitmap.width, bitmap.height],
      format: 'rgba8unorm',
      usage: GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.COPY_DST | GPUTextureUsage.RENDER_ATTACHMENT,
    });
    this.device.queue.copyExternalImageToTexture({ source: bitmap }, { texture: tex }, [bitmap.width, bitmap.height]);
    this.putSource({ key, kind: 'jpg', tex, w: bitmap.width, h: bitmap.height, bytes: bitmap.width * bitmap.height * 4, scene: null });
  }

  private putSource(src: Source): void {
    this.sources.get(src.key)?.tex.destroy();
    this.sources.delete(src.key);
    this.sources.set(src.key, src);
    let total = 0;
    for (const s of this.sources.values()) total += s.bytes;
    for (const [k, s] of this.sources) {
      if (total <= MAX_SOURCE_BYTES || s === src || s === this.current) continue;
      s.tex.destroy();
      this.sources.delete(k);
      total -= s.bytes;
    }
  }

  /** 同一个键再加一次：先销毁旧缓冲（键里带着数据版本，内容相同的不会重复上传）。 */
  addBake(key: string, meta: BakeMeta, data: ArrayBuffer): void {
    const old = this.bakes.get(key);
    if (old) {
      old.buf.destroy();
      this.bakes.delete(key);
    }
    const buf = this.device.createBuffer({ size: data.byteLength, usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_DST });
    this.device.queue.writeBuffer(buf, 0, data);
    this.bakes.set(key, { meta, buf });
    // 配方卡片会一次装进几十张表：舞台正在用的那张不能被挤掉（挤掉了下一帧的颜色就画不出来）
    this.trim(this.bakes, 64, (b) => b.buf.destroy(), this.look.recipe);
  }

  addLut(key: string, meta: LutMeta, data: ArrayBuffer): void {
    const old = this.luts.get(key);
    if (old) {
      old.tex.destroy();
      this.luts.delete(key);
    }
    const n = meta.n;
    const exact = !!meta.table8;
    const tex = this.device.createTexture({
      size: [n, n, n],
      dimension: '3d',
      format: exact ? 'rgba8unorm' : 'rgba32float',
      usage: GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.COPY_DST,
    });
    this.device.queue.writeTexture({ texture: tex }, data, { bytesPerRow: n * (exact ? 4 : 16), rowsPerImage: n }, [n, n, n]);
    this.luts.set(key, { meta, tex });
    this.trim(this.luts, 3, (l) => l.tex.destroy()); // 精确表每张 64 MB
  }

  private trim<T>(map: Map<string, T>, max: number, drop: (v: T) => void, keep: string | null = null): void {
    for (const [k, v] of map) {
      if (map.size <= max) break;
      if (k === keep) continue;
      drop(v);
      map.delete(k);
    }
  }

  // ---- 显示 ----

  attach(canvas: HTMLCanvasElement): void {
    if (this.canvas === canvas) return;
    this.canvas = canvas;
    this.context = canvas.getContext('webgpu');
    this.context?.configure({ device: this.device, format: this.format, alphaMode: 'opaque' });
    this.dirtyGeo = true;
    this.schedule();
  }

  detach(): void {
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
    this.context?.unconfigure();
    this.canvas = null;
    this.context = null;
  }

  /** 换照片：只换绑定，不重建画布。 */
  show(key: string): boolean {
    const src = this.sources.get(key);
    if (!src) return false;
    this.sources.delete(key);
    this.sources.set(key, src);
    if (this.current !== src) {
      this.current = src;
      this.dirtyColor = true;
      this.dirtyGeo = true;
      this.schedule();
    }
    return true;
  }

  setLook(look: Partial<Look>): void {
    Object.assign(this.look, look);
    this.dirtyColor = true;
    this.dirtyGeo = true;
    this.schedule();
  }

  setGeometry(geometry: Partial<Geometry>): void {
    Object.assign(this.geometry, geometry);
    this.dirtyGeo = true;
    this.schedule();
  }

  /** 成片显示的宽高比（宽 ÷ 高）。null = 成片纹理自己的比例。导出的照片和成片纹理比例略有不同时（透视 / 畸变），按导出的比例显示。 */
  setDisplayAspect(aspect: number | null): void {
    const next = aspect !== null && Number.isFinite(aspect) && aspect > 0 ? aspect : null;
    if (next === this.displayAspect) return;
    this.displayAspect = next;
    this.schedule();
  }

  /** 当前显示的源图键（测量和测试用）。 */
  currentKey(): string | null {
    return this.current ? this.current.key : null;
  }

  /** 颜色层尺寸（未拉直、未裁切）。 */
  layerSize(): { w: number; h: number } | null {
    return this.current ? { w: this.current.w, h: this.current.h } : null;
  }

  /** 未裁切的画布尺寸（拉直、透视、畸变、旋转之后；颜色层像素）。 */
  canvasSize(): { w: number; h: number } | null {
    const src = this.current;
    return src ? canvasSizeOf(this.geometry, src) : null;
  }

  /** 成片尺寸（拉直、透视、畸变、旋转、裁切之后）。 */
  finalSize(): { w: number; h: number } | null {
    const src = this.current;
    return src ? finalSizeOf(this.geometry, src) : null;
  }

  private schedule(): void {
    if (this.raf || !this.canvas || this.lost) return;
    this.raf = requestAnimationFrame(() => {
      this.raf = 0;
      this.frame();
    });
  }

  private lutState(src: Source, look: Look = this.look): { mode: number; lut: Lut | null } {
    const lut = look.lut ? this.luts.get(look.lut) ?? null : null;
    if (src.kind === 'jpg') return { mode: lut ? 4 : 3, lut };
    if (!lut) return { mode: 0, lut: null };
    return { mode: lut.meta.display ? 1 : 2, lut };
  }

  private writeUniform(src: Source, bake: Bake | null, lut: Lut | null, mode: number, look: Look = this.look): void {
    const a = look.adjust;
    const u = new Float32Array(UNIFORM_FLOATS);
    const gains = channelGains(a) ?? [1, 1, 1];
    u.set([...gains, 0], 0);
    const table = displayTable(a, look.curve);
    if (table) this.device.queue.writeBuffer(this.toneBuf, 0, table);
    const anyBase = Boolean(a.exposure_ev || a.wb?.warm || a.wb?.tint || table);
    u.set([table ? table.length / TABLE_SIZE : 0, mode, anyBase ? 1 : 0, look.lutStrength], 4);
    u.set([a.black_point ?? 0, a.saturation ?? 0, a.vibrance ?? 0, a.vignette ?? 0], 8);
    u.set([src.w, src.h, 0, 0], 12);
    if (bake) {
      u.set([bake.meta.origin[0], bake.meta.origin[1], 1 / bake.meta.step, bake.meta.nx], 16);
      u.set([bake.meta.ny, 0, 0, 0], 20);
    }
    if (lut && !lut.meta.table8) {
      const m = lut.meta;
      u.set([m.input_space_id, m.output_space_id, m.n, m.input_legal ? 1 : 0], 24);
      u.set([...m.domain_min, 0], 28);
      u.set([...m.domain_max, 0], 32);
      u.set([m.range3[0], m.range3[1], m.output_legal ? 1 : 0, 0], 36);
      let M: Mat3 = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
      let off = [0, 0, 0];
      if (src.scene) {
        M = mul3(m.xyz_to_input, src.scene.A);
        const b = src.scene.b;
        off = m.xyz_to_input.map((r) => r[0] * b[0] + r[1] * b[1] + r[2] * b[2]);
      }
      for (let r = 0; r < 3; r++) u.set([...M[r], off[r]], 40 + r * 4);
      for (let r = 0; r < 3; r++) u.set([...m.out_to_srgb[r], 0], 52 + r * 4);
    }
    this.device.queue.writeBuffer(this.uniform, 0, u);
  }

  private ensureTextures(src: Source, fw: number, fh: number): void {
    const d = this.device;
    if (!this.colorTex || this.colorTex.width !== src.w || this.colorTex.height !== src.h) {
      this.colorTex?.destroy();
      this.colorTex = d.createTexture({ size: [src.w, src.h], format: 'rgba8unorm', usage: GPUTextureUsage.RENDER_ATTACHMENT | GPUTextureUsage.TEXTURE_BINDING });
      this.dirtyColor = true;
    }
    if (!this.finalTex || this.finalTex.width !== fw || this.finalTex.height !== fh) {
      this.finalTex?.destroy();
      this.finalTex = d.createTexture({ size: [fw, fh], format: 'rgba8unorm', usage: GPUTextureUsage.RENDER_ATTACHMENT | GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.COPY_SRC });
      this.dirtyGeo = true;
    }
  }

  private encodeColor(enc: GPUCommandEncoder, src: Source, look: Look = this.look, target: GPUTexture | null = this.colorTex): boolean {
    const { mode, lut } = this.lutState(src, look);
    const needsBake = mode === 0 || mode === 1;
    const bake = look.recipe ? this.bakes.get(look.recipe) ?? null : null;
    if (needsBake && !bake) return false;
    if (look.lut && !lut) return false;
    if (mode === 2 && !src.scene) return false;
    this.writeUniform(src, bake, lut, mode, look);
    const d = this.device;
    const anyBake = bake ?? this.bakes.values().next().value;
    const recBuf = anyBake ? anyBake.buf : this.toneBuf;
    const g0 = d.createBindGroup({
      layout: this.colorPipe.getBindGroupLayout(0),
      entries: [
        { binding: 0, resource: { buffer: this.uniform } },
        { binding: 1, resource: { buffer: recBuf } },
        { binding: 2, resource: { buffer: this.toneBuf } },
        { binding: 3, resource: (lut && !lut.meta.table8 ? lut.tex : this.dummyLut).createView({ dimension: '3d' }) },
        { binding: 4, resource: (lut && lut.meta.table8 ? lut.tex : this.dummyLut8).createView({ dimension: '3d' }) },
      ],
    });
    const rawView = src.kind === 'raw' ? src.tex.createView() : this.dummyRawView();
    const jpgView = src.kind === 'jpg' ? src.tex.createView() : this.dummy2d.createView();
    const g1 = d.createBindGroup({
      layout: this.colorPipe.getBindGroupLayout(1),
      entries: [{ binding: 0, resource: rawView }, { binding: 1, resource: jpgView }],
    });
    const pass = enc.beginRenderPass({ colorAttachments: [{ view: target!.createView(), loadOp: 'clear', storeOp: 'store', clearValue: [0, 0, 0, 1] }] });
    pass.setPipeline(this.colorPipe);
    pass.setBindGroup(0, g0);
    pass.setBindGroup(1, g1);
    pass.draw(3);
    pass.end();
    return true;
  }

  private dummyRaw: GPUTexture | null = null;
  private dummyRawView(): GPUTextureView {
    this.dummyRaw ??= this.device.createTexture({ size: [1, 1], format: 'rgba32float', usage: GPUTextureUsage.TEXTURE_BINDING });
    return this.dummyRaw.createView();
  }

  private encodeGeo(enc: GPUCommandEncoder, src: Source, fw: number, fh: number, from: GPUTexture | null = this.colorTex, to: GPUTexture | null = this.finalTex, g: Geometry = this.geometry): void {
    const th = (g.straighten * Math.PI) / 180;
    const turns = ((g.rotate90 % 4) + 4) % 4;
    const canvas = canvasOf(g, src.w, src.h);
    const { w: rw, h: rh } = rotatedSize(canvas, turns);
    const box = g.box ?? [0, 0, rw, rh];
    const straight = Math.abs(g.straighten) < 1e-9;
    const l = lens(g, src.w, src.h);
    this.device.queue.writeBuffer(this.geoUniform, 0, new Float32Array([
      src.w, src.h, rw, rh,
      straight ? 1 : Math.cos(th), straight ? 0 : Math.sin(th), turns, g.flip ? 1 : 0,
      box[0], box[1], box[2], box[3],
      l.radial, l.sv, l.sh, l.halfDiag,
      canvas.minx, canvas.miny, canvas.expanded ? 1 : 0, 0,
    ]));
    const pass = enc.beginRenderPass({ colorAttachments: [{ view: to!.createView(), loadOp: 'clear', storeOp: 'store', clearValue: [0, 0, 0, 1] }] });
    pass.setPipeline(this.geoPipe);
    pass.setBindGroup(0, this.device.createBindGroup({
      layout: this.geoPipe.getBindGroupLayout(0),
      entries: [{ binding: 0, resource: { buffer: this.geoUniform } }, { binding: 1, resource: from!.createView() }],
    }));
    pass.draw(3);
    pass.end();
    void fw;
    void fh;
  }

  private encodePresent(enc: GPUCommandEncoder, fw: number, fh: number): void {
    const canvas = this.canvas!;
    const dpr = window.devicePixelRatio || 1;
    const cw = Math.max(1, Math.round(canvas.clientWidth * dpr));
    const ch = Math.max(1, Math.round(canvas.clientHeight * dpr));
    if (canvas.width !== cw || canvas.height !== ch) {
      canvas.width = cw;
      canvas.height = ch;
    }
    this.presentTo(enc, this.context!.getCurrentTexture().createView(), cw, ch, fw, fh);
  }

  /** 第三遍画到指定的目标（画布的当前纹理，或测试读回用的离屏纹理）。 */
  private presentTo(enc: GPUCommandEncoder, target: GPUTextureView, cw: number, ch: number, fw: number, fh: number, from: GPUTexture | null = this.finalTex, o: ClipOverlay = this.clipOverlay, aspect: number | null = this.displayAspect): void {
    let sx = Math.max(fw / cw, fh / ch);
    let sy = sx;
    let ox = (cw - fw / sx) / 2;
    let oy = (ch - fh / sy) / 2;
    if (aspect !== null) {
      // 成片要显示成另一个宽高比（透视 / 畸变：导出的照片比半分辨率的成片比例略有不同）：显示矩形取这个比例，成片纹理横竖各自拉伸进去
      const dw = Math.min(cw, ch * aspect);
      const dh = dw / aspect;
      sx = fw / dw;
      sy = fh / dh;
      ox = (cw - dw) / 2;
      oy = (ch - dh) / 2;
    }
    this.device.queue.writeBuffer(this.presentUniform, 0, new Float32Array([
      sx, ox, oy, STAGE_GRAY,
      fw, fh, sy, 0,
      CLIP.shadowMax + 0.5, CLIP.highlightMin - 0.5, o.shadow ? 1 : 0, o.highlight ? 1 : 0,
    ]));
    const pass = enc.beginRenderPass({ colorAttachments: [{ view: target, loadOp: 'clear', storeOp: 'store', clearValue: [STAGE_GRAY, STAGE_GRAY, STAGE_GRAY, 1] }] });
    pass.setPipeline(this.presentPipe);
    pass.setBindGroup(0, this.device.createBindGroup({
      layout: this.presentPipe.getBindGroupLayout(0),
      entries: [{ binding: 0, resource: { buffer: this.presentUniform } }, { binding: 1, resource: from!.createView() }, { binding: 2, resource: this.sampler }],
    }));
    pass.draw(3);
    pass.end();
  }

  /** 画一帧到画布。数据还没到（配方表、LUT）时不画，保留上一帧。 */
  frame(): FrameInfo | null {
    const src = this.current;
    if (!src || !this.context || this.lost) return null;
    const t0 = performance.now();
    const size = this.finalSize()!;
    this.ensureTextures(src, size.w, size.h);
    const enc = this.device.createCommandEncoder();
    const color = this.dirtyColor;
    if (this.dirtyColor) {
      if (!this.encodeColor(enc, src)) return null;
      this.dirtyColor = false;
      this.dirtyGeo = true;
    }
    if (this.dirtyGeo) {
      this.encodeGeo(enc, src, size.w, size.h);
      this.dirtyGeo = false;
      this.version += 1;
    }
    this.encodePresent(enc, size.w, size.h);
    this.device.queue.submit([enc.finish()]);
    const info = { cpuMs: performance.now() - t0, color, submitted: performance.now() };
    this.onFrame?.(info);
    return info;
  }

  /** 重新铺满画布（窗口大小变了）。 */
  redraw(): void {
    this.schedule();
  }

  /** 读回成片纹理（1:1），给一致性测试和导出对拍用。 */
  async readback(): Promise<{ w: number; h: number; data: Uint8Array }> {
    const src = this.current;
    if (!src) throw new Error('没有照片');
    const size = this.finalSize()!;
    this.ensureTextures(src, size.w, size.h);
    const d = this.device;
    const enc = d.createCommandEncoder();
    if (!this.encodeColor(enc, src)) throw new Error('配方表或 LUT 还没到');
    this.encodeGeo(enc, src, size.w, size.h);
    const bpr = Math.ceil((size.w * 4) / 256) * 256;
    const buf = d.createBuffer({ size: bpr * size.h, usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ });
    enc.copyTextureToBuffer({ texture: this.finalTex! }, { buffer: buf, bytesPerRow: bpr }, [size.w, size.h]);
    d.queue.submit([enc.finish()]);
    await buf.mapAsync(GPUMapMode.READ);
    const mapped = new Uint8Array(buf.getMappedRange());
    const out = new Uint8Array(size.w * size.h * 4);
    for (let y = 0; y < size.h; y++) out.set(mapped.subarray(y * bpr, y * bpr + size.w * 4), y * size.w * 4);
    buf.unmap();
    buf.destroy();
    this.dirtyColor = true;
    return { w: size.w, h: size.h, data: out };
  }

  // ---- 直方图和溢出提示 ----

  /** 成片纹理的内容版本：每次重画颜色或几何都加一。直方图据此知道要不要重数。 */
  get contentVersion(): number {
    return this.version;
  }

  /** 有一帧还没画出来（参数刚改、等下一次刷新）：这时成片纹理还是旧的。 */
  get pending(): boolean {
    return this.raf !== 0 || this.dirtyColor || this.dirtyGeo;
  }

  /** 溢出提示盖不盖在画面上。只重画最后一遍（显示），不重算颜色。 */
  setClipOverlay(overlay: Partial<ClipOverlay>): void {
    Object.assign(this.clipOverlay, overlay);
    this.schedule();
  }

  /**
   * 数成片纹理的直方图。region 是归一化矩形 [x, y, w, h]（裁剪页只数框里面的），null = 整幅。
   * 异步读回：上一次还没读完就直接返回 null（跳过，不排队）；成片纹理还没有、设备丢了也返回 null。
   * 只提交一个很小的计算通道，排在已经提交的画面之后，不会拖慢预览。
   */
  async histogram(region: [number, number, number, number] | null = null, maxSamples = HIST_MAX_SAMPLES): Promise<HistogramData | null> {
    const tex = this.finalTex;
    if (!tex || this.lost || this.histBusy) return null;
    this.histBusy = true;
    try {
      const rect = pixelRegion(tex.width, tex.height, region);
      const stride = sampleStride(rect[2] - rect[0], rect[3] - rect[1], maxSamples);
      const nx = Math.ceil((rect[2] - rect[0]) / stride);
      const ny = Math.ceil((rect[3] - rect[1]) / stride);
      const version = this.version;
      const d = this.device;
      d.queue.writeBuffer(this.histUniform, 0, new Uint32Array([rect[0], rect[1], nx, ny, stride, CLIP.shadowMax, CLIP.highlightMin, 0]));
      const enc = d.createCommandEncoder();
      enc.clearBuffer(this.histTotal);
      const pass = enc.beginComputePass();
      pass.setPipeline(this.histPipe);
      pass.setBindGroup(0, d.createBindGroup({
        layout: this.histPipe.getBindGroupLayout(0),
        entries: [
          { binding: 0, resource: { buffer: this.histUniform } },
          { binding: 1, resource: tex.createView() },
          { binding: 2, resource: { buffer: this.histTotal } },
        ],
      }));
      pass.dispatchWorkgroups(8, 8);
      pass.end();
      enc.copyBufferToBuffer(this.histTotal, 0, this.histStage, 0, HIST_SIZE * 4);
      d.queue.submit([enc.finish()]);
      await this.histStage.mapAsync(GPUMapMode.READ);
      const counts = new Uint32Array(this.histStage.getMappedRange().slice(0));
      this.histStage.unmap();
      return { counts, samples: nx * ny, stride, rect, version };
    } catch (e) {
      if (this.lost) return null;
      throw e;
    } finally {
      this.histBusy = false;
    }
  }

  /** 读回最后一遍（显示）的结果，画布大小 cw × ch（含溢出提示）。给测试对拍溢出提示用。 */
  async readPresent(cw: number, ch: number): Promise<{ w: number; h: number; data: Uint8Array }> {
    const src = this.current;
    if (!src) throw new Error('没有照片');
    const size = this.finalSize()!;
    this.ensureTextures(src, size.w, size.h);
    const d = this.device;
    const enc = d.createCommandEncoder();
    if (!this.encodeColor(enc, src)) throw new Error('配方表或 LUT 还没到');
    this.encodeGeo(enc, src, size.w, size.h);
    const target = d.createTexture({ size: [cw, ch], format: this.format, usage: GPUTextureUsage.RENDER_ATTACHMENT | GPUTextureUsage.COPY_SRC });
    this.presentTo(enc, target.createView(), cw, ch, size.w, size.h);
    const data = await this.readTarget(enc, target, cw, ch);
    this.dirtyColor = true;
    this.schedule();
    return { w: cw, h: ch, data };
  }

  /** 把 enc 里已经画好的 target（显示格式）读回成 RGBA。读完销毁 target。 */
  private async readTarget(enc: GPUCommandEncoder, target: GPUTexture, cw: number, ch: number): Promise<Uint8Array> {
    const d = this.device;
    const bpr = Math.ceil((cw * 4) / 256) * 256;
    const buf = d.createBuffer({ size: bpr * ch, usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ });
    enc.copyTextureToBuffer({ texture: target }, { buffer: buf, bytesPerRow: bpr }, [cw, ch]);
    d.queue.submit([enc.finish()]);
    await buf.mapAsync(GPUMapMode.READ);
    const mapped = new Uint8Array(buf.getMappedRange());
    const bgra = this.format.startsWith('bgra');
    const out = new Uint8Array(cw * ch * 4);
    for (let y = 0; y < ch; y++) {
      for (let x = 0; x < cw; x++) {
        const i = y * bpr + x * 4;
        const o = (y * cw + x) * 4;
        out[o] = mapped[i + (bgra ? 2 : 0)];
        out[o + 1] = mapped[i + 1];
        out[o + 2] = mapped[i + (bgra ? 0 : 2)];
        out[o + 3] = mapped[i + 3];
      }
    }
    buf.unmap();
    buf.destroy();
    target.destroy();
    return out;
  }

  private scratch(tex: GPUTexture | null, w: number, h: number): GPUTexture {
    if (tex && tex.width === w && tex.height === h) return tex;
    tex?.destroy();
    return this.device.createTexture({ size: [w, h], format: 'rgba8unorm', usage: GPUTextureUsage.RENDER_ATTACHMENT | GPUTextureUsage.TEXTURE_BINDING });
  }

  private thumbColor: GPUTexture | null = null;
  private thumbFinal: GPUTexture | null = null;

  /**
   * 配方卡片的小图：给定的源图、颜色（配方 / LUT / 调整 / 曲线）和几何，画一张长边 maxEdge 个像素的小图。
   * 三遍和舞台一样（颜色 → 几何 → 缩小，缩小用同一个显示遍），画到自己的纹理里：不碰舞台的纹理，也不看舞台现在显示什么，
   * 所以舞台在画别的、或还在用相机 JPG 时也能给 RAW 的另一个配方出图。aspect：成片显示的宽高比（null = 成片自己的比例）。
   * 源图或配方表（LUT）还没到、设备丢了返回 null。
   */
  async thumb(opts: { source: string; look: Partial<Look>; geometry: Geometry; aspect: number | null; maxEdge: number }): Promise<{ w: number; h: number; data: Uint8Array } | null> {
    const src = this.sources.get(opts.source);
    if (!src || this.lost) return null;
    const size = finalSizeOf(opts.geometry, src);
    const maxEdge = opts.maxEdge;
    const d = this.device;
    this.thumbColor = this.scratch(this.thumbColor, src.w, src.h);
    this.thumbFinal = this.scratch(this.thumbFinal, size.w, size.h);
    const enc = d.createCommandEncoder();
    if (!this.encodeColor(enc, src, { ...this.look, ...opts.look }, this.thumbColor)) return null;
    this.encodeGeo(enc, src, size.w, size.h, this.thumbColor, this.thumbFinal, opts.geometry);
    const aspect = opts.aspect ?? size.w / size.h;
    const cw = Math.max(1, Math.round(aspect >= 1 ? maxEdge : maxEdge * aspect));
    const ch = Math.max(1, Math.round(aspect >= 1 ? maxEdge / aspect : maxEdge));
    const target = d.createTexture({ size: [cw, ch], format: this.format, usage: GPUTextureUsage.RENDER_ATTACHMENT | GPUTextureUsage.COPY_SRC });
    this.presentTo(enc, target.createView(), cw, ch, size.w, size.h, this.thumbFinal, { shadow: false, highlight: false }, opts.aspect);
    const data = await this.readTarget(enc, target, cw, ch);
    return { w: cw, h: ch, data };
  }

  /** 显卡吞吐：同一帧（颜色 + 几何）连续提交 n 次的平均毫秒。 */
  async bench(n = 30): Promise<number> {
    const src = this.current;
    if (!src) return NaN;
    const size = this.finalSize()!;
    this.ensureTextures(src, size.w, size.h);
    await this.device.queue.onSubmittedWorkDone();
    const t0 = performance.now();
    for (let i = 0; i < n; i++) {
      const enc = this.device.createCommandEncoder();
      this.encodeColor(enc, src);
      this.encodeGeo(enc, src, size.w, size.h);
      this.device.queue.submit([enc.finish()]);
    }
    await this.device.queue.onSubmittedWorkDone();
    this.dirtyColor = true;
    return (performance.now() - t0) / n;
  }

  stats(): { sources: number; sourceBytes: number; bakes: number; luts: number } {
    let bytes = 0;
    for (const s of this.sources.values()) bytes += s.bytes;
    return { sources: this.sources.size, sourceBytes: bytes, bakes: this.bakes.size, luts: this.luts.size };
  }

  dispose(): void {
    this.detach();
    this.thumbColor?.destroy();
    this.thumbFinal?.destroy();
    for (const s of this.sources.values()) s.tex.destroy();
    for (const b of this.bakes.values()) b.buf.destroy();
    for (const l of this.luts.values()) l.tex.destroy();
    this.sources.clear();
    this.bakes.clear();
    this.luts.clear();
    this.device.destroy();
  }
}
