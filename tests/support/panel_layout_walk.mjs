// 面板底部操作跟内容排在一起，不能盖住最后一张样式卡片或曲线。
export async function walkPanelLayout({ call, evalIn }, tab, widths) {
  const rows = [];
  for (const width of widths) {
    for (const height of [600, 800, 1000]) {
      await call('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
      rows.push(await evalIn(`(async () => {
        document.querySelector('#tab-${tab}').click();
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const pane = document.querySelector('#tp-${tab}');
        const body = pane.closest('.panel-body');
        body.scrollTop = 0;
        const target = pane.querySelector('${tab === 'frame' ? '.fcard:last-child' : '.curve'}');
        const button = [...pane.querySelectorAll('button')].find((b) => b.textContent === '${tab === 'frame' ? '设为默认' : '重置'}');
        const samples = [];
        for (const scroll of [0, body.scrollHeight / 2, body.scrollHeight]) {
          body.scrollTop = scroll;
          const content = target.getBoundingClientRect();
          const foot = button.parentElement.getBoundingClientRect();
          samples.push({ bottom: content.bottom, footTop: foot.top, gap: foot.top - content.bottom });
        }
        return { width: innerWidth, height: innerHeight, samples };
      })()`));
    }
  }
  return rows;
}
