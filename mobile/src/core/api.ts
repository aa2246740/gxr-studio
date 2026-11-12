// 主线程用的描述接口。传给 Worker 的是拷贝，调用方留着原来的 ArrayBuffer 给显卡。

import type { RawLayout, Scene } from '../../../frontend/src/index';
import { Pool } from './pool';

export interface Described {
  layout: RawLayout | null;
  reason: string | null;
  scene: Scene | null;
}

let pool: Pool | null = null;

function workers(): Pool {
  pool ??= new Pool();
  return pool;
}

function isLayout(value: unknown): value is RawLayout {
  if (!value || typeof value !== 'object') return false;
  const row = value as Record<string, unknown>;
  return typeof row.bps === 'number' && Array.isArray(row.strips) && Array.isArray(row.crop) && Array.isArray(row.wb);
}

function isScene(value: unknown): value is Scene {
  if (!value || typeof value !== 'object') return false;
  const row = value as Record<string, unknown>;
  return Array.isArray(row.A) && Array.isArray(row.b);
}

/** 解析一份 DNG。`bytes` 仍归调用方；Worker 拿到的是拷贝。 */
export async function describeDng(bytes: ArrayBuffer): Promise<Described> {
  const copy = bytes.slice(0);
  const json = await workers().request('describe', copy, [copy]);
  const data = JSON.parse(json) as { layout?: unknown; reason?: unknown; scene?: unknown; error?: { message?: string } };
  if (data.error) throw new Error(data.error.message || '读不了这张照片');
  return {
    layout: isLayout(data.layout) ? data.layout : null,
    reason: typeof data.reason === 'string' ? data.reason : null,
    scene: isScene(data.scene) ? data.scene : null,
  };
}
