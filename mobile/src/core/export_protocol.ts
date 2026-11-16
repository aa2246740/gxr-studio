// 主线程和导出 Worker 之间的消息。导出 Worker 只有一个、一份 WASM、一次做一张（不是 M1/M2 的那组 Worker）。

import type { ExportRequest } from '../lib/export_request';

/** 一批开始时交一次：相机配方文件和校准包（`calib_index` + `calib_blob`，照 export_prepare 的格式）。 */
export interface ExportSetup {
  dat: ArrayBuffer | null;
  calibIndex: string;
  calibBlob: ArrayBuffer | null;
}

export interface ExportJob {
  id: number;
  dng: File | null;
  jpg: File | null;
  req: ExportRequest;
  /** 只在开发服务器的测试里：在这一张的 prepare 之后模拟一次 WASM 陷阱。 */
  devTrap?: boolean;
}

export interface SizesJob {
  id: number;
  dng: File | null;
  jpg: File | null;
  side: Record<string, unknown>;
  /** 用相机 JPG 的尺寸（原样导出或编辑过的相机 JPG），否则 DNG 的裁切窗口。 */
  useJpg: boolean;
}

export type ExportIn =
  | { op: 'setup'; setup: ExportSetup }
  | { op: 'run'; job: ExportJob }
  | { op: 'sizes'; job: SizesJob }
  | { op: 'decode'; id: number; file: File };

/** 内存：WASM 线性内存的大小（只增不减），和这一刻 JS 里拿着的字节。 */
export interface ExportMemory {
  stage: 'prepare' | 'complete';
  wasm: number;
  js: number;
}

/** 失败的种类：核心拒绝（`core`，带桌面的原话）、文件读不了、WASM 陷阱或内存不够、需要墨迹。 */
export type FailKind = 'core' | 'read' | 'trap' | 'ink';

export type ExportOut =
  | { op: 'mem'; id: number; mem: ExportMemory }
  | {
      op: 'done';
      id: number;
      ok: true;
      file: ArrayBuffer;
      result: Record<string, unknown>;
      /** 交给 export_prepare 的 shot 和 side（开发服务器的测试拿它和桌面比）。 */
      shot: Record<string, unknown>;
      ms: number;
    }
  | { op: 'done'; id: number; ok: false; kind: FailKind; code: string; message: string }
  | { op: 'sizes'; id: number; ok: true; json: string }
  | { op: 'sizes'; id: number; ok: false; message: string }
  | { op: 'decoded'; id: number; ok: true; rgb: ArrayBuffer; width: number; height: number }
  | { op: 'decoded'; id: number; ok: false; message: string };
