// 相机配方文件（GXRCE.DAT / GXRFX.BIN）的元数据提取、状态文案与标签不匹配判定。
// 纯函数，不碰 DOM、WASM 或 IndexedDB。

import { DAT_LIBRARY_GENERATIONS, TAG_CATALOG_GENERATIONS, type ParsedRecipeTag } from './recipe_tags.ts';

/** GXRFX.BIN 偏移 84..89（5 字节）为封存目录 ID，与相机写入 EXIF Copyright 的 10 位十六进制标识一致。 */
const CATALOG_ID_START = 84;
const CATALOG_ID_END = 89;

export function extractLibraryCatalogId(library: ArrayBuffer | Uint8Array | null | undefined): string | null {
  if (!library) return null;
  const view = library instanceof Uint8Array ? library : new Uint8Array(library);
  if (view.byteLength < CATALOG_ID_END) return null;
  let hex = '';
  for (let i = CATALOG_ID_START; i < CATALOG_ID_END; i += 1) {
    hex += view[i].toString(16).padStart(2, '0');
  }
  return hex;
}

export async function sha256Hex(buffer: ArrayBuffer | Uint8Array): Promise<string> {
  const bytes = buffer instanceof Uint8Array ? new Uint8Array(buffer) : new Uint8Array(buffer.slice(0));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export function camFilesRememberedLabel(generation: number | null): string {
  return typeof generation === 'number' ? `已记住 · 第 ${generation} 代` : '已记住';
}

export function camFilesGenerationText(generation: number | null): string {
  return typeof generation === 'number' ? `第 ${generation} 代` : '当前文件';
}

export type CamFilesMismatchReason = 'library_generation' | 'catalog_identity' | 'unknown_catalog';

export interface CamFilesMismatch {
  code: 'camera_recipe_catalog_missing';
  reason: CamFilesMismatchReason;
  tagCatalogId: string;
  loadedCatalogId: string | null;
  loadedGeneration: number | null;
}

export interface LoadedCamFilesMeta {
  generation: number | null;
  libraryCatalogId: string | null;
}

/**
 * 核对照片拍摄标签与已记住的相机配方文件是否匹配：
 * - 同库不同固件代数（如 g262 与 g266 共用 KUBIK 库 0ccc86c19b）解析一致，不算不匹配；
 * - 跨库代数（如单文件 g158 / g176 对 g266 照片）、同 178 代内不同库（如非 KUBIK g185 / g250 对 KUBIK 照片）
 *   或未知未来目录 ID 均判为不匹配，绝不套用错误配方。
 */
export function describeCamFilesMismatch(
  tag: ParsedRecipeTag | null,
  dat: LoadedCamFilesMeta | null | undefined,
): CamFilesMismatch | null {
  if (!tag || !dat || typeof dat.generation !== 'number') return null;
  const tagLibGen = TAG_CATALOG_GENERATIONS[tag.catalogIdentity] ?? null;
  if (tagLibGen === null) {
    if (dat.libraryCatalogId && dat.libraryCatalogId === tag.catalogIdentity) return null;
    return {
      code: 'camera_recipe_catalog_missing',
      reason: 'unknown_catalog',
      tagCatalogId: tag.catalogIdentity,
      loadedCatalogId: dat.libraryCatalogId,
      loadedGeneration: dat.generation,
    };
  }
  const datLibGen = DAT_LIBRARY_GENERATIONS[dat.generation] ?? null;
  if (datLibGen !== tagLibGen) {
    return {
      code: 'camera_recipe_catalog_missing',
      reason: 'library_generation',
      tagCatalogId: tag.catalogIdentity,
      loadedCatalogId: dat.libraryCatalogId,
      loadedGeneration: dat.generation,
    };
  }
  if (dat.libraryCatalogId !== null && dat.libraryCatalogId !== tag.catalogIdentity) {
    return {
      code: 'camera_recipe_catalog_missing',
      reason: 'catalog_identity',
      tagCatalogId: tag.catalogIdentity,
      loadedCatalogId: dat.libraryCatalogId,
      loadedGeneration: dat.generation,
    };
  }
  return null;
}

export function camFilesMismatchMessage(generation: number | null, hasJpg: boolean): string {
  const cur = typeof generation === 'number' ? `（已记住第 ${generation} 代）` : '';
  const fallback = hasJpg ? '中性转换或相机 JPG' : '中性转换';
  return `这些照片拍摄用的配方库和当前记住的相机配方文件${cur}不一致。请重新选择这张存储卡最外层的 GXRCE.DAT 和 GXRFX.BIN；更换前只显示${fallback}，不会套用错误配方。`;
}
