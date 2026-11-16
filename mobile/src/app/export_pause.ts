// 导出跑着的时候，配方卡小图和烘焙（M2 的队列）先停，免得和导出 Worker 抢 CPU。滑块、滚动、打开照片照常。

let paused = false;
let waiters: Array<() => void> = [];

export function exportPaused(): boolean {
  return paused;
}

export function setExportPaused(on: boolean): void {
  paused = on;
  if (on) return;
  const ready = waiters;
  waiters = [];
  for (const done of ready) done();
}

/** 导出没在跑就马上 resolve，否则等它停下（或这一批结束）。 */
export function whenExportIdle(): Promise<void> {
  if (!paused) return Promise.resolve();
  return new Promise((done) => waiters.push(done));
}
