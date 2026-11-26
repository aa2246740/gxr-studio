// 手机测试用的一个触摸点（2026-11-26）。没有触摸点、指针精确的浏览器算「电脑」，做好的成片改成直接下载；
// 无头 Chromium 不开 hasTouch 时正是这样。这些驱动测的是手机，所以给页面一个触摸点，仍走系统共享面板。
// 只给 1 个：isIpadDevice 要多于 1 个才当 iPad，文案和版式不变。用法：`await context.addInitScript(phoneTouch)`。

export function phoneTouch() {
  Object.defineProperty(Navigator.prototype, 'maxTouchPoints', { configurable: true, get: () => 1 });
}
