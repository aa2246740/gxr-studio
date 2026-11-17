// 开始算一张之前估它要的内存（Sol final-r2 P2-05）：用同一个导出 Worker 按文件头算成品尺寸（05a 那行用的
// export_output_sizes），再按 export_batch 的 stagePeakEstimate 得到同一阶段 WASM+JS 的保守上界。
// 文件头读不出尺寸时（比如文件已经读不了），按量 ink-text 上限的那张（4288×2848 照片、4875×6500 画布）估：
// 这一张多半在读文件时就失败，不该为它把批次停下来。

import type { ExportCore } from '../core/export_api';
import type { SizeTier } from '../lib/export_request';
import { CEILING_CANVAS_PX, CEILING_SOURCE_PX, stagePeakEstimate, type BatchItem } from './export_batch';

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

/** `item` 照 `tier` 导出时同一阶段的峰值上界。 */
export async function estimatePeak(core: ExportCore, item: BatchItem, tier: SizeTier, fonts: BatchFonts): Promise<number> {
  const fileBytes = Math.max(item.dng?.size ?? 0, item.jpg?.size ?? 0);
  const reference = { source: CEILING_SOURCE_PX, photo: CEILING_SOURCE_PX, canvas: CEILING_CANVAS_PX, fileBytes, hdr: item.req.hdr };
  try {
    return await measured(core, item, tier, fonts, fileBytes);
  } catch {
    return stagePeakEstimate(reference);
  }
}

async function measured(core: ExportCore, item: BatchItem, tier: SizeTier, fonts: BatchFonts, fileBytes: number): Promise<number> {
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
  // 不顺带发这一批的设置：它由这一张的 run() 自己发（发不出去照旧算这一张的陷阱）。
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
    fileBytes,
    hdr: item.req.hdr,
  });
}
