// 换照片、换边框、进出裁剪、100% 之后，大图还在不在（tests/test_frame_navigation_browser.py 起服务器和 Chrome，
// viewer_browser.mjs 的 nav-gpu / nav-server 模式调这里）。
// 起因：有边框的照片点「下一张」，显卡画布（整页唯一一块，换照片时搬进新的 .pv）身上还留着上一张边框写的定位，
// 新 .pv 缩成 0 × 0，大图一片空白，刷新才好。
// 每一步之后量：舞台和 .pv 有尺寸、放得进窗口，照片真的画出来了（取像素，不只看有没有元素），.pv 中心点点得到的是 .pv 里的东西，
// 没有上一张版面留下的行内样式。等的都是「画面成了想要的样子」，不是固定的睡眠：机器忙的时候慢一点，不会误报。

const FRAMES = {
  gallery: { style: 'gallery', color: 'white', weight: 'medium', show_logo: true, show_params: true, show_date: false, text: '', canvas_ratio: '4:5' },
  leica: { style: 'leica', color: 'white', weight: 'medium', show_logo: true, show_params: true, show_date: false, text: 'Leica', canvas_ratio: '4:5' },
  hasselblad: { style: 'hasselblad', color: 'white', weight: 'medium', show_logo: true, show_params: true, show_date: false, text: '', canvas_ratio: '4:5' },
  film: { style: 'film', color: 'white', weight: 'medium', show_logo: true, show_params: true, show_date: false, text: '', canvas_ratio: '4:5' },
  instant: { style: 'instant', color: 'white', weight: 'medium', show_logo: true, show_params: true, show_date: false, text: 'Hello', canvas_ratio: '4:5' },
  canvas: { style: 'canvas', color: 'white', weight: 'medium', show_logo: true, show_params: true, show_date: false, text: '', canvas_ratio: '4:5' },
};
const STAMP = { enabled: true, content: 'text', text: 'Paris', font: 'typewriter', color: 'orange', size: 'large', corner: 'br', glow: false };

// 两轮，每轮六张照片各自的边框 / 时间戳，和走的路线（照片序号，加上到之前要做的事）。
// 第一轮：没有 → 没有 → 白边 → 徕卡 → 拍立得 + 日期戳 → 只有日期戳，再原路退回来；第二轮：换一批样式，再走一遍。
const ROUNDS = [
  {
    plan: [{}, {}, { frame: 'gallery' }, { frame: 'leica' }, { frame: 'instant', stamp: true }, { stamp: true }],
    path: [0, 1, 2, 3, 4, 5, 4, 3, { to: 2, zoomBefore: true }, 1, 0, 1, { to: 2, cropBefore: true }, 1],
  },
  {
    plan: [{ frame: 'hasselblad' }, { frame: 'film' }, { frame: 'canvas' }, {}, {}, { frame: 'leica', stamp: true }],
    path: [0, 1, 2, 3, 4, 5, 4, 3, 2, { to: 1, zoomBefore: true }, 0, { to: 1, cropBefore: true }, 2],
  },
];

// ---- 以下在页面里跑 ----------------------------------------------------------------------------------------

async function inPage(arg) {
  const { round, frames, stamp, wantGpu } = arg;
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const until = async (fn, ms) => {
    const t = performance.now();
    while (performance.now() - t < ms) {
      const v = fn();
      if (v) return v;
      await wait(25);
    }
    return null;
  };
  const gpuStage = await import('/assets/gpu_stage.js');
  const listed = await (await fetch('/api/photos')).json();
  const photos = (listed.photos || listed.items || listed).slice(0, round.plan.length);
  // 这一轮每张照片的边框和时间戳
  for (const [i, p] of photos.entries()) {
    const want = round.plan[i];
    const body = { frame: want.frame ? frames[want.frame] : { style: 'none' }, stamp: want.stamp ? stamp : { enabled: false } };
    const r = await fetch(`/api/photos/${p.id}/edit`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(`PUT edit ${p.id}: ${r.status} ${await r.text()}`);
  }
  const click = (sel) => document.querySelector(sel).click();
  const press = (label) => [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === label).click();
  const box = (e) => {
    if (!e) return null;
    const b = e.getBoundingClientRect();
    return { x: b.x, y: b.y, w: b.width, h: b.height };
  };
  const stats = (ctx, w, h) => {
    const d = ctx.getImageData(0, 0, w, h).data;
    const seen = new Set();
    let sum = 0;
    let sum2 = 0;
    let n = 0;
    for (let i = 0; i < d.length; i += 4 * 17) {
      seen.add(((d[i] >> 3) << 10) | ((d[i + 1] >> 3) << 5) | (d[i + 2] >> 3));
      const l = d[i] * 0.3 + d[i + 1] * 0.6 + d[i + 2] * 0.1;
      sum += l;
      sum2 += l * l;
      n += 1;
    }
    const mean = sum / n;
    return { distinct: seen.size, std: Math.sqrt(Math.max(0, sum2 / n - mean * mean)) };
  };
  // 照片真的画出来了：显卡画布只在提交的那个任务里有内容，当场复制出来；服务器图直接画
  const pixels = (media) => {
    const c = document.createElement('canvas');
    c.width = 320;
    c.height = 200;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    if (media.tagName === 'CANVAS') return gpuStage.gpuSnapshot((cv) => { ctx.drawImage(cv, 0, 0, c.width, c.height); return stats(ctx, c.width, c.height); });
    if (!media.complete || !media.naturalWidth) return null;
    ctx.drawImage(media, 0, 0, c.width, c.height);
    return stats(ctx, c.width, c.height);
  };
  const inkOf = (deco) => {
    if (!deco) return 0;
    const d = deco.getContext('2d', { willReadFrequently: true }).getImageData(0, 0, deco.width, deco.height).data;
    let n = 0;
    for (let i = 3; i < d.length; i += 4 * 7) if (d[i] > 0) n += 1;
    return n;
  };
  const snap = (label, expect) => {
    const area = document.querySelector('.stage-area');
    const pvs = area ? area.querySelectorAll('.pv') : [];
    const pv = pvs[0] || null;
    const media = pv ? pv.querySelector('.pv-gpu, .pv-img') : null;
    const out = { label, hash: location.hash.slice(-16), expect, pvs: pvs.length, area: box(area), pv: box(pv), media: box(media) };
    if (!pv || !media) return out;
    const r = out.pv;
    const hit = document.elementFromPoint(r.x + r.w / 2, r.y + r.h / 2);
    Object.assign(out, {
      gpu: area.classList.contains('gpu'),
      zoom: area.classList.contains('zoom100'),
      framed: pv.classList.contains('framed'),
      decor: pv.dataset.decor || null,
      decos: pv.querySelectorAll('.pv-deco').length,
      pvStyle: pv.getAttribute('style') || '',
      mediaStyle: { position: media.style.position, left: media.style.left, top: media.style.top },
      fx: pv.style.getPropertyValue('--fx'),
      fy: pv.style.getPropertyValue('--fy'),
      bg: getComputedStyle(pv).backgroundColor,
      hit: !!hit && pv.contains(hit),
      pixels: pixels(media),
      ink: inkOf(pv.querySelector('.pv-deco')),
      viewport: { w: innerWidth, h: innerHeight },
    });
    return out;
  };
  // 等画面成了想要的样子（照片 id 对、边框和日期戳的记号对、显卡路径时画布在）：限时，到点就量到什么交什么
  const ready = (id, expect, tab) => () => {
    const area = document.querySelector('.stage-area');
    const pv = area && area.querySelector('.pv');
    const media = pv && pv.querySelector('.pv-gpu, .pv-img');
    if (!location.hash.endsWith(id) || !pv || !media) return false;
    if (tab === 'crop') return !!pv.querySelector('.crop-box');
    if (wantGpu && !area.classList.contains('gpu')) return false;
    if (!wantGpu && media.tagName === 'IMG' && !(media.complete && media.naturalWidth)) return false;
    const decorated = !!expect.frame || !!expect.stamp;
    if (pv.classList.contains('framed') !== !!expect.frame || !!pv.querySelector('.pv-deco') !== decorated) return false;
    // 带字的装饰（日期戳和徕卡、哈苏、胶片、拍立得）要等字体下载好、字画上去
    const texty = !!expect.stamp || ['leica', 'hasselblad', 'film', 'instant'].includes(expect.frame);
    return !texty || inkOf(pv.querySelector('.pv-deco')) > 0;
  };
  const settle = async (label, id, expect, tab) => {
    await until(ready(id, expect, tab), 20000);
    await wait(250);
    return snap(label, { ...expect, tab: tab || 'frame', id: id.slice(-16) });
  };

  const steps = [];
  let at = -1;
  const open = async (i) => {
    location.hash = `#/photo/${photos[i].id}`;
    at = i;
    await until(() => document.querySelector('#tab-frame') && document.querySelector('.stage-area .pv'), 30000);
    click('#tab-frame');
    steps.push(await settle(`open #${i}`, photos[i].id, round.plan[i]));
  };
  await open(0);
  for (const move of round.path.slice(1)) {
    const to = typeof move === 'number' ? move : move.to;
    if (typeof move !== 'number' && move.zoomBefore) {
      press('100%');
      await until(() => document.querySelector('.stage-area.zoom100'), 5000);
      steps.push(await settle(`#${at} at 100%`, photos[at].id, round.plan[at]));
    }
    if (typeof move !== 'number' && move.cropBefore) {
      click('#tab-crop');
      steps.push(await settle(`#${at} crop tab`, photos[at].id, {}, 'crop'));
      click('#tab-frame');
      steps.push(await settle(`#${at} back from crop`, photos[at].id, round.plan[at]));
    }
    click(to > at ? 'button[aria-label^="下一张"]' : 'button[aria-label^="上一张"]');
    const from = at;
    at = to;
    steps.push(await settle(`#${from} → #${to}`, photos[to].id, round.plan[to]));
  }
  return steps;
}

// ---- 以下在 node 里跑（viewer_browser.mjs 调）----------------------------------------------------------------

export async function walkRounds({ call, evalIn, origin, step, wantGpu }) {
  const all = [];
  for (const [n, round] of ROUNDS.entries()) {
    step(`walk the photos, round ${n + 1}`);
    // 每一轮整页重新加载（带不同的查询串，Chrome 不会只当成换 hash）：上一轮的编辑存在服务器上，这一轮前端重新读
    await call('Page.navigate', { url: `${origin}/?open=1&round=${n}` });
    await new Promise((r) => setTimeout(r, 1500));
    const arg = { round, frames: FRAMES, stamp: STAMP, wantGpu };
    all.push(...await evalIn(`(${inPage.toString()})(${JSON.stringify(arg)})`, 240000));
  }
  return all;
}
