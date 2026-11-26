// HDR 预览的增益纹理和显示遍。SDR 的 present 管道不经过这里。

import hdrWgsl from './shaders/hdr_present.wgsl?raw';
import { type GainMeta, type HdrOutputMode, gainWeight } from './hdr_math';

export type { GainMeta, GainTexture, HdrOutputMode } from './hdr_math';
export { f16ToF32, gainWeight, presentMap, srgbDecode } from './hdr_math';

export class HdrOutput {
  readonly module: GPUShaderModule;
  private readonly pipe: GPURenderPipeline;
  private readonly uniform: GPUBuffer;
  private readonly sampler: GPUSampler;
  private gain: GPUTexture | null = null;
  private readonly dummy: GPUTexture;
  meta: GainMeta | null = null;
  headroom: number | null = null;

  constructor(private readonly device: GPUDevice) {
    this.module = device.createShaderModule({ code: hdrWgsl });
    this.pipe = device.createRenderPipeline({
      layout: 'auto',
      vertex: { module: this.module, entryPoint: 'vs' },
      fragment: { module: this.module, entryPoint: 'fs', targets: [{ format: 'rgba16float' }] },
    });
    this.uniform = device.createBuffer({ size: 5 * 16, usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST });
    this.sampler = device.createSampler({ magFilter: 'linear', minFilter: 'linear' });
    this.dummy = device.createTexture({ size: [1, 1], format: 'r8unorm', usage: GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.COPY_DST });
  }

  get active(): boolean {
    return this.gain !== null && this.meta !== null;
  }

  get textureCount(): number {
    return this.gain ? 1 : 0;
  }

  /** 换网格。hdr false 或空网格会释放纹理。 */
  set(mode: HdrOutputMode): void {
    this.gain?.destroy();
    this.gain = null;
    this.meta = null;
    this.headroom = null;
    if (!mode.hdr || mode.gain.width < 1 || mode.gain.height < 1 || mode.gain.codes.length < mode.gain.width * mode.gain.height) return;
    const tex = this.device.createTexture({
      size: [mode.gain.width, mode.gain.height],
      format: 'r8unorm',
      usage: GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.COPY_DST,
    });
    const bpr = Math.ceil(mode.gain.width / 256) * 256;
    const padded = new Uint8Array(bpr * mode.gain.height);
    for (let y = 0; y < mode.gain.height; y++) {
      padded.set(mode.gain.codes.subarray(y * mode.gain.width, (y + 1) * mode.gain.width), y * bpr);
    }
    this.device.queue.writeTexture(
      { texture: tex },
      padded,
      { bytesPerRow: bpr, rowsPerImage: mode.gain.height },
      [mode.gain.width, mode.gain.height],
    );
    this.gain = tex;
    this.meta = mode.meta;
    this.headroom = mode.headroom ?? null;
  }

  release(): void {
    this.set({ hdr: false });
  }

  destroy(): void {
    this.release();
    this.dummy.destroy();
    this.uniform.destroy();
  }

  present(
    enc: GPUCommandEncoder,
    target: GPUTextureView,
    map: Float32Array,
    from: GPUTexture,
  ): void {
    this.writeUniform(map);
    const pass = enc.beginRenderPass({
      colorAttachments: [{ view: target, loadOp: 'clear', storeOp: 'store', clearValue: [0, 0, 0, 1] }],
    });
    pass.setPipeline(this.pipe);
    pass.setBindGroup(0, this.device.createBindGroup({
      layout: this.pipe.getBindGroupLayout(0),
      entries: [
        { binding: 0, resource: { buffer: this.uniform } },
        { binding: 1, resource: from.createView() },
        { binding: 2, resource: this.sampler },
        { binding: 3, resource: (this.gain ?? this.dummy).createView() },
      ],
    }));
    pass.draw(3);
    pass.end();
  }

  private writeUniform(map: Float32Array): void {
    const meta = this.meta;
    const gamma = meta && meta.gamma !== 0 ? 1 / meta.gamma : 1;
    const weight = meta ? gainWeight(meta, this.headroom) : 0;
    const extra = new Float32Array([
      meta?.gain_min ?? 0, meta?.gain_max ?? 0, gamma, weight,
      meta?.offset_sdr ?? 0, meta?.offset_hdr ?? 0, 0, 0,
    ]);
    const all = new Float32Array(20);
    all.set(map, 0);
    all.set(extra, 12);
    this.device.queue.writeBuffer(this.uniform, 0, all);
  }
}
