// 真浏览器里的裁切点按和边框卡片（tests/test_viewer_browser.py 起服务器和 Chrome，这里用 DevTools 协议驱动）。
// 用法：node viewer_browser.mjs <chrome 路径> <端口> <令牌> <照片 id> <gpu|server> <用户目录>
// 额外的 Chrome 开关放在环境变量 CHROME_FLAGS（JSON 数组）。输出一行 JSON，断言在 Python 那边。
// 保存请求（PUT .../crop）用 Fetch 域挂住，模拟后端忙或慢：挂着的时候界面也必须马上有反应。
// 每一步都有超时，总时限在环境变量 VIEWER_DEADLINE_MS（Python 那边的进程超时之前）：卡住时也输出 JSON，
// 里面的 step 是卡住的那一步，chromeLog 是 Chrome 自己的输出末尾。进度逐行写到 stderr。
import { spawn } from 'node:child_process';
import { closeSync, openSync, readFileSync } from 'node:fs';

const [chromePath, port, token, photoId, mode, profile] = process.argv.slice(2);
const extra = JSON.parse(process.env.CHROME_FLAGS || '[]');
const deadlineMs = Number(process.env.VIEWER_DEADLINE_MS || 200000);
const debugPort = 19500 + Math.floor(Math.random() * 400);
const chromeLogPath = `${profile}.log`;
const chromeLog = openSync(chromeLogPath, 'w');
// macOS 上无头 Chrome 第一次存 cookie（带令牌打开页面时）会去钥匙串取加密密钥，CI 机器上这一步会一直挂着；
// 用假钥匙串和明文的密码存储绕开（只是测试用的临时用户目录）。别的平台不认这两个开关，没有影响。
const chrome = spawn(chromePath, [
  '--headless=new', `--user-data-dir=${profile}`, `--remote-debugging-port=${debugPort}`, '--no-first-run', '--no-default-browser-check',
  '--disable-extensions', '--disable-background-networking', '--use-mock-keychain', '--password-store=basic', '--window-size=1500,950',
  '--enable-logging=stderr', '--v=0', ...extra, 'about:blank',
], { stdio: ['ignore', chromeLog, chromeLog] });

const started = Date.now();
const out = { mode, node: process.version, step: 'start' };
const step = (name) => {
  out.step = name;
  process.stderr.write(`[${((Date.now() - started) / 1000).toFixed(1)}s] ${name}\n`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const chromeTail = () => {
  try {
    return readFileSync(chromeLogPath, 'utf8').slice(-3000);
  } catch {
    return '';
  }
};
// 整份 Chrome 日志（只用来查有没有某一种错误）
chromeTail.full = () => {
  try {
    return readFileSync(chromeLogPath, 'utf8');
  } catch {
    return '';
  }
};
// Chrome 日志里和显卡有关的行（GPU 进程崩溃、看门狗、设备丢失、Dawn 报错），留最后 30 行
const gpuLogLines = () => {
  try {
    return readFileSync(chromeLogPath, 'utf8').split('\n').filter((l) => /gpu|webgpu|dawn|swiftshader|vulkan|watchdog|lost|context/i.test(l)).slice(-30).map((l) => l.slice(0, 300));
  } catch {
    return [];
  }
};
let finished = false;
async function finish(code) {
  if (finished) return;
  finished = true;
  if (code !== 0) out.chromeLog = chromeTail();
  console.log(JSON.stringify(out));
  try { ws.close(); } catch { /* 已经关了 */ }
  chrome.kill('SIGTERM');
  await sleep(1200);
  chrome.kill('SIGKILL');
  try { closeSync(chromeLog); } catch { /* 已经关了 */ }
  process.exit(code);
}
// 总时限：不管卡在哪一步，都在 Python 杀进程之前把已经拿到的结果和卡住的那一步交出去
setTimeout(() => {
  out.error = `超过总时限 ${deadlineMs} ms，卡在：${out.step}`;
  finish(1);
}, deadlineMs);

async function connect() {
  for (let i = 0; i < 150; i += 1) {
    try {
      const v = await (await fetch(`http://127.0.0.1:${debugPort}/json/version`, { signal: AbortSignal.timeout(3000) })).json();
      return v.webSocketDebuggerUrl;
    } catch {
      await sleep(200);
    }
  }
  throw new Error('Chrome 没有启动（调试端口 30 秒内没有回应）');
}

let id = 0;
const pending = new Map();
const listeners = [];
let ws;
// 每个 DevTools 调用都有超时（默认 30 秒）；页面里本来就要等很久的那几步单独给更长的时间
const send = (method, params = {}, sessionId, ms = 30000) => new Promise((resolve, reject) => {
  const mid = (id += 1);
  const timer = setTimeout(() => {
    pending.delete(mid);
    reject(new Error(`${method} 超过 ${ms} ms 没有返回`));
  }, ms);
  pending.set(mid, (msg) => {
    clearTimeout(timer);
    if (msg.error) reject(new Error(`${method}: ${JSON.stringify(msg.error)}`));
    else resolve(msg.result);
  });
  ws.send(JSON.stringify({ id: mid, method, params, sessionId }));
});

let exitCode = 0;
try {
  step('connect');
  if (typeof WebSocket !== 'function') throw new Error(`这个 node（${process.version}）没有内置 WebSocket，需要 22 或更新的版本`);
  ws = new WebSocket(await connect());
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('DevTools 的 WebSocket 10 秒内没有连上')), 10000);
    ws.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
    ws.addEventListener('error', () => { clearTimeout(timer); reject(new Error('DevTools 的 WebSocket 连接出错')); }, { once: true });
  });
  ws.addEventListener('message', (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
    } else if (msg.method) {
      listeners.forEach((fn) => fn(msg));
    }
  });
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  const call = (method, params, ms) => send(method, params, sessionId, ms);
  const evalIn = async (expression, ms) => {
    const r = await call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, ms);
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 800));
    return r.result.value;
  };
  // 页面里的报错和未接住的异常，留最后 20 条，失败时一起交出去
  out.console = [];
  listeners.push((msg) => {
    if (msg.sessionId !== sessionId) return;
    let text = null;
    if (msg.method === 'Runtime.exceptionThrown') text = msg.params.exceptionDetails?.exception?.description || msg.params.exceptionDetails?.text;
    else if (msg.method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(msg.params.type)) {
      text = msg.params.args.map((a) => a.value ?? a.description ?? '').join(' ');
    } else if (msg.method === 'Log.entryAdded' && ['error', 'warning'].includes(msg.params.entry.level)) text = msg.params.entry.text;
    if (text) out.console = [...out.console.slice(-19), String(text).slice(0, 300)];
  });
  // 失败的请求（状态 ≥ 400 或网络错误）带上链接，留最后 20 条；测试自己让它失败的保存（PUT .../crop 的 500）也会在里面
  out.failedRequests = [];
  const requestUrls = new Map();
  const requestLog = []; // 每个发出的请求：数一段时间里有几个 /render
  const renderCount = (from, only = /./) => requestLog.slice(from).filter((r) => /\/render\b/.test(r) && only.test(r)).length;
  listeners.push((msg) => {
    if (msg.sessionId !== sessionId) return;
    const p = msg.params;
    let line = null;
    if (msg.method === 'Network.requestWillBeSent') {
      requestUrls.set(p.requestId, `${p.request.method} ${p.request.url}`);
      requestLog.push(`${p.request.method} ${p.request.url}`);
    }
    else if (msg.method === 'Network.responseReceived' && p.response.status >= 400) line = `${p.response.status} ${requestUrls.get(p.requestId) || p.response.url}`;
    else if (msg.method === 'Network.loadingFailed') line = `${p.errorText}${p.canceled ? ' (canceled)' : ''} ${requestUrls.get(p.requestId) || p.requestId}`;
    if (line) out.failedRequests = [...out.failedRequests.slice(-19), line.replace(/token=[^&\s]+/, 'token=…').slice(0, 300)];
  });
  await call('Network.enable');
  await call('Runtime.enable');
  await call('Log.enable');

  // 保存请求（PUT .../crop）：holding 时挂住，release('continue' | 'fail') 时放行或让它失败
  let holding = false;
  const held = [];
  listeners.push(async (msg) => {
    if (msg.method !== 'Fetch.requestPaused' || msg.sessionId !== sessionId) return;
    const p = msg.params;
    if (holding && p.request.method === 'PUT' && /\/crop$/.test(p.request.url)) held.push(p.requestId);
    else await call('Fetch.continueRequest', { requestId: p.requestId }).catch(() => {});
  });
  const release = async (how) => {
    const ids = held.splice(0);
    for (const requestId of ids) {
      if (how === 'fail') {
        await call('Fetch.fulfillRequest', {
          requestId,
          responseCode: 500,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
          body: Buffer.from(JSON.stringify({ code: 'disk_full', message: '磁盘已满，无法写入编辑记录。' })).toString('base64'),
        });
      } else {
        await call('Fetch.continueRequest', { requestId });
      }
    }
    return ids.length;
  };
  await call('Fetch.enable', { patterns: [{ urlPattern: '*/crop', requestStage: 'Request' }] });
  await call('Page.enable');

  // 第一次带令牌打开（换成 cookie），再选预览方式，然后进这张照片
  const origin = `http://127.0.0.1:${port}`;
  step('open with token');
  await call('Page.navigate', { url: `${origin}/?token=${token}` });
  await sleep(1500);
  step('set preview preference');
  await evalIn(`localStorage.setItem('gxr-studio.gpu-preview', '${mode === 'gpu' ? 'on' : 'off'}'); true`);
  if (mode === 'layout') {
    // 边框和时间戳的版面在真 Chrome 里用画布量字（同一批字体文件）算出来，交给 Python 和 Pillow 的结果对
    step('load fonts and lay out');
    const cases = readFileSync(process.env.LAYOUT_CASES, 'utf8');
    out.layout = await evalIn(`(async () => {
      const L = await import('/assets/finish_layout.js');
      const F = await import('/assets/finish_fonts.js');
      const data = ${cases};
      await Promise.all(['atkinson', 'inter', 'courier', 'patrick', 'dseg'].map((id) => F.loadFont(id)));
      const m = F.canvasMetrics();
      const metric = data.table.map(([kind, font, px, text]) => (kind === 'a' ? m.advance(font, px, text, false) : m.bbox(font, px, text, false)));
      const frames = data.frames.map((c) => L.frameLayout(c.w, c.h, c.spec, c.meta, m));
      const stamps = data.stamps.map((c) => { const text = L.stampText(c.spec, c.when); return { text, layout: L.stampLayout(c.w, c.h, c.spec, text, m) }; });
      return { metric, frames, stamps };
    })()`, 120000);
    out.ready = true;
    await finish(0);
  }
  step('open photo');
  await call('Page.navigate', { url: `${origin}/?open=1#/photo/${photoId}` });

  const helpers = `
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const until = async (fn, ms) => { const t = performance.now(); while (performance.now() - t < ms) { const v = fn(); if (v) return v; await wait(10); } return null; };
    const boxRect = () => { const b = document.querySelector('.crop-box'); return b ? [b.style.left, b.style.top, b.style.width, b.style.height].map((s) => parseFloat(s)) : null; };
    const guideLines = () => document.querySelectorAll('.crop-box svg.guides line').length;
    const serverCrop = async () => (await (await fetch('/api/photos/${photoId}/crop')).json());
    const sameBox = (a, b) => !!a && !!b && a.every((v, i) => Math.abs(v - b[i]) < 0.25);
    const pickRatio = async (text) => {
      document.querySelector('button[aria-label="比例"]').click();
      await wait(30);
      const item = [...document.querySelectorAll('.menu .mi')].find((b) => b.textContent.trim() === text);
      const before = boxRect();
      const t0 = performance.now();
      item.click();
      const changed = await until(() => !sameBox(boxRect(), before), 3000);
      return { visibleMs: changed ? Math.round(performance.now() - t0) : null, box: boxRect(), label: document.querySelector('button[aria-label="比例"] span')?.textContent };
    };
  `;
  const ready = async () => {
    const t0 = Date.now();
    while (Date.now() - t0 < 60000) {
      if (await evalIn(`!!document.querySelector('#tab-crop')`, 5000).catch(() => false)) return true;
      await sleep(100);
    }
    return false;
  };
  step('wait for the viewer');
  out.ready = await ready();
  if (!out.ready) {
    out.page = await evalIn(`location.href + ' | ' + document.body.innerText.slice(0, 300)`, 5000).catch((e) => String(e));
    throw new Error(`大图没有打开：${out.page}`);
  }
  await sleep(1500);
  // 进裁切页：框要出现（显卡预览时画面是画布，框也要有）
  step('enter crop');
  await evalIn(`(async () => { ${helpers} document.querySelector('#tab-crop').click(); await until(() => document.querySelector('.crop-box'), 20000); await wait(600); return true; })()`);
  if (mode === 'gpu') {
    // 软件显卡（SwiftShader）在慢机器上建管线、传源图要好几秒，这期间页面照常用服务器出图；
    // 等显卡真正接手（画布出来、框也在）再往下点，不在切换的半路上判断
    step('wait for the WebGPU canvas');
    out.gpuWaitMs = await evalIn(`(async () => { ${helpers}
      const t0 = performance.now();
      const ok = await until(() => document.querySelector('.gpu') && document.querySelector('.gpu .crop-box'), 120000);
      if (!ok) return null;
      await wait(600);
      return Math.round(performance.now() - t0);
    })()`, 130000);
  }
  out.gpu = await evalIn(`!!document.querySelector('.gpu')`);
  if (mode === 'gpu' && !out.gpu) {
    // 没走显卡：把判断用到的几样东西都交出去（开关、本机选择、适配器、这张照片的 /gpu、引擎能不能建）
    step('collect WebGPU diagnostics');
    out.gpuDiag = await evalIn(`(async () => {
      const d = { navigatorGpu: !!navigator.gpu, pref: localStorage.getItem('gxr-studio.gpu-preview') };
      try { d.flag = (await (await fetch('/api/settings')).json()).gpu_preview; } catch (e) { d.settingsError = String(e); }
      try { const g = await (await fetch('/api/photos/${photoId}/gpu')).json(); d.photo = { enabled: g.enabled, keys: Object.keys(g).slice(0, 24) }; } catch (e) { d.photoError = String(e); }
      try {
        const a = navigator.gpu ? await navigator.gpu.requestAdapter({ powerPreference: 'high-performance' }) : null;
        d.adapter = a ? { vendor: a.info?.vendor, architecture: a.info?.architecture, description: a.info?.description, fallback: a.info?.isFallbackAdapter } : null;
      } catch (e) { d.adapterError = String(e); }
      try { const m = await import('/assets/gpu/engine.js'); d.engine = (await m.Engine.create()) ? 'created' : 'null'; } catch (e) { d.engineError = String(e).slice(0, 300); }
      // 页面自己的那台引擎（和页面同一个模块实例）：没建起来，还是建了又丢了（设备丢失后整个会话都走服务器）
      try {
        const st = await import('/assets/gpu_stage.js');
        const e = st.gpuEngineForTests();
        d.pageEngine = e ? { lost: !!e.lost, currentKey: typeof e.currentKey === 'function' ? e.currentKey() : null } : null;
      } catch (e) { d.pageEngineError = String(e).slice(0, 300); }
      // 照片的 JPG 源图：照页面的做法取回来、解码
      try {
        const g = await (await fetch('/api/photos/${photoId}/gpu')).json();
        if (g.jpg) {
          const res = await fetch(g.jpg.url, { credentials: 'same-origin' });
          d.jpgStatus = res.status;
          const bm = await createImageBitmap(await res.blob(), { imageOrientation: 'from-image', colorSpaceConversion: 'none', premultiplyAlpha: 'none' });
          d.jpgBitmap = [bm.width, bm.height];
          bm.close();
        }
        d.photoRaw = g.raw ? 'layout' : g.raw_reason;
      } catch (e) { d.jpgError = String(e).slice(0, 300); }
      d.stage = { classes: document.querySelector('.stage-area, .stage')?.className || null, img: !!document.querySelector('.pv-img'), canvas: !!document.querySelector('canvas') };
      return d;
    })()`, 60000).catch((e) => ({ diagError: String(e) }));
    out.gpuChromeLog = gpuLogLines();
    out.gpuSharedImageError = /Unable to create shared image|SharedImageBackingFactory/.test(chromeTail.full());
    out.failedBeforeClicks = [...out.failedRequests];
    out.consoleBeforeClicks = [...out.console];
  }
  out.boxAfterEnter = await evalIn(`(() => { ${helpers} return boxRect(); })()`);

  // 1) 保存挂着时点比例：框马上变；放行后换成后端算的准框
  step('ratio while the save is held');
  holding = true;
  out.ratio = await evalIn(`(async () => { ${helpers} return pickRatio('1:1'); })()`);
  await sleep(300);
  out.heldDuringRatio = held.length;
  out.ratioStillLocal = await evalIn(`(() => { ${helpers} return boxRect(); })()`);
  await release('continue');
  holding = false;
  await sleep(800);
  out.afterRelease = await evalIn(`(async () => { ${helpers} const s = await serverCrop(); return { box: boxRect(), server: s.rect.map((v) => v * 100), preset: s.preset }; })()`);

  // 2) 横竖切换、再换一个比例（保存照常）
  step('orientation');
  out.orient = await evalIn(`(async () => { ${helpers}
    await pickRatio('4:5');
    await wait(500);
    const btn = [...document.querySelectorAll('.crop-section button.btn')].find((b) => /横向|纵向/.test(b.textContent));
    const before = boxRect();
    const t0 = performance.now();
    btn.click();
    const changed = await until(() => !sameBox(boxRect(), before), 3000);
    await wait(500);
    const s = await serverCrop();
    return { visibleMs: changed ? Math.round(performance.now() - t0) : null, box: boxRect(), label: btn.textContent.trim(), server: s.rect.map((v) => v * 100) };
  })()`);

  // 3) 参考线：从菜单里换一种，框里的线马上跟着换
  step('guides');
  out.guides = await evalIn(`(async () => { ${helpers}
    const before = guideLines();
    document.querySelector('button[aria-label="参考线"]').click();
    await wait(30);
    const item = [...document.querySelectorAll('.menu .mi')].find((b) => b.textContent.trim() === '网格');
    item.click();
    const changed = await until(() => guideLines() !== before, 3000);
    return { before, after: guideLines(), changed: !!changed };
  })()`);

  // 4) 保存失败：框留着、提示可以重试；重试以后和后端一致
  step('failed save and retry');
  holding = true;
  out.failed = await evalIn(`(async () => { ${helpers} return pickRatio('16:9'); })()`);
  await sleep(300);
  await release('fail');
  holding = false;
  await sleep(800);
  out.afterFail = await evalIn(`(async () => { ${helpers}
    const retry = [...document.querySelectorAll('.toast button')].find((b) => b.textContent.trim() === '重试');
    const box = boxRect();
    if (retry) retry.click();
    await wait(1200);
    const s = await serverCrop();
    return { hasRetry: !!retry, boxBefore: box, boxAfter: boxRect(), server: s.rect.map((v) => v * 100), preset: s.preset };
  })()`);

  // 5) 撤销（顶栏按钮）：框回到上一步
  step('undo');
  out.undo = await evalIn(`(async () => { ${helpers}
    const before = boxRect();
    const btn = document.querySelector('#tb-edit button[title^="撤销"]') || document.querySelector('button[title^="撤销"]');
    btn.click();
    const changed = await until(() => !sameBox(boxRect(), before), 3000);
    await wait(800);
    const s = await serverCrop();
    return { changed: !!changed, box: boxRect(), server: s.rect.map((v) => v * 100), preset: s.preset };
  })()`);

  // 6) 边框卡片：浏览器自己画，不请服务器给每种样式出图
  step('frame cards');
  let mark = requestLog.length;
  out.cards = await evalIn(`(async () => { ${helpers}
    const t0 = performance.now();
    document.querySelector('#tab-frame').click();
    const n = await until(() => document.querySelectorAll('.fcard').length, 5000);
    await until(() => document.querySelectorAll('.fcard .thumb canvas').length >= n, 60000);
    const ms = Math.round(performance.now() - t0);
    // 第一张卡（无）是照片本身：不是空白（显卡画面当场复制，复制失败会得到一张黑的或空的）
    const first = document.querySelector('.fcard .thumb canvas');
    const data = first.getContext('2d').getImageData(0, 0, first.width, first.height).data;
    const seen = new Set();
    for (let i = 0; i < data.length; i += 4 * 97) seen.add((data[i] >> 3) + ',' + (data[i + 1] >> 3) + ',' + (data[i + 2] >> 3));
    return { cards: n, canvases: document.querySelectorAll('.fcard .thumb canvas').length, ms, distinct: seen.size };
  })()`, 75000);
  // 离开裁切页时大图会重新出一次（服务器出图时）；卡片自己的底图请求带 max_edge=480，最多一张
  out.cards.cardRenders = renderCount(mark, /max_edge=480/);
  out.cards.stageRenders = renderCount(mark, /^(?!.*max_edge=480)/);

  // 7) 加了边框和时间戳以后：显卡还在管，拖滑块不请求 /render，点样式、改选项当帧就画出来
  step('frame and stamp with the preview on');
  mark = requestLog.length;
  out.live = await evalIn(`(async () => { ${helpers}
    const raf = () => new Promise((r) => requestAnimationFrame(() => r()));
    const deco = () => { const pv = document.querySelector('.pv'); return { framed: pv.classList.contains('framed'), overlay: !!pv.querySelector('.pv-deco'), size: [pv.style.width, pv.style.height] }; };
    const timed = async (fn) => { const t = performance.now(); fn(); await raf(); return Math.round(performance.now() - t); };
    const card = (name) => [...document.querySelectorAll('.fcard')].find((x) => x.textContent.trim() === name);
    const renders = () => performance.getEntriesByType('resource').filter((e) => /\\/render\\b/.test(e.name)).length;
    const renders0 = renders();
    const styleMs = {};
    for (const name of ['徕卡水印', '哈苏水印', '胶片', '拍立得', '画布', '白边']) {
      styleMs[name] = await timed(() => card(name).click());
      styleMs[name + ':framed'] = deco().framed;
    }
    styleMs.leica = await timed(() => card('徕卡水印').click());
    await wait(200);
    const frame = deco();
    // 换一个选项（颜色）也一样
    const colorMs = await timed(() => document.querySelector('.swrow button[title="黑"], .swrow button[aria-label="黑"]')?.click());
    // 时间戳
    document.querySelector('#tab-stamp').click();
    await wait(200);
    const stampMs = await timed(() => document.querySelector('#tp-stamp input[role=switch]').click());
    const stampOn = deco();
    await wait(500);
    const clickRenders = renders() - renders0; // 点样式、改颜色、开时间戳：没有一个 /render
    // 时间戳的文字：边输入边变（当帧），保存等 400 ms；输入时光标不丢（面板没有重搭）
    const edits = [];
    const realFetch = window.fetch;
    window.fetch = (...a) => { if ((a[1] && a[1].method) === 'PUT' && /\\/edit$/.test(String(a[0]))) edits.push(performance.now()); return realFetch(...a); };
    [...document.querySelectorAll('#tp-stamp .seg button')].find((b) => b.textContent === '日期和文字').click();
    await wait(300);
    const input = document.querySelector('#tp-stamp input[type=text]');
    input.focus();
    const key = () => document.querySelector('.pv .pv-deco')?.dataset.key;
    const puts0 = edits.length;
    const k0 = key();
    const typeMs = await timed(() => { input.value = 'Pa'; input.dispatchEvent(new Event('input', { bubbles: true })); });
    const keyChanged = key() !== k0;
    input.value = 'Paris';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await wait(150);
    const putsEarly = edits.length - puts0; // 还在 400 ms 之内：没有保存
    await wait(900);
    const putsLate = edits.length - puts0;
    const typed = { typeMs, keyChanged, putsEarly, putsLate, focusKept: document.activeElement === input, saved: (await (await fetch('/api/photos/${photoId}/edit')).json()).stamp.text };
    // 拖一个调整滑块：几十次 input，中间没有 /render
    document.querySelector('#tab-adjust').click();
    await wait(300);
    const slider = document.querySelector('#tp-adjust input[type=range]');
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    const before = performance.now();
    slider.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
    const frames = [];
    for (let i = 1; i <= 30; i += 1) {
      setter.call(slider, String((i % 15) * 2 - 10));
      const t = performance.now();
      slider.dispatchEvent(new Event('input', { bubbles: true }));
      await raf();
      frames.push(Math.round(performance.now() - t));
    }
    slider.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
    const dragMs = Math.round(performance.now() - before);
    await wait(800);
    const dragRenders = renders() - renders0; // 点样式、改选项、打字、拖滑块的整个过程：服务器出图一次也没有
    const gpuNow = !!document.querySelector('.gpu');
    // 100%：照片一个像素对一个屏幕像素，边框和日期戳跟着（叠加层和整张成片一样大，比照片大一圈）；换回适合
    const rect = (e) => { const b = e.getBoundingClientRect(); return [Math.round(b.width), Math.round(b.height)]; };
    const press = (label) => [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === label).click();
    const look = () => {
      const pv = document.querySelector('.pv');
      const media = pv && pv.querySelector('.pv-gpu, .pv-img');
      const deco = pv && pv.querySelector('.pv-deco');
      return pv && media ? { framed: pv.classList.contains('framed'), overlay: !!deco, pv: rect(pv), media: rect(media), deco: deco ? rect(deco) : null } : null;
    };
    const settle = async () => { await until(look, 10000); await wait(300); return look(); }; // 画面出来了再量（机器忙时慢一点）
    press('100%');
    const zoom100 = await settle();
    press('适合');
    const fit = await settle();
    // 换照片：下一张没有边框（叠加层不跟过去），换回来边框和日期戳还在
    document.querySelector('button[aria-label^="下一张"]').click();
    await wait(300);
    const next = await settle();
    document.querySelector('button[aria-label^="上一张"]').click();
    await wait(300);
    const back = await settle();
    return { styleMs, colorMs, frame, stampMs, stampOn, clickRenders, dragRenders, typed, gpu: gpuNow, dragMs, worstFrameMs: Math.max(...frames), zoom100, fit, next, back };
  })()`, 90000);
  out.live.requests = requestLog.slice(mark).filter((r) => !/^PUT /.test(r) && !/\/(edit|crop)$/.test(r)).slice(-8);
  step('done');
} catch (e) {
  out.error = String(e && e.message ? e.message : e).slice(0, 800);
  exitCode = 1;
}
await finish(exitCode);
