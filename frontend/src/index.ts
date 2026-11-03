// GXR Studio 的 WebGPU 预览引擎。构建产物是 web/assets/gpu/engine.js（已提交，启动工作室不需要 Node）。
export { Engine } from './engine';
export type { BakeMeta, FrameInfo, Geometry, Look, LutMeta, RawLayout, Scene } from './engine';
export { channelGains, displayTable, pchip, TABLE_SIZE } from './tones';
export type { Adjust, CurvePoints } from './tones';
