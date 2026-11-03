// WebGPU 预览引擎。数据（RAW 字节、相机 JPG、配方表、LUT 表）各上传一次；拖滑块只改一块 uniform 和 1021 点调子表。
// 三遍：颜色（源图分辨率）→ 几何（1:1 成片纹理，读回和 Python 比的就是它）→ 显示（缩放到画布）。
import colorWgsl from './shaders/color.wgsl?raw';
import commonWgsl from './shaders/common.wgsl?raw';
import geoWgsl from './shaders/geo.wgsl?raw';
import binWgsl from './shaders/bin.wgsl?raw';
import presentWgsl from './shaders/present.wgsl?raw';
import { type Adjust, type CurvePoints, channelGains, displayTable, TABLE_SIZE } from './tones';

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
  curve: CurvePoints | null;
}

export interface Geometry {
  straighten: number;
  rotate90: number;
  flip: boolean;
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

const UNIFORM_FLOATS = 18 * 4;
const MAX_SOURCE_BYTES = 640 << 20;
// 画布外的空白：设计系统的舞台灰 --stage-dark #2a2a2a
const STAGE_GRAY = 0x2a / 255;

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
  private readonly uniform: GPUBuffer;
  private readonly geoUniform: GPUBuffer;
  private readonly presentUniform: GPUBuffer;
  private readonly toneBuf: GPUBuffer;
  private readonly dummyLut: GPUTexture;
  private readonly dummyLut8: GPUTexture;
  private readonly dummy2d: GPUTexture;
  private readonly sampler: GPUSampler;
  private colorTex: GPUTexture | null = null;
  private finalTex: GPUTexture | null = null;
  private current: Source | null = null;
  private look: Look = { recipe: null, lut: null, lutStrength: 1, adjust: {}, curve: null };
  private geometry: Geometry = { straighten: 0, rotate90: 0, flip: false, box: null };
  private dirtyColor = true;
  private dirtyGeo = true;
  private raf = 0;
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
    this.modules = [colorMod, geoMod, presentMod, binMod];
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
    const ub = (size: number) => device.createBuffer({ size, usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST });
    this.uniform = ub(UNIFORM_FLOATS * 4);
    this.geoUniform = ub(4 * 16);
    this.presentUniform = ub(2 * 16);
    this.toneBuf = device.createBuffer({ size: TABLE_SIZE * 4, usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_DST });
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
    this.trim(this.bakes, 24, (b) => b.buf.destroy());
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

  private trim<T>(map: Map<string, T>, max: number, drop: (v: T) => void): void {
    while (map.size > max) {
      const first = map.entries().next().value;
      if (!first) break;
      drop(first[1]);
      map.delete(first[0]);
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

  /** 当前显示的源图键（测量和测试用）。 */
  currentKey(): string | null {
    return this.current ? this.current.key : null;
  }

  /** 颜色层尺寸（未拉直、未裁切）。 */
  layerSize(): { w: number; h: number } | null {
    return this.current ? { w: this.current.w, h: this.current.h } : null;
  }

  /** 成片尺寸（拉直、旋转、裁切之后）。 */
  finalSize(): { w: number; h: number } | null {
    const src = this.current;
    if (!src) return null;
    const box = this.geometry.box;
    if (box) return { w: box[2], h: box[3] };
    const turns = ((this.geometry.rotate90 % 4) + 4) % 4;
    return turns % 2 ? { w: src.h, h: src.w } : { w: src.w, h: src.h };
  }

  private schedule(): void {
    if (this.raf || !this.canvas || this.lost) return;
    this.raf = requestAnimationFrame(() => {
      this.raf = 0;
      this.frame();
    });
  }

  private lutState(src: Source): { mode: number; lut: Lut | null } {
    const lut = this.look.lut ? this.luts.get(this.look.lut) ?? null : null;
    if (src.kind === 'jpg') return { mode: lut ? 4 : 3, lut };
    if (!lut) return { mode: 0, lut: null };
    return { mode: lut.meta.display ? 1 : 2, lut };
  }

  private writeUniform(src: Source, bake: Bake | null, lut: Lut | null, mode: number): void {
    const a = this.look.adjust;
    const u = new Float32Array(UNIFORM_FLOATS);
    const gains = channelGains(a) ?? [1, 1, 1];
    u.set([...gains, 0], 0);
    const table = displayTable(a, this.look.curve);
    if (table) this.device.queue.writeBuffer(this.toneBuf, 0, table);
    const anyBase = Boolean(a.exposure_ev || a.wb?.warm || a.wb?.tint || table);
    u.set([table ? 1 : 0, mode, anyBase ? 1 : 0, this.look.lutStrength], 4);
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

  private encodeColor(enc: GPUCommandEncoder, src: Source): boolean {
    const { mode, lut } = this.lutState(src);
    const needsBake = mode === 0 || mode === 1;
    const bake = this.look.recipe ? this.bakes.get(this.look.recipe) ?? null : null;
    if (needsBake && !bake) return false;
    if (this.look.lut && !lut) return false;
    if (mode === 2 && !src.scene) return false;
    this.writeUniform(src, bake, lut, mode);
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
    const pass = enc.beginRenderPass({ colorAttachments: [{ view: this.colorTex!.createView(), loadOp: 'clear', storeOp: 'store', clearValue: [0, 0, 0, 1] }] });
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

  private encodeGeo(enc: GPUCommandEncoder, src: Source, fw: number, fh: number): void {
    const g = this.geometry;
    const th = (g.straighten * Math.PI) / 180;
    const turns = ((g.rotate90 % 4) + 4) % 4;
    const rw = turns % 2 ? src.h : src.w;
    const rh = turns % 2 ? src.w : src.h;
    const box = g.box ?? [0, 0, rw, rh];
    const straight = Math.abs(g.straighten) < 1e-9;
    this.device.queue.writeBuffer(this.geoUniform, 0, new Float32Array([
      src.w, src.h, rw, rh,
      straight ? 1 : Math.cos(th), straight ? 0 : Math.sin(th), turns, g.flip ? 1 : 0,
      box[0], box[1], box[2], box[3],
      0, 0, 0, 0,
    ]));
    const pass = enc.beginRenderPass({ colorAttachments: [{ view: this.finalTex!.createView(), loadOp: 'clear', storeOp: 'store', clearValue: [0, 0, 0, 1] }] });
    pass.setPipeline(this.geoPipe);
    pass.setBindGroup(0, this.device.createBindGroup({
      layout: this.geoPipe.getBindGroupLayout(0),
      entries: [{ binding: 0, resource: { buffer: this.geoUniform } }, { binding: 1, resource: this.colorTex!.createView() }],
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
    const scale = Math.max(fw / cw, fh / ch);
    const ox = (cw - fw / scale) / 2;
    const oy = (ch - fh / scale) / 2;
    this.device.queue.writeBuffer(this.presentUniform, 0, new Float32Array([scale, ox, oy, STAGE_GRAY, fw, fh, 0, 0]));
    const pass = enc.beginRenderPass({ colorAttachments: [{ view: this.context!.getCurrentTexture().createView(), loadOp: 'clear', storeOp: 'store', clearValue: [STAGE_GRAY, STAGE_GRAY, STAGE_GRAY, 1] }] });
    pass.setPipeline(this.presentPipe);
    pass.setBindGroup(0, this.device.createBindGroup({
      layout: this.presentPipe.getBindGroupLayout(0),
      entries: [{ binding: 0, resource: { buffer: this.presentUniform } }, { binding: 1, resource: this.finalTex!.createView() }, { binding: 2, resource: this.sampler }],
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
    for (const s of this.sources.values()) s.tex.destroy();
    for (const b of this.bakes.values()) b.buf.destroy();
    for (const l of this.luts.values()) l.tex.destroy();
    this.sources.clear();
    this.bakes.clear();
    this.luts.clear();
    this.device.destroy();
  }
}
