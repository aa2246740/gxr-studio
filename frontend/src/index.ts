// GXR Studio 的 WebGPU 预览引擎。构建产物是 web/assets/gpu/engine.js（已提交，启动工作室不需要 Node）。
export { CLIP, Engine, HIST_MAX_SAMPLES, pixelRegion, sampleStride } from './engine';
export type { BakeMeta, ClipOverlay, FrameInfo, Geometry, HistogramData, Look, LutMeta, RawLayout, Scene } from './engine';
export { canvasOf, expands, rotatedSize, validRect } from './warp';
export type { Canvas } from './warp';
export { channelGains, displayTable, pchip, TABLE_SIZE } from './tones';
export type { Adjust, CurvePoints } from './tones';
