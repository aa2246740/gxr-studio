// 定时刷新：同一件事一次只有一个请求在路上（上一个没回来就不发下一个），
// 页面在后台（document.hidden）时不发，回到前台立刻补一次（U15）。

export function every(ms, task, { doc = globalThis.document } = {}) {
  let running = false;
  let stopped = false;
  let timer = null;
  const run = async () => {
    if (stopped || running || (doc && doc.hidden)) return;
    running = true;
    try {
      await task();
    } catch {
      // 每个任务自己处理错误；这里只保证下一轮照常
    } finally {
      running = false;
    }
  };
  const schedule = () => {
    timer = setTimeout(async () => {
      await run();
      if (!stopped) schedule();
    }, ms);
  };
  const onVisible = () => {
    if (doc && !doc.hidden) run();
  };
  doc?.addEventListener?.('visibilitychange', onVisible);
  schedule();
  return {
    now: run,
    stop() {
      stopped = true;
      clearTimeout(timer);
      doc?.removeEventListener?.('visibilitychange', onVisible);
    },
  };
}
