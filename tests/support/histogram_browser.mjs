// 真浏览器里的直方图（tests/test_histogram_browser.py 起服务器和 Chrome，这里用 DevTools 协议驱动）。
// 用法：node histogram_browser.mjs <chrome 路径> <端口> <令牌> <照片 id> <gpu|server> <用户目录>
// 额外的 Chrome 开关放在环境变量 CHROME_FLAGS（JSON 数组）。输出一行 JSON，断言在 Python 那边。
// 做的事：打开大图 → 直方图在所有标签下都在面板顶部 → 曝光加到 +1.5 档后直方图右移 → 模拟拖滑块量更新速度 →
// 按 J / 点三角标出溢出（显卡路径读回显示结果、服务器路径读叠加层）→ 收起记住 → 裁剪页只数框里面 → 快捷键表里有 J。
// 每一步都有超时，总时限在环境变量 VIEWER_DEADLINE_MS；卡住时也输出 JSON，step 是卡住的那一步。进度逐行写到 stderr。
import { spawn } from 'node:child_process';
import { closeSync, openSync, readFileSync } from 'node:fs';

const [chromePath, port, token, photoId, mode, profile] = process.argv.slice(2);
const extra = JSON.parse(process.env.CHROME_FLAGS || '[]');
const deadlineMs = Number(process.env.VIEWER_DEADLINE_MS || 200000);
const dragMs = Number(process.env.HIST_DRAG_MS || 3000);
const debugPort = 19900 + Math.floor(Math.random() * 400);
const chromeLogPath = `${profile}.log`;
const chromeLog = openSync(chromeLogPath, 'w');
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
  try { return readFileSync(chromeLogPath, 'utf8').slice(-3000); } catch { return ''; }
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
  out.console = [];
  listeners.push((msg) => {
    if (msg.sessionId !== sessionId) return;
    let text = null;
    if (msg.method === 'Runtime.exceptionThrown') text = msg.params.exceptionDetails?.exception?.description || msg.params.exceptionDetails?.text;
    else if (msg.method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(msg.params.type)) {
      text = msg.params.args.map((a) => a.value ?? a.description ?? '').join(' ');
    }
    if (text) out.console = [...out.console.slice(-19), String(text).slice(0, 300)];
  });
  await call('Runtime.enable');
  await call('Page.enable');
  await call('Emulation.setFocusEmulationEnabled', { enabled: true });

  // 真的按键（走浏览器的输入管线，和用户按下去一样）
  const press = async (key, code, vk) => {
    await call('Input.dispatchKeyEvent', { type: 'keyDown', key, code, text: key, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
    await call('Input.dispatchKeyEvent', { type: 'keyUp', key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
  };

  const origin = `http://127.0.0.1:${port}`;
  step('open with token');
  await call('Page.navigate', { url: `${origin}/?token=${token}` });
  await sleep(1500);
  await evalIn(`localStorage.removeItem('gxr-studio.histogram-collapsed'); localStorage.setItem('gxr-studio.gpu-preview', '${mode === 'gpu' ? 'on' : 'off'}'); true`);
  step('open photo');
  await call('Page.navigate', { url: `${origin}/?open=1#/photo/${photoId}` });

  const helpers = `
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const until = async (fn, ms) => { const t = performance.now(); while (performance.now() - t < ms) { const v = await fn(); if (v) return v; await wait(15); } return null; };
    const hs = async () => (await import('/assets/histogram.js')).histogramState();
    const slider = (label) => document.querySelector('input[type=range][aria-label="' + label + '"]');
    const frame = () => new Promise((r) => requestAnimationFrame(r));
    const grab = (input) => { input.focus(); input.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })); };
    const drag = (input, v) => { input.value = String(v); input.dispatchEvent(new Event('input', { bubbles: true })); };
    const drop = (input) => { input.dispatchEvent(new PointerEvent('pointerup', { bubbles: true })); input.dispatchEvent(new Event('change', { bubbles: true })); };
    const visible = (el) => !!el && el.getBoundingClientRect().height > 0 && el.getBoundingClientRect().width > 0;
    const waitUpdate = async (before, ms) => until(async () => { const s = await hs(); return s && s.updates > before ? s : null; }, ms);
    // 显卡路径：读回带溢出提示的显示结果，数红 / 蓝的点
    const gpuPaint = async () => {
      const e = (await import('/assets/gpu_stage.js')).gpuEngineForTests();
      const size = e.finalSize();
      const pr = await e.readPresent(size.w, size.h);
      let red = 0, blue = 0;
      for (let i = 0; i < pr.data.length; i += 4) {
        const r = pr.data[i], g = pr.data[i + 1], b = pr.data[i + 2];
        if (r === 255 && g === 50 && b === 40) red += 1;
        else if (r === 40 && g === 110 && b === 255) blue += 1;
      }
      return { red, blue, total: size.w * size.h };
    };
    // 服务器路径：叠加层 canvas 里数红 / 蓝的点
    const overlayPaint = () => {
      const cv = document.querySelector('.clip-overlay');
      if (!cv || !cv.width) return { red: 0, blue: 0, total: 0, present: !!cv };
      const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
      let red = 0, blue = 0;
      for (let i = 0; i < d.length; i += 4) {
        if (d[i + 3] !== 255) continue;
        if (d[i] === 255 && d[i + 1] === 50 && d[i + 2] === 40) red += 1;
        else if (d[i] === 40 && d[i + 1] === 110 && d[i + 2] === 255) blue += 1;
      }
      return { red, blue, total: cv.width * cv.height, present: true };
    };
    const paint = () => (${mode === 'gpu'} ? gpuPaint() : Promise.resolve(overlayPaint()));
  `;

  step('wait for the viewer');
  let ready = false;
  for (let t0 = Date.now(); Date.now() - t0 < 60000 && !ready; await sleep(100)) {
    ready = await evalIn(`!!document.querySelector('#tab-adjust') && !!document.querySelector('.hist')`, 5000).catch(() => false);
  }
  out.ready = ready;
  if (!ready) throw new Error(`大图没有打开：${await evalIn(`location.href + ' | ' + document.body.innerText.slice(0, 300)`, 5000).catch((e) => String(e))}`);

  // 1) 位置：在面板顶部、工具标签之上；每个标签下都看得见
  step('placement');
  out.placement = await evalIn(`(async () => { ${helpers}
    const hist = document.querySelector('.hist');
    const panel = document.querySelector('aside.panel');
    const tabs = document.querySelector('.tooltabs');
    const kids = [...panel.children];
    const per = {};
    for (const id of ['recipe', 'adjust', 'crop', 'frame', 'stamp', 'info']) {
      document.querySelector('#tab-' + id).click();
      await wait(120);
      per[id] = visible(hist) && visible(document.querySelector('.hist-plot'));
    }
    document.querySelector('#tab-adjust').click();
    await wait(150);
    return { first: kids[0] === hist, aboveTabs: kids.indexOf(hist) < kids.indexOf(tabs), perTab: per };
  })()`, 30000);

  if (mode === 'gpu') {
    step('wait for the WebGPU canvas');
    out.gpuWaitMs = await evalIn(`(async () => { ${helpers}
      const t0 = performance.now();
      const ok = await until(() => document.querySelector('.gpu'), 120000);
      if (!ok) return null;
      await wait(300);
      return Math.round(performance.now() - t0);
    })()`, 130000);
  }
  out.gpu = await evalIn(`!!document.querySelector('.gpu')`);
  if (mode === 'gpu') {
    out.adapter = await evalIn(`navigator.gpu.requestAdapter({ powerPreference: 'high-performance' }).then((a) => a && { vendor: a.info.vendor, architecture: a.info.architecture, description: a.info.description, fallback: !!a.info.isFallbackAdapter })`, 20000).catch(() => null);
  }
  if (mode === 'gpu' && !out.gpu) {
    // 这台机器没有走显卡预览：交给 Python 判断是跳过还是失败
    await finish(0);
  }

  // 2) 第一次数出来
  step('first histogram');
  out.first = await evalIn(`(async () => { ${helpers}
    const s = await until(async () => { const s = await hs(); return s && s.updates >= 1 && s.source === '${mode === 'gpu' ? 'gpu' : 'server'}' ? s : null; }, 60000);
    return s;
  })()`, 70000);
  if (!out.first) throw new Error(`直方图 60 秒内没有数出来：${JSON.stringify(await evalIn(`import('/assets/histogram.js').then((m) => m.histogramState())`, 5000))}`);

  // 3) 曝光 +1.5：直方图右移（平均亮度档变大）。曝光滑块拖到底，松手
  step('exposure shifts the histogram right');
  out.exposure = await evalIn(`(async () => { ${helpers}
    const input = slider('曝光');
    const s0 = await hs();
    const t0 = performance.now();
    grab(input);
    drag(input, 1.5);
    const s1 = await waitUpdate(s0.updates, 20000);
    const firstUpdateMs = s1 ? Math.round(performance.now() - t0) : null; // 第一次输入到第一次更新（别把下面的等待算进去）
    // 等画面稳定（服务器路径要等出图），取最后一次
    await wait(1200);
    const s2 = await hs();
    drop(input);
    return { before: s0.mean, after: s2.mean, firstUpdateMs, source: s2.source, updates: s2.updates - s0.updates };
  })()`, 60000);

  // 4) 拖滑块量更新速度：每帧改一次曝光，来回扫 dragMs 毫秒，数直方图更新了几次
  step('drag rate');
  out.rate = await evalIn(`(async () => { ${helpers}
    const {backgroundStats}=await import('/assets/finish_background.js');
    const cards=[...document.querySelectorAll('#tp-frame .fcards canvas')];
    const blurBefore=backgroundStats().builds;
    let cardRedraws=0;
    const observer=new MutationObserver(records=>{cardRedraws+=records.length;});
    for(const canvas of cards) observer.observe(canvas,{attributes:true,attributeFilter:['width']});
    const input = slider('曝光');
    const s0 = await hs();
    grab(input);
    const dts = [];
    const stamps = [];
    let last = performance.now(), prev = s0.updates;
    const t0 = last;
    let frames = 0;
    while (performance.now() - t0 < ${dragMs}) {
      const t = performance.now() - t0;
      drag(input, (Math.round((1.2 + 1.2 * Math.sin(t / 350)) * 10) / 10));
      await frame();
      if (${mode !== 'gpu'}) await wait(160); // 服务器出图：页面把输入防抖 120 毫秒，真实的拖动也有停顿
      const now = performance.now();
      dts.push(now - last); last = now; frames += 1;
      const s = await hs();
      if (s.updates !== prev) { stamps.push(now); prev = s.updates; }
    }
    const s1 = await hs();
    drop(input);
    const lastMs = s1.lastMs;
    const secs = (performance.now() - t0) / 1000;
    dts.sort((a, b) => a - b);
    const gaps = stamps.slice(1).map((v, i) => v - stamps[i]).sort((a, b) => a - b);
    const q = (arr, p) => arr.length ? Math.round(arr[Math.min(arr.length - 1, Math.floor(arr.length * p))]) : null;
    observer.disconnect();
    const frameWork={cards:cards.length,hidden:document.querySelector('#tp-frame').hidden,redraws:cardRedraws,blurBuilds:backgroundStats().builds-blurBefore};
    return { frameWork, lastMs, seconds: secs, updates: s1.updates - s0.updates, perSecond: (s1.updates - s0.updates) / secs, frames, frameMs: { p50: q(dts, 0.5), p95: q(dts, 0.95), max: q(dts, 1) }, gapMs: { p50: q(gaps, 0.5), p95: q(gaps, 0.95) } };
  })()`, dragMs + 60000);
  await sleep(600);

  // 5) 溢出：曝光 +5 档，高光大面积死白；曝光 −3 档再把黑点推到 +100，暗部大面积死黑（假卡的照片底色不够暗，光减曝光压不死）。三角各自亮起
  step('clipping triangles light up');
  out.lit = await evalIn(`(async () => { ${helpers}
    const tri = (side) => document.querySelector('.hist-clip[data-side="' + side + '"]');
    const set = async (exposure, black) => {
      const s0 = await hs();
      const ex = slider('曝光');
      const bp = slider('黑点');
      grab(ex); drag(ex, exposure); drop(ex);
      grab(bp); drag(bp, black); drop(bp);
      await waitUpdate(s0.updates, 20000);
      await wait(1800);
      return hs();
    };
    const mid = await set(0, 0);
    const up = await set(5, 0);
    const upTri = { shadow: tri('shadow').dataset.lit, highlight: tri('highlight').dataset.lit, title: tri('highlight').title };
    const down = await set(-3, 100);
    const downTri = { shadow: tri('shadow').dataset.lit, highlight: tri('highlight').dataset.lit, title: tri('shadow').title };
    await set(5, 0);
    return { mid: { shadowWarn: mid.shadowWarn, highlightWarn: mid.highlightWarn }, up: { ...upTri, blown: up.highlightShare, crushed: up.shadowShare }, down: { ...downTri, blown: down.highlightShare, crushed: down.shadowShare } };
  })()`, 180000);

  // 6) J：先让曝光 +5（高光大面积死白），焦点留在滑块上（拖完滑块马上按 J），按 J 标出，再按 J 关掉
  step('J toggles the overlay');
  out.keyJ = await evalIn(`(async () => { ${helpers}
    const input = slider('曝光');
    const s0 = await hs();
    grab(input); drag(input, 4.9); drop(input);
    await waitUpdate(s0.updates, 20000);
    await wait(1500);
    input.focus();
    return { focusOnSlider: document.activeElement === input, overlayBefore: (await hs()).clip };
  })()`, 60000);
  await press('j', 'KeyJ', 74);
  await sleep(1200);
  out.keyJ.on = await evalIn(`(async () => { ${helpers}
    const s = await hs();
    return { clip: s.clip, pressed: [...document.querySelectorAll('.hist-clip')].map((b) => b.getAttribute('aria-pressed')), legend: visible(document.querySelector('.clip-legend')) ? document.querySelector('.clip-legend').textContent : null, paint: await paint(), blown: s.blown, samples: s.samples };
  })()`, 30000);
  await press('j', 'KeyJ', 74);
  await sleep(1200);
  out.keyJ.off = await evalIn(`(async () => { ${helpers}
    const s = await hs();
    return { clip: s.clip, pressed: [...document.querySelectorAll('.hist-clip')].map((b) => b.getAttribute('aria-pressed')), legend: visible(document.querySelector('.clip-legend')), paint: await paint() };
  })()`, 30000);

  // 7) 点三角：只标那一边；再点一下关掉
  step('triangles toggle their own side');
  out.click = await evalIn(`(async () => { ${helpers}
    const tri = (side) => document.querySelector('.hist-clip[data-side="' + side + '"]');
    tri('highlight').click();
    await wait(1000);
    const a = { clip: (await hs()).clip, paint: await paint() };
    tri('highlight').click();
    await wait(800);
    const b = { clip: (await hs()).clip, paint: await paint() };
    tri('shadow').click();
    await wait(1000);
    const c = { clip: (await hs()).clip, paint: await paint() };
    tri('shadow').click();
    await wait(800);
    return { highlightOn: a, highlightOff: b, shadowOn: c, shadowOff: (await hs()).clip };
  })()`, 60000);

  // 8) 收起：只剩一行，三角还在；刷新后还是收起的；再展开
  step('collapse is remembered');
  out.collapse = await evalIn(`(async () => { ${helpers}
    document.querySelector('.hist-toggle').click();
    await wait(400);
    const hist = document.querySelector('.hist');
    return { collapsed: hist.dataset.collapsed, expanded: document.querySelector('.hist-toggle').getAttribute('aria-expanded'), stored: localStorage.getItem('gxr-studio.histogram-collapsed'),
      plotVisible: document.querySelector('.hist-body').getBoundingClientRect().height > 10, trianglesVisible: [...document.querySelectorAll('.hist-clip')].every(visible), headHeight: Math.round(document.querySelector('.hist-head').getBoundingClientRect().height) };
  })()`, 20000);
  await call('Page.navigate', { url: `${origin}/?open=1#/photo/${photoId}` });
  await sleep(1500);
  for (let t0 = Date.now(); Date.now() - t0 < 60000; await sleep(100)) {
    if (await evalIn(`!!document.querySelector('.hist')`, 5000).catch(() => false)) break;
  }
  out.collapse.afterReload = await evalIn(`({ collapsed: document.querySelector('.hist').dataset.collapsed, stored: localStorage.getItem('gxr-studio.histogram-collapsed') })`);
  out.collapse.afterExpand = await evalIn(`(async () => { ${helpers}
    document.querySelector('.hist-toggle').click();
    await wait(400);
    return { collapsed: document.querySelector('.hist').dataset.collapsed, stored: localStorage.getItem('gxr-studio.histogram-collapsed'), plotVisible: document.querySelector('.hist-body').getBoundingClientRect().height > 10 };
  })()`, 20000);

  // 9) 裁剪页：画面是整张未裁切的照片，直方图只数裁剪框里面的（选 1:1 让框明显比整张小）
  step('crop tab counts the crop box');
  out.crop = await evalIn(`(async () => { ${helpers}
    await until(async () => { const s = await hs(); return s && s.updates >= 1 ? s : null; }, 60000);
    const full = await hs();
    document.querySelector('#tab-crop').click();
    await until(() => document.querySelector('.crop-box'), 30000);
    await wait(800);
    const before = await hs();
    document.querySelector('button[aria-label="比例"]').click();
    const item = await until(() => [...document.querySelectorAll('.menu .mi')].find((b) => b.textContent.trim() === '1:1'), 5000);
    item.click();
    const t0 = performance.now();
    const seen = [];
    const s = await until(async () => {
      const s = await hs();
      const k = s ? s.updates + ' ' + JSON.stringify(s.region) : 'null';
      if (seen.length < 40 && (!seen.length || seen[seen.length - 1].k !== k)) seen.push({ k, t: Math.round(performance.now() - t0) });
      const a = s && s.region ? s.region.w * s.region.h : 0;
      return s && s.updates > before.updates && a > 0.2 && a < 0.9 ? s : null;
    }, 30000);
    const r = s && s.region;
    const img = document.querySelector('.pv-img');
    // 没等到时留下线索：点比例前后直方图的更新次数和区域、画面上那张图
    const diag = s ? null : { before: { updates: before.updates, region: before.region }, seen, last: await hs(),
      img: img && { src: img.src.slice(-80), complete: img.complete, w: img.naturalWidth } };
    const mark = (await hs()).updates;
    document.querySelector('#tab-recipe').click();
    const back = await until(async () => { const b = await hs(); return b && b.region === null && b.updates > mark ? b : null; }, 30000) || await hs();
    return { fullRegion: full.region, region: r, area: r ? r.w * r.h : null, backRegion: back.region, diag };
  })()`, 120000);

  // 10) 快捷键表里有 J
  step('shortcut dialog lists J');
  await evalIn(`document.activeElement && document.activeElement.blur && document.activeElement.blur(); true`);
  await press('?', 'Slash', 191);
  await sleep(600);
  out.help = await evalIn(`(() => {
    const rows = [...document.querySelectorAll('.keys tr')].map((tr) => [...tr.children].map((td) => td.textContent.trim()));
    return rows.filter((r) => r[0] === 'J');
  })()`);
  // 11) 边框不算进直方图（显卡和服务器出图两条路径都测）。边框由浏览器画在 .pv 的底色上（stage_finish.js），照片本身没有边框：
  //     先把照片压暗（曝光 −3、黑点 +100，和第 5 步的暗部一样），再套白色粗边框（255，会算成死白）：屏幕上整张成片有大片纯白，
  //     直方图里不该有，也不该点亮高光三角。（"黑"边框是 12 不是 0，不会算成死黑，所以用白的才分得出来）
  //     时间戳同理：再加一个大的白色文字戳（不发光），叠在暗的照片上。它在叠加层上，不在服务器图或成片纹理里；
  //     先量叠加层上的白色占照片多少（要超过 1%，数进去的话会点亮高光三角），再看直方图里一点也没有。
  //     服务器路径：不再另要一张无边框的小图（max_edge=800 的 /render 一个也没有）；溢出提示只盖照片那一块，不盖边框。
  step('the frame is not counted');
  await evalIn(`(async () => {
    const r = await fetch('/api/photos/${photoId}/edit', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ adjust: { exposure_ev: -3, black_point: 100 }, frame: { style: 'gallery', color: 'white', weight: 'thick' }, stamp: { enabled: true, content: 'text', text: 'WWWWWWWWWWWWWWWW', font: 'sans', color: 'white', size: 'large', corner: 'br', glow: false } }) });
    if (!r.ok) throw new Error('PUT frame ' + r.status + ' ' + await r.text());
    return true;
  })()`);
  await call('Page.reload', {});
  await sleep(1500);
  out.frame = await evalIn(`(async () => { ${helpers}
    const s = await until(async () => { const s = await hs(); return s && s.updates >= 1 && s.source === '${mode}' ? s : null; }, 60000);
    const photo = await until(() => { const i = document.querySelector('.pv.framed .pv-img, .pv.framed .pv-gpu'); return i && (i.tagName === 'CANVAS' || (i.src.startsWith('blob:') && i.complete && i.naturalWidth)) ? i : null; }, 30000);
    await wait(800);
    const pv = document.querySelector('.pv');
    const box = (e) => { const b = e.getBoundingClientRect(); return [b.left, b.top, b.width, b.height].map((v) => Math.round(v * 10) / 10); };
    const stage = box(pv);
    const shown = box(photo);
    // 屏幕上这张成片里白色的占比：边框是 .pv 的白色底色，照片自己是暗的
    const white = getComputedStyle(pv).backgroundColor === 'rgb(255, 255, 255)' ? 1 - (shown[2] * shown[3]) / (stage[2] * stage[3]) : 0;
    // 服务器图本身（<img>）里纯白的点占多少：照片是暗的，不该有（边框不在这张图里）
    let photoBright = null;
    if (photo.tagName === 'IMG') {
      const cv = document.createElement('canvas');
      const k = 200 / Math.max(photo.naturalWidth, photo.naturalHeight);
      cv.width = Math.round(photo.naturalWidth * k); cv.height = Math.round(photo.naturalHeight * k);
      const ctx = cv.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(photo, 0, 0, cv.width, cv.height);
      const d = ctx.getImageData(0, 0, cv.width, cv.height).data;
      let bright = 0;
      for (let i = 0; i < d.length; i += 4) if (Math.min(d[i], d[i + 1], d[i + 2]) >= 253) bright += 1;
      photoBright = bright / (cv.width * cv.height);
    }
    // 叠加层（边框上的字、日期戳）里的纯白占照片面积多少：边框底色是 CSS 背景，不在这块画布里
    let stampWhite = null;
    const deco = pv.querySelector('.pv-deco');
    if (deco) {
      const d = deco.getContext('2d', { willReadFrequently: true }).getImageData(0, 0, deco.width, deco.height).data;
      let n = 0;
      for (let i = 0; i < d.length; i += 4) if (d[i + 3] > 200 && Math.min(d[i], d[i + 1], d[i + 2]) >= 250) n += 1;
      const k = deco.width / stage[2];
      stampWhite = n / (shown[2] * shown[3] * k * k);
    }
    const now = await hs();
    const cleanRenders = performance.getEntriesByType('resource').filter((e) => e.name.includes('/render') && e.name.includes('max_edge=800')).length;
    return { source: now.source, crushedShare: now.shadowShare, blownShare: now.highlightShare, highlightWarn: now.highlightWarn, mean: now.mean, stage, shown, white, stampWhite, photoBright, cleanRenders, gpu: !!document.querySelector('.gpu') };
  })()`, 120000);
  if (out.frame) {
    await evalIn(`document.activeElement && document.activeElement.blur && document.activeElement.blur(); true`);
    await press('j', 'KeyJ', 74);
    await sleep(1000);
    out.frame.legend = await evalIn(`(() => { const l = document.querySelector('.clip-legend'); return l && !l.hidden ? l.textContent : null; })()`);
    // 服务器路径：溢出提示是照片上的一块 canvas，大小和位置要和照片一样（不是整张成片）；显卡路径由着色器直接盖，没有这块 canvas
    out.frame.overlay = await evalIn(`(() => {
      const o = document.querySelector('.clip-overlay');
      const i = document.querySelector('.pv-img');
      if (!o || !i) return null;
      const box = (e) => { const b = e.getBoundingClientRect(); return [b.left, b.top, b.width, b.height].map((v) => Math.round(v * 10) / 10); };
      return { overlay: box(o), photo: box(i), stage: box(document.querySelector('.pv')) };
    })()`);
    await press('j', 'KeyJ', 74);
  }
  await evalIn(`(async () => { await fetch('/api/photos/${photoId}/edit', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ frame: { style: 'none' }, stamp: { enabled: false } }) }); return true; })()`);
  step('done');
} catch (e) {
  out.error = String(e && e.message ? e.message : e).slice(0, 800);
  exitCode = 1;
}
await finish(exitCode);
