// 真浏览器里的裁切点按和边框卡片（tests/test_viewer_browser.py 起服务器和 Chrome，这里用 DevTools 协议驱动）。
// 用法：node viewer_browser.mjs <chrome 路径> <端口> <令牌> <照片 id> <gpu|server> <用户目录>
// 额外的 Chrome 开关放在环境变量 CHROME_FLAGS（JSON 数组）。输出一行 JSON，断言在 Python 那边。
// 保存请求（PUT .../crop）用 Fetch 域挂住，模拟后端忙或慢：挂着的时候界面也必须马上有反应。
// 每一步都有超时，总时限在环境变量 VIEWER_DEADLINE_MS（Python 那边的进程超时之前）：卡住时也输出 JSON，
// 里面的 step 是卡住的那一步，chromeLog 是 Chrome 自己的输出末尾。进度逐行写到 stderr。
import { spawn } from 'node:child_process';
import { closeSync, openSync, readFileSync } from 'node:fs';
import { walkRounds } from './frame_nav_walk.mjs';
import { chromeLogReader, diagnosticsExpression } from './gpu_diag.mjs';
import { walkLibrary, walkLibraryRefresh } from './library_walk.mjs';
import { walkPanelLayout } from './panel_layout_walk.mjs';
import { walkSliderKeys } from './slider_keys_walk.mjs';

const [chromePath, port, token, photoId, mode, profile] = process.argv.slice(2);
const extra = JSON.parse(process.env.CHROME_FLAGS || '[]');
const deadlineMs = Number(process.env.VIEWER_DEADLINE_MS || 200000);
const delayRenderMs = Number(process.env.VIEWER_DELAY_RENDER_MS || 0);
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
// Chrome 日志（尾部、整份、和显卡有关的行、共享图像的错）：和 warp_browser.mjs 共用 gpu_diag.mjs
const gpuLog = chromeLogReader(chromeLogPath);
let finished = false;
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
  let holdEdit = false; // 编辑页的场景：把 PUT .../edit 也挂住
  let fakeAuto = null; // 编辑页的场景：POST .../geometry/auto 由这里挂住、再给固定的答案
  const held = [];
  const pausedAuto = [];
  listeners.push(async (msg) => {
    if (msg.method !== 'Fetch.requestPaused' || msg.sessionId !== sessionId) return;
    const p = msg.params;
    if (holding && p.request.method === 'PUT' && /\/crop$/.test(p.request.url)) held.push(p.requestId);
    else if (holdEdit && p.request.method === 'PUT' && /\/edit$/.test(p.request.url)) held.push(p.requestId);
    else if (fakeAuto && p.request.method === 'POST' && /\/geometry\/auto$/.test(p.request.url)) pausedAuto.push(p.requestId);
    else {
      // VIEWER_DELAY_RENDER_MS：舞台自己的出图（不是边框卡片 480 像素的小底图）慢这么久才回来，模拟忙的机器或慢的服务器
      if (delayRenderMs && p.request.method === 'POST' && p.request.url.includes('/render?') && !p.request.url.includes('max_edge=480')) await sleep(delayRenderMs);
      await call('Fetch.continueRequest', { requestId: p.requestId }).catch(() => {});
    }
  });
  const answerAuto = async (json) => {
    for (const requestId of pausedAuto.splice(0)) {
      await call('Fetch.fulfillRequest', {
        requestId,
        responseCode: 200,
        responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
        body: Buffer.from(JSON.stringify(json)).toString('base64'),
      });
    }
  };
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
  await call('Fetch.enable', { patterns: ['*/crop', '*/edit', '*/geometry/auto', ...(delayRenderMs ? ['*/render*'] : [])].map((urlPattern) => ({ urlPattern, requestStage: 'Request' })) });
  await call('Page.enable');
  // 每个新页面都装一个记录：照片出图（/render?…）什么时候「发出」、有几个还在路上、最近一次动静是什么时候。
  // 数「回来」的（resource timing 只记完成的）会把前一步发出、这一步才回来的出图算到这一步头上，机器一慢就误报
  await call('Page.addScriptToEvaluateOnNewDocument', {
    source: `(() => {
      const real = window.fetch.bind(window);
      const log = (window.__fetchLog = { renders: 0, inflight: 0, lastAt: 0 });
      window.fetch = (...args) => {
        const first = args[0];
        const isRender = String((first && first.url) || first).includes('/render?');
        if (isRender) { log.renders += 1; log.inflight += 1; log.lastAt = performance.now(); }
        const p = real(...args);
        if (isRender) { const done = () => { log.inflight -= 1; log.lastAt = performance.now(); }; p.then(done, done); }
        return p;
      };
    })()`,
  });

  // 第一次带令牌打开（换成 cookie），再选预览方式，然后进这张照片
  const origin = `http://127.0.0.1:${port}`;
  step('open with token');
  await call('Page.navigate', { url: `${origin}/?token=${token}` });
  await sleep(1500);
  // NAV_NO_WEBGPU=1：页面里没有 navigator.gpu（走一遍「显卡接不了手」的路，不管这台机器有没有显卡）
  if (process.env.NAV_NO_WEBGPU) await call('Page.addScriptToEvaluateOnNewDocument', { source: "Object.defineProperty(navigator, 'gpu', { value: undefined, configurable: true })" });
  step('set preview preference');
  await evalIn(`localStorage.setItem('gxr-studio.gpu-preview', '${/gpu$/.test(mode) ? 'on' : 'off'}'); true`);
  if (mode === 'library' || mode === 'library-refresh') {
    out.library = await (mode === 'library' ? walkLibrary : walkLibraryRefresh)({ call, evalIn, step });
    out.ready = true;
    await finish(0);
  }
  // 没走显卡时把判断用到的几样东西都交出去（开关、本机选择、适配器、这张照片的 /gpu、引擎能不能建、Chrome 日志里有没有共享图像的错）：
  // gpu 模式和 nav-gpu 模式共用
  const collectGpuDiagnostics = async () => {
    step('collect WebGPU diagnostics');
    out.gpuDiag = await evalIn(diagnosticsExpression(photoId), 60000).catch((e) => ({ diagError: String(e) }));
    // 这张照片的 JPG 源图：照页面的做法取回来、解码（共用的诊断里没有这一项）
    const jpg = await evalIn(`(async () => {
      const d = {};
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
      return d;
    })()`, 30000).catch((e) => ({ jpgDiagError: String(e) }));
    Object.assign(out.gpuDiag, jpg);
    out.gpuChromeLog = gpuLog.gpuLines();
    out.gpuSharedImageError = gpuLog.sharedImageError();
    out.failedBeforeClicks = [...out.failedRequests];
    out.consoleBeforeClicks = [...out.console];
  };
  if (mode === 'layout') {
    // 边框和时间戳的版面在真 Chrome 里算：字体表从服务器下载（和导出读的是同一份），版面只用表里的整数，交给 Python 的结果对
    step('load the font tables and lay out');
    const cases = readFileSync(process.env.LAYOUT_CASES, 'utf8');
    out.layout = await evalIn(`(async () => {
      const L = await import('/assets/finish_layout.js');
      const F = await import('/assets/finish_fonts.js');
      const data = ${cases};
      await Promise.all(['atkinson', 'inter', 'courier', 'patrick', 'dseg'].map((id) => F.loadFont(id)));
      const m = F.metrics;
      const frames = data.frames.map((c) => L.frameLayout(c.w, c.h, c.spec, c.meta, m));
      const stamps = data.stamps.map((c) => { const text = L.stampText(c.spec, c.when); return { text, layout: L.stampLayout(c.w, c.h, c.spec, text, m) }; });
      return { frames, stamps };
    })()`, 120000);
    out.ready = true;
    await finish(0);
  }
  if (mode.startsWith('nav-')) {
    // 换照片、换边框、进出裁剪、100%：每一步之后大图还在不在（tests/support/frame_nav_walk.mjs）
    const walked = await walkRounds({ call, evalIn, origin, step, wantGpu: mode === 'nav-gpu', collectGpuDiagnostics, presentErrors: () => gpuLog.sharedImageError() });
    out.nav = walked.steps;
    out.gpuWaitMs = walked.gpuWaitMs;
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
  if (mode.startsWith('panel-')) {
    const tab = mode.slice('panel-'.length);
    out.panel = await walkPanelLayout({ call, evalIn }, tab, tab === 'frame' ? [1280, 1440] : [375, 768, 1024, 1280, 1440]);
    await finish(0);
  }
  if (mode === 'slider-keys') {
    out.keys = await walkSliderKeys({ call, evalIn });
    await finish(0);
  }
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
  if (mode === 'gpu' && !out.gpu) await collectGpuDiagnostics();
  out.boxAfterEnter = await evalIn(`(() => { ${helpers} return boxRect(); })()`);

  if (process.env.VIEWER_SCENARIO === 'edit-fixes') {
    // 2026-11-07 的两个编辑页问题，在真服务器和真 Chrome 里走一遍
    // 1) 小红书 3:4 → 缩放 → 拉直 → 自动（带透视）→ 向左旋转：不能 422、不能留「有修改没有保存」、面板不能成骨架
    // 2) 调整页曲线上点一下，保存还没回来时按一次撤销，点就消失
    const fx = {};
    out.fx = fx;
    const fxHelpers = `${helpers}
      const slide = async (label, value) => {
        const input = document.querySelector('input[aria-label="' + label + '"]');
        input.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, String(value));
        input.dispatchEvent(new Event('input', { bubbles: true }));
        await wait(50);
        input.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
      };
      const key = (k) => document.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }));
      const saveText = () => document.querySelector('.save-state')?.innerText || '';
      const quiet = async () => { await wait(300); await until(() => !/保存中/.test(saveText()), 10000); await wait(300); };
      const toasts = () => [...document.querySelectorAll('.toast')].map((t) => t.textContent);
    `;
    step('edit-fixes: ratio, zoom, straighten');
    await evalIn(`(async () => { ${fxHelpers}
      await pickRatio('小红书 3:4');
      await wait(600);
      await slide('缩放', 71);
      await wait(600);
      const r = document.querySelector('.ruler');
      r.focus();
      for (let i = 0; i < 30; i += 1) r.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, repeat: i > 0 }));
      r.dispatchEvent(new KeyboardEvent('keyup', { key: 'ArrowRight', bubbles: true }));
      await quiet();
      return true;
    })()`, 60000);
    step('edit-fixes: auto');
    fakeAuto = true;
    fx.autoDuring = await evalIn(`(async () => { ${fxHelpers}
      const find = (t) => [...document.querySelectorAll('.sticky-foot button')].find((b) => b.textContent.trim() === t);
      find('自动').click();
      await wait(200);
      const b = find('正在分析…');
      const again = find('正在分析…');
      if (again) again.click();
      return { text: b ? b.textContent.trim() : null, busy: b ? b.getAttribute('aria-busy') : null, disabled: b ? b.getAttribute('aria-disabled') : null };
    })()`);
    await sleep(300);
    out.autoPaused = pausedAuto.length; // 点了两下，只发出一个请求
    await answerAuto({ straighten: 4.0, vertical: 60, confident: true, message: '' });
    fakeAuto = null;
    fx.autoAfter = await evalIn(`(async () => { ${fxHelpers}
      await until(() => toasts().some((t) => t.includes('已自动')), 5000);
      await quiet();
      const b = [...document.querySelectorAll('.sticky-foot button')].find((x) => x.textContent.trim() === '自动');
      return { toasts: toasts(), restored: !!b && b.getAttribute('aria-busy') === null && b.getAttribute('aria-disabled') === null };
    })()`, 30000);
    step('edit-fixes: rotate');
    fx.rotate = await evalIn(`(async () => { ${fxHelpers}
      key('[');
      await wait(100);
      const skelRightAfter = document.querySelectorAll('.panel .skel').length;
      await quiet();
      await wait(1500);
      const s = await serverCrop();
      return { skelRightAfter, skel: document.querySelectorAll('.panel .skel').length, save: saveText(), box: boxRect(), server: s.rect.map((v) => v * 100), frame: s.frame };
    })()`, 60000);
    step('edit-fixes: curve undo');
    await evalIn(`(async () => { ${fxHelpers} document.querySelector('#tab-adjust').click(); await until(() => document.querySelector('svg.curve'), 5000); await wait(500); document.querySelector('svg.curve').scrollIntoView({ block: 'center' }); await wait(300); return true; })()`);
    const curveBox = await evalIn(`(() => { const r = document.querySelector('svg.curve').getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()`);
    fx.curveBefore = await evalIn(`document.querySelector('.curve-wrap .small').textContent`);
    holdEdit = true;
    const x = curveBox[0] + curveBox[2] * 0.5;
    const y = curveBox[1] + curveBox[3] * 0.3;
    await call('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
    await call('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
    await sleep(200);
    fx.curveAfterClick = await evalIn(`document.querySelector('.curve-wrap .small').textContent`);
    fx.heldWrites = held.length; // 曲线的保存还挂着
    fx.curveAfterUndo = await evalIn(`(async () => { ${fxHelpers}
      document.querySelector('#tb-edit button[title^="撤销"]').click();
      await wait(200);
      return document.querySelector('.curve-wrap .small').textContent;
    })()`);
    holdEdit = false;
    await release('continue');
    await sleep(800);
    step('done');
    await finish(0);
  }

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
  out.cards.renderUrls = requestLog.slice(mark).filter((r) => /\/render\b/.test(r)).map((r) => r.replace(/^POST http:\/\/[^/]+/, '').replace(/token=[^&\s]+/, 'token=…'));

  // 7) 加了边框和时间戳以后：显卡还在管，拖滑块不请求 /render，点样式、改选项当帧就画出来
  step('frame and stamp with the preview on');
  mark = requestLog.length;
  out.live = await evalIn(`(async () => { ${helpers}
    const raf = () => new Promise((r) => requestAnimationFrame(() => r()));
    const deco = () => { const pv = document.querySelector('.pv'); return { framed: pv.classList.contains('framed'), overlay: !!pv.querySelector('.pv-deco'), size: [pv.style.width, pv.style.height] }; };
    const card = (name) => [...document.querySelectorAll('.fcard')].find((x) => x.textContent.trim() === name);
    // 前一步（离开裁剪页、画边框卡片）发出的出图可能还在路上，或者刚发出：等到安静下来（没有在路上的、最近 0.8 秒没有新的）再开始数，
    // 数的是点下去之后「发出」的 /render，不是「回来」的：前一步留下的那一个晚到，不算在这些点击头上
    const fetchLog = window.__fetchLog;
    const quiet = await until(() => fetchLog.inflight === 0 && performance.now() - fetchLog.lastAt > 800, 30000);
    const renders = () => fetchLog.renders;
    const renders0 = renders();
    // 每个「当帧画出来」的操作都记三样：点下（或输入）之后的下一帧，叠加层的画法（key）变了没有（服务器出图的话一帧里不可能变）；
    // 耗时多少；这一次操作前后各三帧里最长的一帧是多长（机器忙时帧本身就拖长，拿它当这一次的尺子）
    const key = () => document.querySelector('.pv .pv-deco')?.dataset.key;
    const gaps = async (n) => { const out = []; for (let last = performance.now(), i = 0; i < n; i += 1) { await raf(); const now = performance.now(); out.push(now - last); last = now; } return out; };
    const clicks = [];
    const timed = async (name, fn) => {
      const lead = await gaps(3);
      const k0 = key();
      const t = performance.now();
      fn();
      await raf();
      const ms = Math.round(performance.now() - t);
      const changed = key() !== k0;
      const trail = await gaps(3);
      clicks.push({ name, ms, changed, frameMs: Math.round(Math.max(...lead, ...trail)) });
    };
    const styleFramed = {};
    for (const name of ['徕卡水印', '哈苏水印', '胶片', '拍立得', '画布', '白边', '徕卡水印']) {
      await timed(name, () => card(name).click());
      styleFramed[name] = deco().framed;
    }
    await wait(200);
    const frame = deco();
    // 换一个选项（颜色）也一样
    await timed('color', () => document.querySelector('.swrow button[title="黑"], .swrow button[aria-label="黑"]')?.click());
    // 时间戳
    document.querySelector('#tab-stamp').click();
    await wait(200);
    await timed('stamp', () => document.querySelector('#tp-stamp input[role=switch]').click());
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
    const puts0 = edits.length;
    let lastKey = 0;
    await timed('typing', () => {
      input.value = 'Pa';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.value = 'Paris'; // 连着打：两下之间没有停顿，只会保存一次（机器再忙，中间也不会隔 400 毫秒）
      input.dispatchEvent(new Event('input', { bubbles: true }));
      lastKey = performance.now();
    });
    await wait(100);
    const putsEarly = edits.length - puts0; // 还在 400 ms 之内：没有保存
    const earlyValid = performance.now() - lastKey < 350; // 机器忙得这一步拖过了 350 毫秒，这一项没有意义，不判
    await wait(900);
    const putsLate = edits.length - puts0;
    const typed = { keyChanged: clicks[clicks.length - 1].changed, putsEarly, earlyValid, putsLate, focusKept: document.activeElement === input, saved: (await (await fetch('/api/photos/${photoId}/edit')).json()).stamp.text };
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
    // 等画面成为想要的样子再量（机器忙时慢一点；等了 15 秒还不是，就量到什么交什么，由断言去报）
    const settle = async (want) => { await until(() => { const l = look(); return l && want(l) ? l : null; }, 15000); await wait(200); return look(); };
    const framed = (l) => l.framed && l.overlay;
    press('100%');
    const zoom100 = await settle(framed);
    press('适合');
    const fit = await settle(framed);
    // 换照片：下一张没有边框（叠加层不跟过去），换回来边框和日期戳还在
    document.querySelector('button[aria-label^="下一张"]').click();
    const next = await settle((l) => !l.framed && !l.overlay);
    document.querySelector('button[aria-label^="上一张"]').click();
    const back = await settle(framed);
    return { quiet: !!quiet, clicks, styleFramed, frame, stampOn, clickRenders, dragRenders, typed, gpu: gpuNow, dragMs, worstFrameMs: Math.max(...frames), zoom100, fit, next, back };
  })()`, 90000);
  out.live.requests = requestLog.slice(mark).filter((r) => !/^PUT /.test(r) && !/\/(edit|crop)$/.test(r)).slice(-8);
  // 步骤 7 里发出的每一个 /render（带地址）：失败时看得出是哪一个
  out.live.renderUrls = requestLog.slice(mark).filter((r) => /\/render\b/.test(r)).map((r) => r.replace(/token=[^&\s]+/, 'token=…'));
  step('done');
} catch (e) {
  out.error = String(e && e.message ? e.message : e).slice(0, 800);
  exitCode = 1;
}
await finish(exitCode);
