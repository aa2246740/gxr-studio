// 真浏览器里的配方卡片（tests/test_look_cards_browser.py 起服务器和 Chrome，这里用 DevTools 协议驱动）。
// 用法：node look_cards_browser.mjs <chrome 路径> <端口> <令牌> <照片 id> <下一张照片 id> <gpu|server> <用户目录>
// 额外的 Chrome 开关放在环境变量 CHROME_FLAGS（JSON 数组）。输出一行 JSON，断言在 Python 那边。
// 步骤：卡片出来（有真像素、各不相同）→ 点一张卡片，卡片和舞台比对 → 拖滑块，期间没有请求、卡片不画 → 换照片，旧照片的卡片停下。
import { spawn } from 'node:child_process';
import { closeSync, openSync } from 'node:fs';
import { chromeLogReader, diagnosticsExpression } from './gpu_diag.mjs';

const [chromePath, port, token, photoId, nextId, mode, profile] = process.argv.slice(2);
const extra = JSON.parse(process.env.CHROME_FLAGS || '[]');
const deadlineMs = Number(process.env.LOOKS_DEADLINE_MS || 280000);
const debugPort = 19900 + Math.floor(Math.random() * 400);
const chromeLogPath = `${profile}.log`;
const chromeLog = openSync(chromeLogPath, 'w');
const chrome = spawn(chromePath, [
  '--headless=new', `--user-data-dir=${profile}`, `--remote-debugging-port=${debugPort}`, '--no-first-run', '--no-default-browser-check',
  '--disable-extensions', '--disable-background-networking', '--use-mock-keychain', '--password-store=basic', '--window-size=1440,900',
  '--enable-logging=stderr', '--v=0', ...extra, 'about:blank',
], { stdio: ['ignore', chromeLog, chromeLog] });

const started = Date.now();
const out = { mode, step: 'start' };
const step = (name) => {
  out.step = name;
  process.stderr.write(`[${((Date.now() - started) / 1000).toFixed(1)}s] ${name}\n`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const gpuLog = chromeLogReader(chromeLogPath);
let finished = false;
let ws;
async function finish(code) {
  if (finished) return;
  finished = true;
  if (code !== 0) out.chromeLog = gpuLog.tail();
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
    else if (msg.method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(msg.params.type)) text = msg.params.args.map((a) => a.value ?? a.description ?? '').join(' ');
    if (text) out.console = [...out.console.slice(-19), String(text).slice(0, 300)];
  });
  // 发出的每个请求（方法 + 地址）：数一段时间里有几个、是什么
  const requestLog = [];
  listeners.push((msg) => {
    if (msg.sessionId === sessionId && msg.method === 'Network.requestWillBeSent') requestLog.push(`${msg.params.request.method} ${msg.params.request.url.replace(/token=[^&\s]+/, 'token=…')}`);
  });
  await call('Network.enable');
  await call('Runtime.enable');
  await call('Page.enable');
  await call('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 2, mobile: false });

  const origin = `http://127.0.0.1:${port}`;
  step('open with token');
  await call('Page.navigate', { url: `${origin}/?token=${token}` });
  await sleep(1500);
  await evalIn(`localStorage.setItem('gxr-studio.gpu-preview', '${mode === 'gpu' ? 'on' : 'off'}'); true`);

  const helpers = `
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const until = async (fn, ms) => { const t = performance.now(); while (performance.now() - t < ms) { const v = fn(); if (v) return v; await wait(20); } return null; };
    const root = () => document.querySelector('.edit-panel');
    const stats = () => { const r = root(); return r ? { ...r.dataset } : null; };
    const card = (id) => document.querySelector('.lcard[data-look="' + id + '"]');
    // 一张卡片的像素统计：不是空白（亮度有变化）
    const pixels = (c) => { const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0, s = 0, s2 = 0, r = 0, g = 0, b = 0; for (let i = 0; i < d.length; i += 4) { const y = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]; s += y; s2 += y * y; r += d[i]; g += d[i + 1]; b += d[i + 2]; n += 1; } const m = s / n; return { w: c.width, h: c.height, mean: m, std: Math.sqrt(Math.max(0, s2 / n - m * m)), rgb: [r / n, g / n, b / n].map((v) => Math.round(v)) }; };
  `;
  // 页面先出来、相机数据的状态随后才到（app.js）：等它到了再进大图，和用户从图库点进来一样（直接按地址打开会在它到之前搭好配方标签）
  await evalIn(`(async () => { const { state } = await import('/assets/state.js'); const t0 = performance.now(); while (performance.now() - t0 < 30000 && state.camdata?.ready !== true) await new Promise((r) => setTimeout(r, 50)); return state.camdata?.ready; })()`, 40000);
  const openPhoto = async (pid) => {
    await evalIn(`location.hash = '#/photo/${pid}'; true`);
    const t0 = Date.now();
    while (Date.now() - t0 < 60000) {
      if (await evalIn(`(async () => { const v = (await import('/assets/viewer.js')).currentView(); return !!v && v.photo.id === '${pid}' && !!v.ed && !!document.querySelector('.lcard'); })()`, 5000).catch(() => false)) return true;
      await sleep(100);
    }
    return false;
  };
  step('open photo');
  out.ready = await openPhoto(photoId);
  if (!out.ready) {
    out.page = await evalIn(`location.href + ' | ' + document.body.innerText.slice(0, 300)`, 5000).catch((e) => String(e));
    throw new Error(`配方卡片没有出现：${out.page}`);
  }
  if (mode === 'gpu') {
    step('wait for the WebGPU canvas');
    out.gpuWaitMs = await evalIn(`(async () => { ${helpers} const t0 = performance.now(); const ok = await until(() => document.querySelector('.gpu'), 120000); return ok ? Math.round(performance.now() - t0) : null; })()`, 130000);
  }
  out.gpu = await evalIn(`!!document.querySelector('.gpu')`);
  if (mode === 'gpu' && !out.gpu) {
    out.gpuDiag = await evalIn(diagnosticsExpression(photoId), 60000).catch((e) => ({ diagError: String(e) }));
    out.gpuSharedImageError = gpuLog.sharedImageError();
    out.gpuChromeLog = gpuLog.gpuLines();
    await finish(0);
  }

  // ---- 1. 卡片一张张出来，有真像素，各不相同 ---------------------------------------------------
  step('wait for all cards');
  // 一张都没有要画的（配方不能用）：马上交出原因，不等 200 秒
  out.why = await evalIn(`(async () => { ${helpers}
    await until(() => Number(stats()?.looksTotal) > 0, 15000);
    if (Number(stats()?.looksTotal) > 0) return null;
    return { caps: await (await fetch('/api/photos/${photoId}/capabilities')).json(), camdata: await (await fetch('/api/camdata/status')).json(), panel: document.querySelector('.edit-panel')?.textContent.slice(0, 300) };
  })()`, 30000);
  if (out.why) throw new Error('没有可画的卡片');
  const doneAt = await evalIn(`(async () => { ${helpers}
    const ok = await until(() => { const s = stats(); return s && Number(s.looksTotal) > 0 && s.looksPainted === s.looksTotal; }, 200000);
    return ok ? stats() : { timeout: true, stats: stats() };
  })()`, 210000);
  out.progress = doneAt;
  out.cards = await evalIn(`(() => { ${helpers}
    const list = [...document.querySelectorAll('.lcard')].filter((b) => b.querySelector('canvas'));
    const px = list.map((b) => ({ id: b.dataset.look, ...pixels(b.querySelector('canvas')) }));
    return { total: document.querySelectorAll('.lcard').length, canvases: list.length, blank: px.filter((p) => p.std < 3 || p.mean < 2 || p.mean > 253).map((p) => p.id), distinct: new Set(px.map((p) => p.rgb.join(','))).size, sizes: [...new Set(px.map((p) => p.w + 'x' + p.h))] };
  })()`);
  out.cardRequests = {
    bake: requestLog.filter((r) => /\/bake\?/.test(r)).length,
    serverCards: requestLog.filter((r) => /\/render\?.*max_edge=/.test(r)).length,
  };

  // ---- 2. 点一张卡片：用的就是原来的配方，卡片和舞台一样 ------------------------------------------
  step('click a card');
  const target = await evalIn(`(async () => { ${helpers}
    const b = card('builtin:3') || card('builtin:1');
    const id = b.dataset.look;
    const c = b.querySelector('canvas');
    const before = c.getContext('2d').getImageData(0, 0, c.width, c.height);
    window.__before = { w: c.width, h: c.height, data: before.data };
    const t0 = performance.now();
    b.click();
    const marked = await until(() => card(id)?.getAttribute('aria-pressed') === 'true', 5000);
    let put = null; // 保存排在后面写：等到它写进去
    for (let i = 0; i < 50 && !put; i++) { put = (await (await fetch('/api/photos/${photoId}/edit')).json()).recipe; if (!put) await wait(100); }
    return { id, markedMs: marked ? Math.round(performance.now() - t0) : null, put };
  })()`);
  out.clicked = target;
  step('wait for the stage');
  await sleep(2500);
  out.stage = await evalIn(`(async () => { ${helpers}
    await wait(500);
    const w = window.__before.w, h = window.__before.h;
    const c = document.createElement('canvas'); c.width = w; c.height = h;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    // 舞台的画面比卡片大：一半一半地缩到卡片的大小（单次缩小会有锯齿，和引擎的 2×2 超采样缩小不是一回事）
    const shrink = (src, sw, sh) => {
      let cur = src, cw = sw, ch = sh;
      while (cw / 2 >= w && ch / 2 >= h) {
        const t = document.createElement('canvas'); t.width = Math.round(cw / 2); t.height = Math.round(ch / 2);
        const tc = t.getContext('2d'); tc.imageSmoothingQuality = 'high'; tc.drawImage(cur, 0, 0, t.width, t.height);
        cur = t; cw = t.width; ch = t.height;
      }
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(cur, 0, 0, w, h);
    };
    let source = 'none';
    ${mode === 'gpu' ? `
    const st = await import('/assets/gpu_stage.js');
    const engine = st.gpuEngine();
    const cv = document.querySelector('.pv-gpu');
    if (engine && cv && engine.frame()) {
      // 等 GPU 读回显示遍的像素。无头 Chromium 里 drawImage(WebGPU canvas) 会拿到黑图。
      const snap = await engine.readPresent(cv.width, cv.height);
      const copy = document.createElement('canvas'); copy.width = snap.w; copy.height = snap.h;
      copy.getContext('2d').putImageData(new ImageData(new Uint8ClampedArray(snap.data), snap.w, snap.h), 0, 0);
      shrink(copy, snap.w, snap.h); source = 'gpu';
    }` : `
    const img = document.querySelector('.pv-img');
    if (img && img.complete && img.naturalWidth) { shrink(img, img.naturalWidth, img.naturalHeight); source = 'img'; }`}
    if (source === 'none') return { source };
    const now = ctx.getImageData(0, 0, w, h).data;
    const was = window.__before.data;
    const diffs = [];
    let sum = 0;
    for (let i = 0; i < was.length; i += 4) { for (let k = 0; k < 3; k++) { const d = Math.abs(was[i + k] - now[i + k]); sum += d; diffs.push(d); } }
    diffs.sort((a, b) => a - b);
    return { source, w, h, meanAbs: sum / diffs.length, p99: diffs[Math.floor(diffs.length * 0.99)], max: diffs[diffs.length - 1],
      label: [...document.querySelectorAll('.stage-labels .tag')].map((t) => t.textContent) };
  })()`, 30000);

  // ---- 3. 拖滑块：卡片不画，不替卡片发请求 ----------------------------------------------------
  // 配方标签里拖档位滑块：舞台自己要这个档位的配方表（和以前一样），但不能替别的卡片发请求；手按着时一张卡片也不画
  const dragIn = (selector) => `(async () => { ${helpers}
    const input = document.querySelector('${selector}');
    if (!input) return { error: 'no slider: ${selector}' };
    const before = stats();
    const frames = [];
    let last = performance.now(), run = true;
    const loop = () => { const t = performance.now(); frames.push(t - last); last = t; if (run) requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
    const min = Number(input.min), max = Number(input.max);
    input.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
    for (let i = 0; i < 30; i++) {
      input.value = String(min + Math.round(((max - min) * (i % 10)) / 9));
      input.dispatchEvent(new Event('input', { bubbles: true }));
      await wait(16);
    }
    await wait(900); // 手还按着：卡片的调度该一直等着
    const during = stats();
    run = false;
    return { before, during, frames: frames.slice(1).map((v) => Math.round(v)) };
  })()`;
  const release = (selector) => evalIn(`(() => { const input = document.querySelector('${selector}'); input.dispatchEvent(new PointerEvent('pointerup', { bubbles: true })); input.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`);
  step('drag a recipe slider');
  const knob = '.edit-panel .knob input[type=range]:not([disabled])';
  let mark = requestLog.length;
  out.drag = await evalIn(dragIn(knob), 30000);
  out.dragRequests = requestLog.slice(mark);
  await release(knob);
  await sleep(800);
  // 调整标签里拖曝光：显卡路径上整个拖动期间一个请求都没有，卡片（藏在后面的配方标签）也不动
  step('drag an adjust slider');
  await evalIn(`document.querySelector('#tab-adjust').click(); true`);
  await sleep(500);
  const adj = '#tp-adjust input[type=range]:not([disabled])';
  mark = requestLog.length;
  out.dragAdjust = await evalIn(dragIn(adj), 30000);
  out.dragAdjustRequests = requestLog.slice(mark);
  await release(adj);
  await sleep(500);
  await evalIn(`document.querySelector('#tab-recipe').click(); true`);

  // ---- 4. 换照片：旧照片的卡片停下 ------------------------------------------------------------
  step('switch photo');
  out.ready2 = await openPhoto(nextId);
  out.switched = await evalIn(`(async () => { ${helpers}
    const t0 = performance.now();
    const caught = await until(() => { const s = stats(); return s && Number(s.looksPainted) > 0 && Number(s.looksPainted) < Number(s.looksTotal); }, 20000);
    return { caught: !!caught, at: stats() };
  })()`, 30000);
  const switchMark = requestLog.length;
  await evalIn(`location.hash = '#/photo/${photoId}'; true`);
  await sleep(300);
  const settleMark = requestLog.length;
  await sleep(3000);
  const nextRe = new RegExp(`/photos/${nextId}/(bake|render|gpu/box)`);
  out.afterSwitch = {
    rightAway: requestLog.slice(switchMark, settleMark).filter((r) => nextRe.test(r)).length,
    later: requestLog.slice(settleMark).filter((r) => nextRe.test(r)).length,
  };
  step('done');
  out.requests = requestLog.length;
  await finish(0);
} catch (e) {
  out.error = String(e && e.message ? e.message : e);
  await finish(1);
}
