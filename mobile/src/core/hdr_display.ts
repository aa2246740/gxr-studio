// 这台屏幕能不能显示 HDR。结果缓存一次。模拟器和无头 Chrome 的 media 不匹配，自然是 false。
// 测试用 debugForceHdrDisplay 覆盖。

let cached: boolean | null = null;
let forced: boolean | null = null;

export function debugForceHdrDisplay(value: boolean | null): void {
  forced = value;
  cached = null;
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
  const range = typeof matchMedia === 'function' && matchMedia('(dynamic-range: high)').matches;
  cached = Boolean(range && device && canvasAcceptsExtended(device));
  return cached;
}

export function previewWanted(display: boolean, hasDng: boolean, exportOn: boolean): boolean {
  return display && hasDng && exportOn;
}
