// 导出开始前先等默认校准落定（2026-11-24）：页面刚打开、内置的默认校准还在下载或核对时就点了导出，这一批会
// 悄悄按中性转换做出来。这里最多等 CALIB_WAIT_MS，落定了就按它导出；到时还没好就照旧开始（中性转换，「未校准」照旧写原因）。
// 几次点击按先后排队，等完一个个开始（后一个开始时能看到前一个建好的批次，去重照旧）；等的时候清空了照片就不开始。

import type { Book } from './book';

/** 最多等几秒：默认校准只有约 300 KB，正常几百毫秒就核对完。 */
export const CALIB_WAIT_MS = 4000;

/** 返回一个「等默认校准落定再做」的函数。`book`、`session` 每次现取。 */
export function calibGate(book: () => Book, session: () => number, ms: number = CALIB_WAIT_MS): (go: () => void) => void {
  let pending: Promise<void> | null = null;
  return (go) => {
    const current = book();
    if (!pending && !current.loadingDefault) {
      go();
      return;
    }
    const started = session();
    const job: Promise<void> = (pending ?? Promise.resolve())
      .then(() => current.calibSettled(ms))
      .then(() => {
        if (session() === started) go();
      })
      // 开始导出时抛的错照旧报给页面（以前是同步抛在点击里），不留未处理的 rejection。
      .catch((err: unknown) => reportError(err))
      .finally(() => {
        if (pending === job) pending = null;
      });
    pending = job;
  };
}
