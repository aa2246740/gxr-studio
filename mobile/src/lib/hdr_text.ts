// 导出 HDR 的几句话和计数。纯函数，不碰 DOM。原话来自设计稿 05a / 05b / 05c 和桌面
// （web/assets/export.js 的 HDR_NO_RAW、hdrAvailable、hdrSummary，api/routes.py 的 HDR_NOTES）。

/** 05a 开关下面那句。 */
export const HDR_LINE = '在 iPhone 上看，亮处会更亮，更接近你当时看到的；别的设备照常显示。';
/** 桌面 HDR_NO_RAW：选中的照片一张 RAW 都没有时，开关停用，写这句。 */
export const HDR_NO_RAW = 'HDR 需要 RAW 文件';
/** 05b 的第二行（开了 HDR）。 */
export const HDR_PROGRESS = '生成 HDR 版本要多等几秒。可以接着看照片。';
/** 桌面 api/routes.py 的 HDR_NOTES 和 HDR_UNSUPPORTED：没做成 HDR 的原因，按 hdr_reason。 */
export const HDR_NOTES: Readonly<Record<string, string>> = {
  no_raw: 'HDR 需要 RAW 文件',
  not_jpeg: 'HDR 需要 JPEG 格式',
  capture_mismatch: 'RAW 与 JPG 不是同一次拍摄',
  frame_layout: '这个边框样式不能存 HDR',
};
export const HDR_UNSUPPORTED = '这张照片不支持 HDR';

/** 桌面 hdrAvailable（按所选照片）：至少一张有 RAW 才能开 HDR。 */
export function hdrAvailable(photos: ReadonlyArray<{ dng: unknown }>): boolean {
  return photos.some((p) => p.dng != null);
}

/** 05a：开了 HDR、选中里有只有 JPG 的照片时那一行。没有就是 null。 */
export function hdrMixLine(photos: ReadonlyArray<{ dng: unknown }>): string | null {
  const jpgOnly = photos.filter((p) => p.dng == null).length;
  if (!jpgOnly || jpgOnly === photos.length) return null;
  return `${HDR_NO_RAW}：${jpgOnly} 张只有 JPG，会存普通版本。`;
}

/** 05a：开了 HDR、有带 RAW 且用了模糊填充边框的照片时提醒，没有就是 null。 */
export function hdrFrameLine(photos: ReadonlyArray<{ dng: unknown; border?: { style?: string } | null }>): string | null {
  const n = photos.filter((p) => p.dng != null && p.border?.style === 'blur_fill').length;
  return n ? `${n} 张用的边框样式不能存 HDR，会存普通版本。` : null;
}

/** 桌面 hdrSummary：批量里没做成 HDR 的张数。 */
export function hdrSummary(n: number): string {
  return `${n} 张未生成 HDR`;
}

/** 桌面单张结果的 hdr_note：`HDR_NOTES.get(hdr_reason, HDR_UNSUPPORTED)`。 */
export function hdrNote(reason: unknown): string {
  return typeof reason === 'string' && Object.hasOwn(HDR_NOTES, reason) ? HDR_NOTES[reason] : HDR_UNSUPPORTED;
}

/** 一张做好的照片：主干名和 export_complete 的结果（`hdr`、`hdr_reason`）。 */
export interface HdrRow {
  stem: string;
  result: Record<string, unknown> | undefined;
}

/**
 * 05c / 05e 的第二行和逐张说明。只有 JPG（`no_raw`）的合成一句「N 张只有 JPG，存普通版本」；别的原因按桌面
 * hdrSummary 计数，逐张写桌面的 hdr_note。核心说了算：`hdr` 为真才算 HDR。
 */
export function hdrResultText(rows: readonly HdrRow[]): { line: string; notes: string[] } {
  let made = 0;
  let jpgOnly = 0;
  const notes: string[] = [];
  for (const { stem, result } of rows) {
    if (!result) continue;
    if (result.hdr === true) made += 1;
    else if (result.hdr === false && result.hdr_reason === 'no_raw') jpgOnly += 1;
    else if (result.hdr === false) notes.push(`${stem}：${hdrNote(result.hdr_reason)}`);
  }
  const parts: string[] = [];
  if (made) parts.push(`${made} 张 HDR`);
  if (jpgOnly) parts.push(`${jpgOnly} 张只有 JPG，存普通版本`);
  if (notes.length) parts.push(hdrSummary(notes.length));
  return { line: parts.join(' · '), notes };
}
