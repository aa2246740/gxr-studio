// sidecar、草稿和撤销快照共用 { master?, r?, g?, b? }。恒等通道省略；全恒等为 null。
export const CURVE_MAX = 16;
export const CURVE_CHANNELS = [['master', '主曲线'], ['r', '红'], ['g', '绿'], ['b', '蓝']];
export const IDENTITY_CURVE = [[0, 0], [255, 255]];

export function normalizeCurves(curves) {
  const out = {};
  for (const [key] of CURVE_CHANNELS) {
    const points = curves?.[key];
    if (points && !points.every(([x, y]) => x === y)) out[key] = points.map((p) => [...p]);
  }
  return Object.keys(out).length ? out : null;
}

export function curveBody(ed) {
  return normalizeCurves(ed.curve);
}
