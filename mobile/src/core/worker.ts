// 模块 Worker：WASM 只在这里加载。主线程把 DNG 的一份拷贝传进来，原件留给显卡引擎。

import init, { dng_describe } from '../wasm-pkg/gxr_wasm.js';
import wasmUrl from '../wasm-pkg/gxr_wasm_bg.wasm?url';

interface Request {
  id: number;
  op: 'describe';
  buffer: ArrayBuffer;
}

interface Ok {
  id: number;
  ok: true;
  json: string;
}

interface Fail {
  id: number;
  ok: false;
  message: string;
}

let ready: Promise<unknown> | null = null;

function load(): Promise<unknown> {
  ready ??= init({ module_or_path: wasmUrl });
  return ready;
}

self.onmessage = (event: MessageEvent<Request>) => {
  const { id, op, buffer } = event.data;
  load()
    .then(() => {
      if (op !== 'describe') throw new Error('未知请求');
      const json = dng_describe(new Uint8Array(buffer));
      const reply: Ok = { id, ok: true, json };
      self.postMessage(reply);
    })
    .catch((err: unknown) => {
      const reply: Fail = { id, ok: false, message: err instanceof Error ? err.message : String(err) };
      self.postMessage(reply);
    });
};
