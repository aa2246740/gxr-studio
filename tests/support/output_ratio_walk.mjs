// 成品比例的真实浏览器回归：数字和舞台当帧改变，导出沿用同一版面。
export async function walkOutputRatios({ evalIn }) {
  return evalIn(`(async () => {
    const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const until = async (fn) => {
      const deadline = performance.now() + 30000;
      while (performance.now() < deadline) { const value = await fn(); if (value) return value; await wait(25); }
      throw new Error('成品比例检查等待超时');
    };
    const raf = () => new Promise((resolve) => requestAnimationFrame(resolve));
    const fonts = await import('/assets/finish_fonts.js');
    await Promise.all(['atkinson', 'inter', 'courier', 'patrick'].map(fonts.loadFont));
    document.querySelector('#tab-frame').click();
    const card = (name) => [...document.querySelectorAll('.fcard')].find((node) => node.textContent.trim() === name);
    card('白边').click();
    await until(() => document.querySelector('.frame-size')?.textContent.includes('像素'));
    const labels = [...document.querySelector('select[aria-label="成品比例"]').options].map((option) => option.textContent);
    await until(() => window.__fetchLog.inflight === 0 && performance.now() - window.__fetchLog.lastAt > 800);
    let queries = 0;
    const realFetch = window.fetch.bind(window);
    window.fetch = (...args) => {
      if (String(args[0]).includes('/output-size')) queries++;
      return realFetch(...args);
    };
    const photoId = location.hash.split('/').pop();
    const api = async (path, body) => {
      const response = await fetch('/api/photos/' + photoId + path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
      if (!response.ok) throw new Error(path + ' ' + response.status);
      return response.json();
    };
    const samples = [];
    for (const name of ['白边', '徕卡水印', '哈苏水印', '胶片', '拍立得']) {
      card(name).click();
      for (const ratio of ['1:1', '4:5', '3:4', '2:3', '9:16', '16:9', '3:2', 'original']) {
        const before = {renders:window.__fetchLog.renders, queries};
        const start = performance.now();
        const select = document.querySelector('select[aria-label="成品比例"]');
        select.value = ratio;
        select.dispatchEvent(new Event('change', {bubbles:true}));
        const immediate = document.querySelector('.frame-size').textContent;
        await raf();
        const ms = performance.now() - start;
        const stage = document.querySelector('.pv.framed').getBoundingClientRect();
        const key = document.querySelector('.pv-deco').dataset.key;
        const clickRenders = window.__fetchLog.renders - before.renders;
        const clickQueries = queries - before.queries;
        await until(async () => {
          const response = await fetch('/api/photos/' + photoId + '/edit');
          if (!response.ok) throw new Error('无法读取保存后的边框');
          const data = await response.json();
          return data.frame?.output_ratio === ratio;
        });
        const size = await api('/output-size', {});
        const exported = await api('/export', {size:'original'});
        samples.push({name, ratio, ms, immediate, key, stage:[stage.width, stage.height],
          size:size.sizes.original, exported:[exported.width, exported.height], clickRenders, clickQueries});
      }
    }
    window.fetch = realFetch;
    return {labels, samples};
  })()`, 120000);
}
