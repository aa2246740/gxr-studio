// 主线程和核心 Worker 之间的消息。

export type CoreOp = 'ping' | 'describe' | 'dat' | 'calib' | 'bake';

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
}

export type CoreReply =
  | { id: number; ok: true; json: string; buffers: ArrayBuffer[] }
  | { id: number; ok: false; message: string };

export interface CoreResult {
  json: string;
  buffers: ArrayBuffer[];
}
