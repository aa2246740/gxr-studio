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
// 六张可渲染 JPG，含 RAW+JPG、只有 JPG 和竖拍；不依赖图库首六张恰好都能出图。
const PHOTO_NAMES = ['R0000101', 'R0000102', 'R0000103', 'R0000104', 'R0000105', 'R0000106'];

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

// 显卡预览能不能接手、并且接得住：打开第一张照片，等显卡画布出来，再看它能不能留住 holdMs。
// 限时 boundMs（软件显卡在慢机器上建管线、传源图要好几秒，和 gpu 模式一样给 120 秒）；明显退回服务器出图的不干等，马上交出去：
// 浏览器没有 WebGPU、没有适配器，或者引擎建起来过、之后又没了并且这样持续了 2 秒。
// 光有 .gpu 不够：Linux 无头 Chrome 里软件显卡能建设备、画布也会短暂出现，但上不了屏（共享图像建不起来、取不到），
// 页面随后退回服务器图（.gpu 没了），或者引擎还活着但画面从没真正出来。所以出现以后还要留住 holdMs，
// 中途 .gpu 没了就是 gpu-dropped；是不是 Chrome 日志里的共享图像错误，由 node 那边看日志定（页面里看不到）。
// 每一次状态变化都记进 timeline，判断用到的东西都放在 inputs 里，失败时一起交出去。
async function gpuTakesOver(arg) {
  const t0 = performance.now();
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
  const stage = await import('/assets/gpu_stage.js');
  const listed = await (await fetch('/api/photos')).json();
  const first = (listed.photos || listed.items || listed).find(p => p.has_jpg && arg.names.includes(p.name));
  location.hash = `#/photo/${first.id}`;
  const adapter = navigator.gpu ? await navigator.gpu.requestAdapter({ powerPreference: 'high-performance' }).catch(() => null) : null;
  const gpuStage = () => !!document.querySelector('.stage-area.gpu .pv');
  const engineState = () => {
    const e = stage.gpuEngineForTests();
    return !e ? 'none' : e.lost ? 'lost' : 'alive';
  };
  let sawEngine = false;
  let last = '';
  const timeline = [];
  const sample = () => {
    const key = `${gpuStage()}|${engineState()}`;
    if (engineState() === 'alive') sawEngine = true;
    if (key !== last && timeline.length < 60) timeline.push({ t: Math.round(performance.now() - t0), gpuStage: gpuStage(), engine: engineState() });
    last = key;
  };
  const done = (verdict) => ({
    verdict,
    waitMs: Math.round(performance.now() - t0),
    inputs: { navigatorGpu: !!navigator.gpu, adapter: !!adapter, gpuStage: gpuStage(), engine: engineState(), sawEngine, hash: location.hash.slice(-16) },
    timeline,
  });
  if (!adapter) return done('no-adapter');
  let goneSince = 0;
  const took = await until(() => {
    sample();
    if (gpuStage()) return 'gpu';
    if (engineState() === 'alive') {
      goneSince = 0;
      return null;
    }
    if (!sawEngine) return null;
    goneSince = goneSince || performance.now();
    return performance.now() - goneSince > 2000 ? 'engine-lost' : null;
  }, arg.boundMs);
  if (took !== 'gpu') return done(took || 'timeout');
  // 留得住吗：中途 .gpu 没了（页面退回服务器图），或者引擎没了，就是没接住
  const held = performance.now();
  while (performance.now() - held < arg.holdMs) {
    sample();
    if (!gpuStage()) return done('gpu-dropped');
    if (engineState() !== 'alive') return done('engine-lost');
    await wait(100);
  }
  return done('gpu');
}

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
  let sawEngine = false;
  const listed = await (await fetch('/api/photos')).json();
  const photos = (listed.photos || listed.items || listed).filter(p => p.has_jpg && arg.names.includes(p.name)).slice(0, round.plan.length);
  if (photos.length !== round.plan.length) throw new Error('边框导航夹具需要六张有效 JPG，包含竖拍');
  const { state } = await import('/assets/state.js');
  state.photos = photos;
  state.photosComplete = true;
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
  // 等画面成了想要的样子（照片 id 对、边框和日期戳的记号对、显卡路径时画布在）：限时，到点就量到什么交什么。
  // stale：点「下一张」之前的舞台。换照片时整个编辑区重建（viewer.js 打开照片时 buildLayout），新照片的数据到之前，
  // 地址已经是新照片了，屏幕上还是上一张的舞台；不认它，不然会把上一张的边框和字当成这一张的量
  const ready = (id, expect, tab, stale = null) => () => {
    const area = document.querySelector('.stage-area');
    if (stale && area === stale) return false;
    const pv = area && area.querySelector('.pv');
    const media = pv && pv.querySelector('.pv-gpu, .pv-img');
    if (!location.hash.endsWith(id) || !pv || !media) return false;
    if (tab === 'crop') return !!pv.querySelector('.crop-box');
    if (wantGpu && !area.classList.contains('gpu')) {
      // 走到一半显卡引擎没了（建起来过，之后失效）：不干等，照现在的样子量，断言会说「没走显卡」，而不是整段超时
      const e = gpuStage.gpuEngineForTests();
      if (e && !e.lost) sawEngine = true;
      return sawEngine && (!e || e.lost);
    }
    if (!wantGpu && media.tagName === 'IMG' && !(media.complete && media.naturalWidth)) return false;
    const decorated = !!expect.frame || !!expect.stamp;
    if (pv.classList.contains('framed') !== !!expect.frame || !!pv.querySelector('.pv-deco') !== decorated) return false;
    // 带字的装饰（日期戳和徕卡、哈苏、胶片、拍立得）要等字体下载好、字画上去
    const texty = !!expect.stamp || ['leica', 'hasselblad', 'film', 'instant'].includes(expect.frame);
    return !texty || inkOf(pv.querySelector('.pv-deco')) > 0;
  };
  // 整轮有预算（200 秒，比 evaluate 的 270 秒短）；任何一步等不到想要的样子就停：把这一步现在的样子交出去（timedOut），
  // 后面的步不再走。这样不管卡在哪里（显卡路径上画布没真正出来、字体一直不来……）都带着证据回来，不会整段超时。
  const deadline = performance.now() + 200000;
  let aborted = false;
  const engineState = () => {
    const e = gpuStage.gpuEngineForTests();
    return !e ? 'none' : e.lost ? 'lost' : 'alive';
  };
  const settle = async (label, id, expect, tab, boundMs = 20000, stale = null) => {
    const limit = Math.max(0, Math.min(boundMs, deadline - performance.now()));
    const t0 = performance.now();
    const isReady = ready(id, expect, tab, stale);
    let ok = await until(isReady, limit);
    await wait(250);
    // 停了 250 毫秒再看一次：又不是想要的样子了（比如又重画了一遍、字还没画上），就在剩下的时间里再等。
    // 最后这次判断和下面的量之间没有别的任务插进来，量到的就是判断过的样子
    if (ok && !isReady()) ok = await until(isReady, Math.max(0, limit - (performance.now() - t0)));
    const step = snap(label, { ...expect, tab: tab || 'frame', id: id.slice(-16) });
    if (!ok) Object.assign(step, { timedOut: true, waitedMs: Math.round(limit), engine: engineState(), sawEngine, staleStage: !!stale && document.querySelector('.stage-area') === stale });
    if (!ok) aborted = true;
    return step;
  };

  const steps = [];
  let at = -1;
  const open = async (i) => {
    location.hash = `#/photo/${photos[i].id}`;
    at = i;
    await until(() => document.querySelector('#tab-frame') && document.querySelector('.stage-area .pv'), 30000);
    click('#tab-frame');
    // 整页刚加载：显卡要重新建、重新传源图，第一步多等一会儿（之后的每一步都很快）
    steps.push(await settle(`open #${i}`, photos[i].id, round.plan[i], undefined, wantGpu ? 120000 : 20000));
  };
  await open(0);
  for (const move of round.path.slice(1)) {
    if (aborted) break;
    const to = typeof move === 'number' ? move : move.to;
    if (typeof move !== 'number' && move.zoomBefore) {
      press('100%');
      await until(() => document.querySelector('.stage-area.zoom100'), 5000);
      steps.push(await settle(`#${at} at 100%`, photos[at].id, round.plan[at]));
      if (aborted) break;
    }
    if (typeof move !== 'number' && move.cropBefore) {
      click('#tab-crop');
      steps.push(await settle(`#${at} crop tab`, photos[at].id, {}, 'crop'));
      if (aborted) break;
      click('#tab-frame');
      steps.push(await settle(`#${at} back from crop`, photos[at].id, round.plan[at]));
      if (aborted) break;
    }
    const stale = document.querySelector('.stage-area');
    click(to > at ? 'button[aria-label^="下一张"]' : 'button[aria-label^="上一张"]');
    const from = at;
    at = to;
    steps.push(await settle(`#${from} → #${to}`, photos[to].id, round.plan[to], undefined, 20000, stale));
  }
  return steps;
}

// ---- 以下在 node 里跑（viewer_browser.mjs 调）----------------------------------------------------------------

export async function walkRounds({ call, evalIn, origin, step, wantGpu, collectGpuDiagnostics, presentErrors, pauseMs = 1500 }) {
  let gpuWaitMs = null;
  if (wantGpu) {
    // 先等一次显卡接手并且留得住（有上限，明显退回服务器出图的马上交出去）；接不了手就只交出一步 gpu: false、判断用到的东西和诊断，
    // 由测试按平台决定是跳过还是失败，不在后面一步步等、最后整段超时
    step('wait for the WebGPU canvas');
    await call('Page.navigate', { url: `${origin}/?open=1&round=gpu` });
    await new Promise((r) => setTimeout(r, pauseMs));
    const took = await evalIn(`(${gpuTakesOver.toString()})(${JSON.stringify({ boundMs: 120000, holdMs: 3000, names: PHOTO_NAMES })})`, 135000);
    gpuWaitMs = took.waitMs;
    // 画布留住了也不一定上得了屏：Chrome 自己的日志里有共享图像的错（建不起来、取不到），就是上不了屏（页面里看不到这个）
    const presentError = presentErrors();
    const verdict = took.verdict === 'gpu' && presentError ? 'cannot-present' : took.verdict;
    if (verdict !== 'gpu') {
      await collectGpuDiagnostics();
      return { steps: [{ label: 'WebGPU did not take over', gpu: false, ...took, verdict, presentError }], gpuWaitMs };
    }
  }
  const all = [];
  for (const [n, round] of ROUNDS.entries()) {
    step(`walk the photos, round ${n + 1}`);
    // 每一轮整页重新加载（带不同的查询串，Chrome 不会只当成换 hash）：上一轮的编辑存在服务器上，这一轮前端重新读
    await call('Page.navigate', { url: `${origin}/?open=1&round=${n}` });
    await new Promise((r) => setTimeout(r, pauseMs));
    const arg = { round, frames: FRAMES, stamp: STAMP, wantGpu, names: PHOTO_NAMES };
    const steps = await evalIn(`(${inPage.toString()})(${JSON.stringify(arg)})`, 270000);
    all.push(...steps);
    if (steps.some((x) => x.timedOut)) {
      // 有一步等不到想要的样子：后面的轮不走了；显卡路径上还要把诊断一起交出去
      if (wantGpu && steps.some((x) => x.timedOut && !x.gpu)) await collectGpuDiagnostics();
      break;
    }
  }
  return { steps: all, gpuWaitMs };
}
