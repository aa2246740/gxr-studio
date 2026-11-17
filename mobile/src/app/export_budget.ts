// 开始算一张之前估它要的内存（Sol final-r2 P2-05）：用同一个导出 Worker 按文件头算成品尺寸（05a 那行用的
// export_output_sizes），再按 export_batch 的 stagePeakEstimate 得到同一阶段 WASM+JS 的保守上界。

import type { ExportCore } from '../core/export_api';
import type { SizeTier } from '../lib/export_request';
import { stagePeakEstimate, type BatchItem } from './export_batch';

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

/** `item` 照 `tier` 导出时同一阶段的峰值上界。尺寸算不出来就抛错（批次按整页预算算）。 */
export async function estimatePeak(core: ExportCore, item: BatchItem, tier: SizeTier, fonts: BatchFonts): Promise<number> {
  const reply = await core.sizes({
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
  });
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
