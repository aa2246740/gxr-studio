// 主线程和导出 Worker 之间的消息。导出 Worker 只有一个、一份 WASM、一次做一张（不是 M1/M2 的那组 Worker）。

import type { ExportRequest } from '../lib/export_request';
import type { HalfframeSource } from '../lib/halfframe';

/** 一批开始时交一次：相机配方文件、校准包（`calib_index` + `calib_blob`）以及快照好的 LUT 字节与空间配置。 */
export interface ExportLutEntry {
  cube: ArrayBuffer;
  space: string;
}

export interface ExportSetup {
  dat: ArrayBuffer | null;
  /** 同卡的 GXRFX.BIN。没写时由导出这边按 DAT 字节找回。 */
  library?: ArrayBuffer | null;
  calibIndex: string;
  calibBlob: ArrayBuffer | null;
  luts?: Record<string, ExportLutEntry>;
  fontIndex?: string;
  fontPack?: ArrayBuffer | null;
  logoIndex?: string;
  logoBlob?: ArrayBuffer | null;
}

export interface ExportJob {
  id: number;
  dng: File | null;
  jpg: File | null;
  req: ExportRequest;
  /** 半格单格来源：第 1 张中心条带 / 双联左半 / 双联右半（排除 112px 黑缝并加 `_half` 后缀）。 */
  halfframeSource?: HalfframeSource;
  /** 第 1 张不在库里时移除双联来源的 EXIF Copyright（0x8298），绝不把第 2 张配方标给第 1 格。 */
  stripCopyright?: boolean;
  /** 只在开发服务器的测试里：在这一张的 prepare 之后模拟一次 WASM 陷阱。 */
  devTrap?: boolean;
  /** 只在开发服务器的测试里：在这一张的 export_ink 阶段模拟一次 WASM 陷阱。 */
  devInkTrap?: boolean;
  /** 只在开发服务器的测试里：当 `export_ink` 返回 `exact === false` 时把着墨画布交到回执端点。 */
  devCanvasReport?: string;
  /** 只在开发服务器的测试里：`exact === true` 时也交画布（去 hint 的整张对拍）。 */
  devForceCanvas?: boolean;
}

export interface SizesJob {
  id: number;
  dng: File | null;
  jpg: File | null;
  side: Record<string, unknown>;
  /** 用相机 JPG 的尺寸（原样导出或编辑过的相机 JPG），否则 DNG 的裁切窗口。 */
  useJpg: boolean;
  layout?: Record<string, unknown>;
  stem?: string;
  mtimeMs?: number;
  tzOffsetMin?: number;
  fontIndex?: string;
  fontPack?: ArrayBuffer | null;
}

export type ExportIn =
  | { op: 'setup'; setup: ExportSetup }
  | { op: 'run'; job: ExportJob }
  | { op: 'sizes'; job: SizesJob }
  | { op: 'decode'; id: number; file: File };

/** 内存：WASM 线性内存的大小（只增不减），和这一刻 JS 里拿着的字节。 */
export interface ExportMemory {
  stage: 'prepare' | 'ink' | 'complete';
  wasm: number;
  js: number;
}

/** 失败的种类：核心拒绝（`core`，带桌面的原话）、文件读不了、WASM 陷阱或内存不够。 */
export type FailKind = 'core' | 'read' | 'trap';

export type ExportOut =
  | { op: 'mem'; id: number; mem: ExportMemory }
  | {
      op: 'done';
      id: number;
      ok: true;
      file: ArrayBuffer;
      result: Record<string, unknown>;
      /** 交给 export_prepare 的 shot（开发服务器的测试拿它和桌面比）。 */
      shot: Record<string, unknown>;
      /** `export_ink` 返回的精确等级标记（true 时文件与桌面逐字节相等）。 */
      exact: boolean;
      ms: number;
    }
  | { op: 'done'; id: number; ok: false; kind: FailKind; code: string; message: string }
  /** `source`：两份文件头里大的那份源图的像素数（导出前估内存用）。 */
  | { op: 'sizes'; id: number; ok: true; json: string; source: number }
  | { op: 'sizes'; id: number; ok: false; message: string }
  | { op: 'decoded'; id: number; ok: true; rgb: ArrayBuffer; width: number; height: number }
  | { op: 'decoded'; id: number; ok: false; message: string };
