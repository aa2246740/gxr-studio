// 开始算一张之前估它要的内存（Sol final-r2 P2-05）：用同一个导出 Worker 按文件头算成品尺寸（05a 那行用的
// export_output_sizes），再按 export_batch 的 stagePeakEstimate 得到同一阶段 WASM+JS 的保守上界。
// 文件读不了时不估：抛 FileUnreadable，批次照 Worker 里读不了一样按 read 结算这张。别的原因估不出来就抛错，按整页预算算。

import type { ExportCore } from '../core/export_api';
import type { SizeTier } from '../lib/export_request';
import { FileUnreadable, stagePeakEstimate, type BatchItem } from './export_batch';

export interface BatchFonts {
  fontIndex: string;
  fontPack: ArrayBuffer | null;
}

interface Sized {
  photo?: [number, number];
  sizes?: Partial<Record<SizeTier, [number, number]>>;
}

function area(pair: [number, number] | undefined): number {
  return pair && Number.isFinite(pair[0]) && Number.isFinite(pair[1]) ? pair[0] * pair[1] : 0;
}

/** 读得到第一个字节。Worker 的 run() 两份文件都整个读，哪份读不了这张就是 `read`。 */
async function readable(file: File | null): Promise<boolean> {
  if (!file) return true;
  try {
    await file.slice(0, 1).arrayBuffer();
    return true;
  } catch {
    return false;
  }
}

/** `item` 照 `tier` 导出时同一阶段的峰值上界。文件读不了抛 `FileUnreadable`；尺寸算不出来抛错（批次按整页预算算）。 */
export async function estimatePeak(core: ExportCore, item: BatchItem, tier: SizeTier, fonts: BatchFonts): Promise<number> {
  const [dngOk, jpgOk] = await Promise.all([readable(item.dng), readable(item.jpg)]);
  if (!dngOk || !jpgOk) throw new FileUnreadable();
  const job = {
    dng: item.dng,
    jpg: item.jpg,
    side: item.req.side,
    layout: item.req.layout,
    useJpg: item.jpg !== null && (item.req.needsJpg || item.dng === null),
    stem: item.stem,
    mtimeMs: item.req.mtimeMs,
    tzOffsetMin: item.req.tzOffsetMin,
    fontIndex: fonts.fontIndex,
    fontPack: fonts.fontPack,
  };
  // 不顺带发这一批的设置：它由这一张的 run() 自己发（发不出去照旧算这一张的陷阱，Sol r2 P2-04）。
  const reply = await core.sizes(job, false);
  if (!reply.ok || !reply.json) throw new Error('sizes');
  const sized = JSON.parse(reply.json) as Sized;
  const full = area(sized.sizes?.original);
  const canvas = area(sized.sizes?.[tier]) || full;
  const photo = area(sized.photo) * (full > 0 ? canvas / full : 1);
  if (!(canvas > 0) || !(photo > 0)) throw new Error('sizes');
  return stagePeakEstimate({
    source: Math.max(reply.source ?? 0, area(sized.photo)),
    photo,
    canvas,
    fileBytes: Math.max(item.dng?.size ?? 0, item.jpg?.size ?? 0),
    hdr: item.req.hdr,
  });
}
