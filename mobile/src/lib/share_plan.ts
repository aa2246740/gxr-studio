// 交给系统共享面板的分批，和导出面板上的尺寸那一行。纯函数。

/** 一次共享最多几张、多少字节。iOS 的真实上限还不知道（真机检查量），先按这两个数分批。 */
export const SHARE_MAX_FILES = 20;
export const SHARE_MAX_BYTES = 256 * 1024 * 1024;

/**
 * 按顺序切成几批：每批不超过 `maxFiles` 张、`maxBytes` 字节。单张就超过字节上限时自己一批（不能拆开）。
 * 返回每批的下标。
 */
export function shareChunks(sizes: readonly number[], maxFiles = SHARE_MAX_FILES, maxBytes = SHARE_MAX_BYTES): number[][] {
  const out: number[][] = [];
  let cur: number[] = [];
  let bytes = 0;
  const files = Math.max(1, Math.floor(maxFiles));
  sizes.forEach((size, i) => {
    if (cur.length && (cur.length >= files || bytes + size > maxBytes)) {
      out.push(cur);
      cur = [];
      bytes = 0;
    }
    cur.push(i);
    bytes += size;
  });
  if (cur.length) out.push(cur);
  return out;
}

/** 05c 的按钮：只有一批时是「存到相册」（或「存 N 张到相册」），几批时写第几批。 */
export function shareLabel(batch: number, batches: number, base: string): string {
  return batches > 1 ? `存到相册（第 ${batch + 1} 批，共 ${batches} 批）` : base;
}

/** 桌面 output_size.js 的 ratioText。 */
export function ratioText(ratio: readonly [number, number] | null | undefined): string {
  if (!ratio) return '';
  const [w, h] = ratio;
  if (w === 191 && h === 100) return '1.91:1';
  if (w === 100 && h === 191) return '1:1.91';
  if (w === 47 && h === 20) return '2.35:1';
  if (w === 20 && h === 47) return '1:2.35';
  if (w === 8 && h === 5) return '16:10';
  if (w === 5 && h === 8) return '10:16';
  return `${w}:${h}`;
}

/** 桌面 output_size.js 的 sizeText。 */
export function sizeText(size: readonly [number, number], ratio: readonly [number, number] | null | undefined): string {
  return `${size[0]} × ${size[1]} 像素${ratio ? ` · ${ratioText(ratio)}` : ''}`;
}

export const MANY_SIZES = '每张照片按各自比例导出，成品尺寸可能不同。';
export const SIZE_PENDING = '正在计算成品尺寸…';
export const SIZE_FAILED = '无法读取成品尺寸。请关闭后重试。';
