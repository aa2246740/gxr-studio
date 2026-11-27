// 这台屏幕能不能显示 HDR。结果先缓存；窗口在 SDR 屏和 HDR 屏之间挪动时 `(dynamic-range: high)` 会变，缓存清掉，
// 再告诉 watchHdrDisplay 的订阅者重查（2026-11-26）。模拟器和无头 Chrome 的 media 不匹配，自然是 false。
// 测试用 debugForceHdrDisplay 覆盖，它也会通知订阅者。

const RANGE_QUERY = '(dynamic-range: high)';

let cached: boolean | null = null;
let forced: boolean | null = null;
const watchers = new Set<() => void>();
let range: MediaQueryList | null = null;

function changed(): void {
  cached = null;
  for (const fn of [...watchers]) fn();
}

export function debugForceHdrDisplay(value: boolean | null): void {
  forced = value;
  changed();
}

/** 屏幕能不能显示 HDR 变了（换了屏幕）时调 `fn`。返回取消订阅。 */
export function watchHdrDisplay(fn: () => void): () => void {
  watchers.add(fn);
  if (!range && typeof matchMedia === 'function') {
    range = matchMedia(RANGE_QUERY);
    range.addEventListener?.('change', changed);
  }
  return () => {
    watchers.delete(fn);
    if (watchers.size || !range) return;
    range.removeEventListener?.('change', changed);
    range = null;
  };
}

/** 线性倍数（1 = SDR 白）。浏览器没暴露就返回 null，权重退回 cap_max。 */
export function displayHeadroom(): number | null {
  const screen = globalThis.screen as Screen & { currentHdrHeadroom?: number };
  const value = screen?.currentHdrHeadroom;
  if (typeof value === 'number' && Number.isFinite(value) && value > 0) return value;
  return null;
}

export function canvasAcceptsExtended(device: GPUDevice): boolean {
  const canvas = document.createElement('canvas');
  const context = canvas.getContext('webgpu');
  if (!context) return false;
  try {
    context.configure({
      device,
      format: 'rgba16float',
      alphaMode: 'opaque',
      colorSpace: 'srgb',
      toneMapping: { mode: 'extended' },
    });
    const got = context.getConfiguration?.();
    context.unconfigure();
    if (!got) return false;
    const tone = (got as { toneMapping?: { mode?: string } }).toneMapping;
    return got.format === 'rgba16float' && tone?.mode === 'extended';
  } catch {
    return false;
  }
}

export function hdrDisplayCapable(device: GPUDevice | null): boolean {
  if (forced !== null) return forced;
  if (cached !== null) return cached;
  const high = typeof matchMedia === 'function' && matchMedia(RANGE_QUERY).matches;
  cached = Boolean(high && device && canvasAcceptsExtended(device));
  return cached;
}

export function previewWanted(display: boolean, hasDng: boolean, exportOn: boolean): boolean {
  return display && hasDng && exportOn;
}
