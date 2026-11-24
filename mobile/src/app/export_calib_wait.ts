// 导出开始前先等默认校准落定（2026-11-24）：页面刚打开、内置的默认校准还在下载或核对时就点了导出，这一批会
// 悄悄按中性转换做出来。这里最多等 CALIB_WAIT_MS，落定了就按它导出；到时还没好就照旧开始（中性转换，「未校准」照旧写原因）。
// 等的时候再点几次，都排进同一次等待（截止时间也是同一个，见 Book.calibSettled），落定或到时后在同一轮里按先后一个个
// 开始：后一个开始时已经能看到前一个建好的批次，去重照旧。这一轮已经到时了，再点就不再等，马上开始。
// 等的时候清空了照片，那之前的点击就不开始。

import type { Book } from './book';

/** 最多等几秒：默认校准只有约 300 KB，正常几百毫秒就核对完。 */
export const CALIB_WAIT_MS = 4000;

/** 等校准这件事只用到 Book 的这两样（测试里可以换成假的）。 */
export type CalibWaiter = Pick<Book, 'loadingDefault' | 'calibSettled'>;

/** 返回一个「等默认校准落定再做」的函数。`book`、`session` 每次现取。 */
export function calibGate(book: () => CalibWaiter, session: () => number, ms: number = CALIB_WAIT_MS): (go: () => void) => void {
  /** 正在等的点击，按先后。null 表示现在没有在等。 */
  let queue: Array<{ go: () => void; session: number }> | null = null;
  const drain = (): void => {
    const waiting = queue ?? [];
    queue = null;
    for (const item of waiting) {
      if (session() !== item.session) continue;
      try {
        item.go();
      } catch (err) {
        // 开始导出时抛的错照旧报给页面（以前是同步抛在点击里），不让它拦住后面排着的点击。
        reportError(err);
      }
    }
  };
  return (go) => {
    if (queue) {
      queue.push({ go, session: session() });
      return;
    }
    const current = book();
    if (!current.loadingDefault) {
      go();
      return;
    }
    queue = [{ go, session: session() }];
    current
      .calibSettled(ms)
      .then(drain, drain)
      .catch((err: unknown) => reportError(err));
  };
}
