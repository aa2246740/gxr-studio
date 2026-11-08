export async function walkStampBorder({ evalIn }) {
  return evalIn(`(async () => {
    const until = async (fn) => {
      const deadline = performance.now() + 20000;
      while (performance.now() < deadline) {
        const value = await fn(); if (value) return value;
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
      throw new Error('拍立得印记没有保存或画出来');
    };
    document.querySelector('#tab-frame').click();
    const frame = [...document.querySelectorAll('#tp-frame .fcard')].find((b) => b.textContent.includes('拍立得'));
    frame.click();
    document.querySelector('#tab-stamp').click();
    const toggle = document.querySelector('#tp-stamp [role=switch]');
    if (toggle.getAttribute('aria-checked') !== 'true') toggle.click();
    const border = [...document.querySelectorAll('#tp-stamp button')].find((b) => b.textContent === '拍立得边框上');
    if (!border) throw new Error('没有拍立得边框上的印记选项');
    border.click();
    const photoId = location.hash.split('/').pop();
    const saved = await until(async () => {
      const edit = await (await fetch('/api/photos/' + photoId + '/edit')).json();
      return edit.stamp?.placement === 'border' ? edit : null;
    });
    await until(() => document.querySelector('.pv-deco')?.dataset.key.includes('"placement":"border"'));
    return { frame: saved.frame.style, stamp: saved.stamp, preview: !!document.querySelector('.pv.framed .pv-deco') };
  })()`);
}
