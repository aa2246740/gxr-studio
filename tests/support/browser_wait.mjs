// 固定 Playwright 的 waitForFunction 不等待异步谓词，Promise 会直接命中。
// 模块状态需异步读取时，用 page.evaluate 等到实际值，再决定是否继续轮询。
export async function waitForPage(page, predicate, arg = null, { timeout = 30000 } = {}) {
  const deadline = Date.now() + timeout;
  do {
    const value = await page.evaluate(predicate, arg);
    if (value) return value;
    await page.waitForTimeout(50);
  } while (Date.now() < deadline);
  throw new Error(`页面条件在 ${timeout} 毫秒内没有满足`);
}
