// 显卡设备丢了（手机 Safari 退到后台、内存紧时都会丢）。只有一条路：落到和「没有 WebGPU」一样的状态——
// 引擎是 null，data-gpu=0，没有 data-frame，舞台显示相机 JPG，只有 RAW 的显示「预览不了」，只靠显卡的控件停用。
// 挂在旧引擎上的东西一起放掉：烘焙表的显卡键（Book.attach）、配方卡小图（lookCache）、中性小图、LUT 纹理、传上去的 RAW。

import type { Engine } from '../../../frontend/src/index';
import { createEngine } from './boot_lazy';
import { el } from './dom';
import { lookCache } from './looks';
import type { LutGpu } from './lut_gpu';
import type { Photo } from './model';

/** 只有 RAW、又没有显卡预览时舞台中间那句话。 */
export const NO_PREVIEW_TEXT = '这张只有 RAW，现在预览不了';

/** 舞台的占位：只在没有显卡、照片只有 RAW 又没有内嵌预览时露出来（editor_media.showStillPhoto）。 */
export function noPreviewPlaceholder(): HTMLElement {
  const node = el('div', 'ph', NO_PREVIEW_TEXT);
  node.dataset.state = 'no-preview';
  return node;
}

/**
 * 显卡引擎的唯一持有者。建好、建不起来、设备丢了，都通过 `use` 告诉页面：丢了是 `use(null, true)`。
 * 建引擎从不失败（失败就是 null，原因交给 `report`）。
 */
export class EngineSlot {
  private readonly shell: () => HTMLElement;
  private readonly report: (err: unknown) => void;
  private readonly use: (engine: Engine | null, lost: boolean) => void;
  private engine: Engine | null = null;
  private task: Promise<Engine | null> | null = null;

  constructor(shell: () => HTMLElement, report: (err: unknown) => void, use: (engine: Engine | null, lost: boolean) => void) {
    this.shell = shell;
    this.report = report;
    this.use = use;
  }

  /** 第一次建引擎；之后返回最近一次的结果（丢了以后是 null）。 */
  ensure(): Promise<Engine | null> {
    this.task ??= this.create();
    return this.task;
  }

  private async create(): Promise<Engine | null> {
    const engine = await createEngine(this.shell(), this.report);
    if (engine) engine.onLost = () => this.lose(engine);
    this.engine = engine;
    this.use(engine, false);
    // 建的时候设备就丢了（那时 onLost 还没接上）：照丢了处理。
    if (engine?.isLost) this.lose(engine);
    return this.engine;
  }

  private lose(engine: Engine): void {
    if (this.engine !== engine) return;
    engine.onLost = null;
    engine.onFrame = null;
    this.engine = null;
    this.task = Promise.resolve(null);
    this.use(null, true);
  }
}

/**
 * 设备丢了：放掉页面里挂在旧引擎上的东西。调之前先关掉编辑器（它画着这些小图），Book 的显卡键由 Book.attach 清。
 * 之后照片都按没有引擎打开。
 */
export function dropGpuState(shell: HTMLElement, photos: Iterable<Photo>, luts: LutGpu): void {
  delete shell.dataset.frame;
  lookCache.clear();
  luts.clear();
  for (const photo of photos) {
    // 传进显卡的完整源没了：不再算「在显卡里」（拔卡时靠它还能打开的照片也一样）。
    photo.cached = null;
    photo.neutral?.bitmap.close();
    photo.neutral = null;
  }
}
