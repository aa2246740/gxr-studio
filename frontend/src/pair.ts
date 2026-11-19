// 半格一对的 WebGPU 合成辅助（从 engine.ts 拆出）：窗口限额检查、两张半宽条带顺序上传、第二半 uniform 与渲染遍。
import pairColorWgsl from './shaders/pair_color.wgsl?raw';
import { expandPairColorWgsl } from './pair_wgsl';
import type { BakeMeta, Look, LutMeta, RawLayout, Scene } from './engine';

type Mat3 = [number[], number[], number[]];

export interface PairWindow {
  width: number;
  height: number;
  half_w: number;
  strip_x0: number;
  seam_x0: number;
  seam_w: number;
  right_x0: number;
}

export interface PairSourceConfig {
  first_half: 'left' | 'right';
  orientation: number;
  window: PairWindow;
  sources: [
    { layout: RawLayout; scene: Scene | null },
    { layout: RawLayout; scene: Scene | null },
  ];
}

export interface PairInfo {
  sensorW: number;
  sensorH: number;
  halfW: number;
  rightX0: number;
  orientation: number;
  firstLeft: boolean;
}

export interface PairSource {
  key: string;
  kind: 'pair-raw';
  tex: GPUTexture;
  tex2: GPUTexture;
  w: number;
  h: number;
  bytes: number;
  scene: Scene | null;
  scene2: Scene | null;
  pair: PairInfo;
}

export interface PairBake {
  meta: BakeMeta;
  buf: GPUBuffer;
}

export interface PairLut {
  meta: LutMeta;
  tex: GPUTexture;
}

export interface PairResources {
  module: GPUShaderModule;
  pipeline: GPURenderPipeline;
  uniform: GPUBuffer;
}

const PAIR_UNIFORM_FLOATS = 7 * 4;

export function createPairResources(device: GPUDevice, commonWgsl: string): PairResources {
  const module = device.createShaderModule({ code: commonWgsl + expandPairColorWgsl(commonWgsl, pairColorWgsl) });
  const pipeline = device.createRenderPipeline({
    layout: 'auto',
    vertex: { module, entryPoint: 'vs' },
    fragment: { module, entryPoint: 'fs', targets: [{ format: 'rgba8unorm' }] },
  });
  const uniform = device.createBuffer({
    size: PAIR_UNIFORM_FLOATS * 4,
    usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST,
  });
  return { module, pipeline, uniform };
}

/** 下载或上传两份 DNG 之前先核对成片纹理和两份条带的显卡限额，超限直接退回服务器，不白下 RAW。 */
export function checkPairLimits(limits: GPUSupportedLimits | undefined, cfg: PairSourceConfig): { outW: number; outH: number } {
  const swap = cfg.orientation >= 5;
  const outW = swap ? cfg.window.height : cfg.window.width;
  const outH = swap ? cfg.window.width : cfg.window.height;
  if (limits) {
    if (limits.maxTextureDimension2D && (outW > limits.maxTextureDimension2D || outH > limits.maxTextureDimension2D)) {
      throw new Error('pair_exceeds_texture_limit');
    }
    for (const s of cfg.sources) {
      const lay = s.layout;
      const total = lay.strips.reduce((acc, [, n]) => acc + n, 0);
      const padded = Math.ceil(total / 4) * 4;
      const w2 = Math.floor(lay.crop[3] / 2);
      const h2 = Math.floor(lay.crop[2] / 2);
      const sw = lay.orientation >= 5 ? h2 : w2;
      const sh = lay.orientation >= 5 ? w2 : h2;
      if (limits.maxStorageBufferBindingSize && padded > limits.maxStorageBufferBindingSize) {
        throw new Error('raw_exceeds_storage_limit');
      }
      if (limits.maxBufferSize && padded > limits.maxBufferSize) {
        throw new Error('raw_exceeds_buffer_limit');
      }
      if (limits.maxTextureDimension2D && (sw > limits.maxTextureDimension2D || sh > limits.maxTextureDimension2D)) {
        throw new Error('raw_exceeds_texture_limit');
      }
    }
  }
  return { outW, outH };
}

/** 半格一对的两份 DNG → 逐份按窗口拆包到两张半宽传感器条带纹理（总显存 ≤ 单张），顺序上传不叠加临时缓冲。 */
export async function loadPairRaw(
  limits: GPUSupportedLimits | undefined,
  binRaw: (layout: RawLayout, dng: ArrayBuffer) => Promise<{ tex: GPUTexture; w: number; h: number }>,
  key: string,
  cfg: PairSourceConfig,
  dng1: ArrayBuffer,
  dng2: ArrayBuffer,
): Promise<PairSource> {
  const { outW, outH } = checkPairLimits(limits, cfg);
  const b1 = await binRaw(cfg.sources[0].layout, dng1);
  let b2: { tex: GPUTexture; w: number; h: number };
  try {
    b2 = await binRaw(cfg.sources[1].layout, dng2);
  } catch (err) {
    b1.tex.destroy();
    throw err;
  }
  if (b1.w !== cfg.window.half_w || b1.h !== cfg.window.height || b2.w !== cfg.window.half_w || b2.h !== cfg.window.height) {
    b1.tex.destroy();
    b2.tex.destroy();
    throw new Error('pair_strip_size_mismatch');
  }
  return {
    key,
    kind: 'pair-raw',
    tex: b1.tex,
    tex2: b2.tex,
    w: outW,
    h: outH,
    bytes: (b1.w * b1.h + b2.w * b2.h) * 16,
    scene: cfg.sources[0].scene,
    scene2: cfg.sources[1].scene,
    pair: {
      sensorW: cfg.window.width,
      sensorH: cfg.window.height,
      halfW: cfg.window.half_w,
      rightX0: cfg.window.right_x0,
      orientation: cfg.orientation,
      firstLeft: cfg.first_half === 'left',
    },
  };
}

function mul3(a: Mat3, b: Mat3): Mat3 {
  return a.map((row) => [0, 1, 2].map((j) => row[0] * b[0][j] + row[1] * b[1][j] + row[2] * b[2][j])) as Mat3;
}

export function writePairUniform(
  device: GPUDevice,
  pairUniform: GPUBuffer,
  src: { pair: PairInfo; scene2?: Scene | null },
  bake2: PairBake | null,
  lut: PairLut | null,
  sameBake: boolean,
): void {
  const p = src.pair;
  const u = new Float32Array(PAIR_UNIFORM_FLOATS);
  u.set([p.sensorW, p.sensorH, p.halfW, p.rightX0], 0);
  u.set([p.orientation, p.firstLeft ? 1 : 0, sameBake ? 1 : 0, 0], 4);
  if (bake2) {
    u.set([bake2.meta.origin[0], bake2.meta.origin[1], 1 / bake2.meta.step, bake2.meta.nx], 8);
    u.set([bake2.meta.ny, 0, 0, 0], 12);
  }
  if (lut && !lut.meta.table8) {
    const m = lut.meta;
    let M: Mat3 = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
    let off = [0, 0, 0];
    if (src.scene2) {
      M = mul3(m.xyz_to_input, src.scene2.A);
      const b = src.scene2.b;
      off = m.xyz_to_input.map((r) => r[0] * b[0] + r[1] * b[1] + r[2] * b[2]);
    }
    for (let r = 0; r < 3; r++) u.set([...M[r], off[r]], 16 + r * 4);
  }
  device.queue.writeBuffer(pairUniform, 0, u);
}

export interface PairColorContext {
  device: GPUDevice;
  pipeline: GPURenderPipeline;
  uniform: GPUBuffer;
  pairUniform: GPUBuffer;
  toneBuf: GPUBuffer;
  dummyLut: GPUTexture;
  dummyLut8: GPUTexture;
  bakes: Map<string, PairBake>;
  lutState: (look: Look) => { mode: number; lut: PairLut | null };
  writeMainUniform: (bake1: PairBake | null, lut: PairLut | null, mode: number) => void;
}

export function encodePairColor(
  ctx: PairColorContext,
  enc: GPUCommandEncoder,
  src: { tex: GPUTexture; tex2?: GPUTexture; scene: Scene | null; scene2?: Scene | null; pair?: PairInfo },
  look: Look,
  target: GPUTexture,
): boolean {
  if (!src.tex2 || !src.pair) return false;
  const { mode, lut } = ctx.lutState(look);
  const needsBake = mode === 0 || mode === 1;
  const bake1 = look.recipe ? ctx.bakes.get(look.recipe) ?? null : null;
  // P3-1：第二半必须显式给出 recipe2，绝不静默借用第一半的烘焙表；缺表返回 false 退回服务器。
  const bake2 = look.recipe2 ? ctx.bakes.get(look.recipe2) ?? null : null;
  if (needsBake && (!bake1 || !bake2)) return false;
  if (look.lut && !lut) return false;
  if (mode === 2 && (!src.scene || !src.scene2)) return false;
  ctx.writeMainUniform(bake1, lut, mode);
  writePairUniform(ctx.device, ctx.pairUniform, { pair: src.pair, scene2: src.scene2 }, bake2, lut, bake1 !== null && bake1 === bake2);
  const d = ctx.device;
  const anyBake = bake1 ?? ctx.bakes.values().next().value;
  const recBuf1 = anyBake ? anyBake.buf : ctx.toneBuf;
  const recBuf2 = (bake2 ?? anyBake)?.buf ?? ctx.toneBuf;
  const g0 = d.createBindGroup({
    layout: ctx.pipeline.getBindGroupLayout(0),
    entries: [
      { binding: 0, resource: { buffer: ctx.uniform } },
      { binding: 1, resource: { buffer: recBuf1 } },
      { binding: 2, resource: { buffer: ctx.toneBuf } },
      { binding: 3, resource: (lut && !lut.meta.table8 ? lut.tex : ctx.dummyLut).createView({ dimension: '3d' }) },
      { binding: 4, resource: (lut && lut.meta.table8 ? lut.tex : ctx.dummyLut8).createView({ dimension: '3d' }) },
    ],
  });
  const g1 = d.createBindGroup({
    layout: ctx.pipeline.getBindGroupLayout(1),
    entries: [
      { binding: 0, resource: { buffer: ctx.pairUniform } },
      { binding: 1, resource: src.tex.createView() },
      { binding: 2, resource: src.tex2.createView() },
      { binding: 3, resource: { buffer: recBuf2 } },
    ],
  });
  const pass = enc.beginRenderPass({ colorAttachments: [{ view: target.createView(), loadOp: 'clear', storeOp: 'store', clearValue: [0, 0, 0, 1] }] });
  pass.setPipeline(ctx.pipeline);
  pass.setBindGroup(0, g0);
  pass.setBindGroup(1, g1);
  pass.draw(3);
  pass.end();
  return true;
}
