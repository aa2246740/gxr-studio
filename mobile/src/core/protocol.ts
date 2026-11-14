// 主线程和核心 Worker 之间的消息。

export type CoreOp = 'ping' | 'describe' | 'dat' | 'calib-file' | 'calib-restore' | 'bake';

/** 烘焙一张表要的全部输入。字节都是拷贝，交给 Worker 后这边不再用。 */
export interface BakeArgs {
  entry: ArrayBuffer;
  stock: ArrayBuffer;
  menu: string;
  ctx: ArrayBuffer;
  ring: ArrayBuffer;
  params: string;
}

export interface CoreRequest {
  id: number;
  op: CoreOp;
  buffer?: ArrayBuffer | null;
  args?: BakeArgs | null;
  /** 校准数据 zip：File 交给 Worker，大小在 Worker 里读之前先查，主线程不碰字节。 */
  file?: File | null;
  /** 导入时存进 IndexedDB 用的名字和时间。 */
  meta?: { name: string; savedAt: number } | null;
}

export type CoreReply =
  | { id: number; ok: true; json: string; buffers: ArrayBuffer[] }
  | { id: number; ok: false; message: string };

export interface CoreResult {
  json: string;
  buffers: ArrayBuffer[];
}
