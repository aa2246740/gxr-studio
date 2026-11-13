// 帧时间。空闲时先量刷新间隔；拖动或自动测试期间记每一帧，算出 p50、p95、最大值和丢帧。

export interface PerfStats {
  p50: number;
  p95: number;
  max: number;
  dropped: number;
  interval: number;
  samples: number;
}

function quantile(sorted: number[], p: number): number {
  if (!sorted.length) return 0;
  const index = Math.min(sorted.length - 1, Math.max(0, Math.round(p * (sorted.length - 1))));
  return sorted[index];
}

export function summarize(deltas: number[], interval: number): PerfStats {
  const sorted = [...deltas].sort((a, b) => a - b);
  const dropped = deltas.filter((delta) => delta > 1.5 * interval).length;
  return {
    p50: quantile(sorted, 0.5),
    p95: quantile(sorted, 0.95),
    max: sorted[sorted.length - 1] ?? 0,
    dropped,
    interval,
    samples: deltas.length,
  };
}

/** 空闲大约 1 秒，用 rAF 间隔的中位数当作这块屏幕的刷新间隔。 */
export function measureInterval(): Promise<number> {
  return new Promise((resolve) => {
    const deltas: number[] = [];
    let last = performance.now();
    const started = last;
    const tick = (now: number) => {
      deltas.push(now - last);
      last = now;
      if (now - started < 1000) requestAnimationFrame(tick);
      else resolve(quantile([...deltas].sort((a, b) => a - b), 0.5) || 16.7);
    };
    requestAnimationFrame(tick);
  });
}

/** 开始记帧。`stop` 返回这段时间的统计。 */
export function watchFrames(interval: number): { stop: () => PerfStats } {
  const deltas: number[] = [];
  let last = performance.now();
  let handle = 0;
  const tick = (now: number) => {
    deltas.push(now - last);
    last = now;
    handle = requestAnimationFrame(tick);
  };
  handle = requestAnimationFrame(tick);
  return {
    stop: () => {
      cancelAnimationFrame(handle);
      return summarize(deltas, interval);
    },
  };
}

export function formatStats(stats: PerfStats): string {
  const ms = (value: number) => value.toFixed(1);
  return `p50 ${ms(stats.p50)} ms · p95 ${ms(stats.p95)} ms · 最大 ${ms(stats.max)} ms · 丢帧 ${stats.dropped}`;
}
