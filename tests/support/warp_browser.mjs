// 真浏览器里拖 垂直 / 水平 / 畸变（tests/test_warp_browser.py 起服务器和 Chrome，这里用 DevTools 协议驱动）。
// 用法：node warp_browser.mjs <chrome 路径> <端口> <令牌> <照片 id> <gpu|server> <用户目录>
// 显卡预览：拖滑块的整个过程不请求 /render，也不请求 /gpu/box；每一次 input 之后的两帧之内，引擎的成片版本就变了（硬判据：
// 如果舞台在等服务器，两帧里不可能变，和机器忙不忙无关）；松手以后，裁切框和服务器收好的框一样；回到成片页，画布是裁切后的形状。
// 服务器出图（server）：行为不变，拖滑块照旧请求 /render。输出一行 JSON，断言在 Python 那边。
import { spawn } from 'node:child_process';
import { closeSync, openSync } from 'node:fs';
import { chromeLogReader, diagnosticsExpression } from './gpu_diag.mjs';

const [chromePath, port, token, photoId, mode, profile] = process.argv.slice(2);
const extra = JSON.parse(process.env.CHROME_FLAGS || '[]');
const deadlineMs = Number(process.env.VIEWER_DEADLINE_MS || 200000);
const debugPort = 19900 + Math.floor(Math.random() * 90);
const chromeLogPath = `${profile}.log`;
const chromeLog = openSync(chromeLogPath, 'w');
const chrome = spawn(chromePath, [
  '--headless=new', `--user-data-dir=${profile}`, `--remote-debugging-port=${debugPort}`, '--no-first-run', '--no-default-browser-check',
  '--disable-extensions', '--disable-background-networking', '--use-mock-keychain', '--password-store=basic', '--window-size=1500,950',
  '--enable-logging=stderr', '--v=0', ...extra, 'about:blank',
], { stdio: ['ignore', chromeLog, chromeLog] });

const started = Date.now();
const out = { mode, step: 'start' };
const step = (name) => {
  out.step = name;
  process.stderr.write(`[${((Date.now() - started) / 1000).toFixed(1)}s] ${name}\n`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const chromeLogFile = chromeLogReader(chromeLogPath);
const chromeTail = () => chromeLogFile.tail(3000);
let finished = false;
let ws;
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
const send = (method, params = {}, sessionId, ms = 30000) => new Promise((resolve, reject) => {
  const mid = (id += 1);
  const timer = setTimeout(() => { pending.delete(mid); reject(new Error(`${method} 超过 ${ms} ms 没有返回`)); }, ms);
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
    else if (msg.method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(msg.params.type)) text = msg.params.args.map((a) => a.value ?? a.description ?? '').join(' ');
    if (text) out.console = [...out.console.slice(-19), String(text).slice(0, 300)];
  });
  const requestLog = [];
  listeners.push((msg) => {
    if (msg.sessionId === sessionId && msg.method === 'Network.requestWillBeSent') requestLog.push(`${msg.params.request.method} ${msg.params.request.url}`);
  });
  await call('Network.enable');
  await call('Runtime.enable');
  await call('Page.enable');
  const origin = `http://127.0.0.1:${port}`;
  step('open with token');
  await call('Page.navigate', { url: `${origin}/?token=${token}` });
  await sleep(1500);
  await evalIn(`localStorage.setItem('gxr-studio.gpu-preview', '${mode === 'gpu' ? 'on' : 'off'}'); true`);
  step('open photo');
  await call('Page.navigate', { url: `${origin}/?open=1#/photo/${photoId}` });

  const helpers = `
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const raf = () => new Promise((r) => requestAnimationFrame(() => r()));
    const until = async (fn, ms) => { const t = performance.now(); while (performance.now() - t < ms) { const v = await fn(); if (v) return v; await wait(10); } return null; };
    const boxRect = () => { const b = document.querySelector('.crop-box'); return b ? [b.style.left, b.style.top, b.style.width, b.style.height].map((s) => parseFloat(s)) : null; };
    const serverCrop = async () => (await (await fetch('/api/photos/${photoId}/crop')).json());
    const serverGeometry = async () => (await (await fetch('/api/photos/${photoId}/edit')).json()).geometry || {};
    const range = (label) => document.querySelector('input[type=range][aria-label="' + label + '"]');
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  `;
  step('wait for the viewer');
  const t0 = Date.now();
  out.ready = false;
  while (Date.now() - t0 < 60000 && !out.ready) {
    out.ready = await evalIn(`!!document.querySelector('#tab-crop')`, 5000).catch(() => false);
    if (!out.ready) await sleep(100);
  }
  if (!out.ready) throw new Error('大图没有打开');
  await sleep(1500);
  step('enter crop');
  await evalIn(`(async () => { ${helpers} document.querySelector('#tab-crop').click(); await until(() => document.querySelector('.crop-box'), 20000); await wait(600); return true; })()`);
  if (mode === 'gpu') {
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
    // 没走显卡：把判断用到的几样东西交出去（和 viewer_browser.mjs 同一组字段，Python 的 cannot_present 据此决定 Linux 上跳过）
    out.gpuDiag = await evalIn(diagnosticsExpression(photoId), 60000).catch((e) => ({ diagError: String(e) }));
    out.gpuChromeLog = chromeLogFile.gpuLines();
    out.gpuSharedImageError = chromeLogFile.sharedImageError();
    out.consoleBeforeClicks = [...out.console];
    out.ready = true;
    await finish(0);
  }

  // 每个滑块：按下、10 步到目标值（每一步一次 input）、松手。每一步量：两帧之内引擎的成片版本变了没有、画布尺寸变了没有、耗时。
  step('drag the three sliders');
  const mark = requestLog.length;
  const targets = [['垂直', 'vertical', 35], ['水平', 'horizontal', -30], ['畸变', 'distortion', 40]];
  out.sliders = [];
  for (const [label, key, target] of targets) {
    const m = requestLog.length;
    const r = await evalIn(`(async () => { ${helpers}
      const eng = ${mode === 'gpu' ? `(await import('/assets/gpu_stage.js')).gpuEngineForTests()` : 'null'};
      const input = range(${JSON.stringify(label)});
      const steps = [];
      input.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
      for (let i = 1; i <= 10; i += 1) {
        setter.call(input, String(Math.round((${target} * i) / 10)));
        const version = eng ? eng.contentVersion : 0;
        const size = eng ? JSON.stringify(eng.canvasSize()) : '';
        const t = performance.now();
        input.dispatchEvent(new Event('input', { bubbles: true }));
        let frames = 0;
        while (frames < 2) {
          await raf();
          frames += 1;
          if (eng && eng.contentVersion > version) break;
        }
        steps.push({ frames, ms: Math.round(performance.now() - t), changed: eng ? eng.contentVersion > version : null, resized: eng ? JSON.stringify(eng.canvasSize()) !== size : null });
      }
      input.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return { steps, gpu: !!document.querySelector('.gpu'), box: boxRect() };
    })()`, 60000);
    await sleep(1500); // 服务器出图有 120 毫秒的防抖：等它发出来再数
    r.requestsWhileDragging = requestLog.slice(m).filter((q) => !/^PUT /.test(q));
    r.renders = r.requestsWhileDragging.filter((q) => /\/render\b/.test(q)).length;
    r.boxRequests = r.requestsWhileDragging.filter((q) => /\/gpu\/box/.test(q)).length;
    // 松手：几何写进服务器，框以服务器收好的为准（等写入完成再比）
    const saved = await evalIn(`(async () => { ${helpers}
      const ok = await until(async () => (await serverGeometry())['${key}'] === ${target}, 20000);
      await wait(1500);
      const s = await serverCrop();
      return { savedGeometry: !!ok, box: boxRect(), server: s.rect.map((v) => v * 100), gpu: !!document.querySelector('.gpu') };
    })()`, 40000);
    Object.assign(r, { label, key, target, afterRelease: saved });
    out.sliders.push(r);
  }
  // 自动：答案用替身（不靠检测器）——拉直 3°、垂直 −25。按下以后两帧之内引擎的成片版本就变了，没有 /render，保存以后框和服务器一样
  step('auto');
  const markAuto = requestLog.length;
  out.auto = await evalIn(`(async () => { ${helpers}
    const eng = ${mode === 'gpu' ? `(await import('/assets/gpu_stage.js')).gpuEngineForTests()` : 'null'};
    const realFetch = window.fetch;
    window.fetch = (url, opts) => /\\/geometry\\/auto/.test(String(url))
      ? Promise.resolve(new Response(JSON.stringify({ straighten: 3, vertical: -25, confident: true, message: '' }), { headers: { 'Content-Type': 'application/json' } }))
      : realFetch(url, opts);
    const button = [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === '自动');
    const version = eng ? eng.contentVersion : 0;
    const size = eng ? JSON.stringify(eng.canvasSize()) : '';
    const t = performance.now();
    button.click();
    let frames = 0;
    while (frames < 2) {
      await raf();
      frames += 1;
      if (eng && eng.contentVersion > version) break;
    }
    const first = { frames, ms: Math.round(performance.now() - t), changed: eng ? eng.contentVersion > version : null, resized: eng ? JSON.stringify(eng.canvasSize()) !== size : null };
    const ok = await until(async () => { const g = await serverGeometry(); return g.vertical === -25 && g.straighten === 3; }, 20000);
    await wait(1500);
    const s = await serverCrop();
    window.fetch = realFetch;
    return { ...first, savedGeometry: !!ok, box: boxRect(), server: s.rect.map((v) => v * 100), gpu: !!document.querySelector('.gpu') };
  })()`, 60000);
  out.auto.renders = requestLog.slice(markAuto).filter((q) => /\/render\b/.test(q)).length;
  out.dragRendersTotal = requestLog.slice(mark).filter((q) => /\/render\b/.test(q)).length;

  // 回到成片页：画布是裁切后的形状；走显卡的话一个 /render 也没有
  step('final stage');
  const markFinal = requestLog.length;
  out.final = await evalIn(`(async () => { ${helpers}
    const eng = ${mode === 'gpu' ? `(await import('/assets/gpu_stage.js')).gpuEngineForTests()` : 'null'};
    document.querySelector('#tab-adjust').click();
    await wait(1500);
    const s = await serverCrop();
    const cv = document.querySelector('.pv-gpu');
    return {
      gpu: !!document.querySelector('.gpu'),
      canvasAspect: cv ? cv.clientWidth / cv.clientHeight : null,
      engineSize: eng ? eng.finalSize() : null,
      engineCanvas: eng ? eng.canvasSize() : null,
      serverRect: s.rect,
    };
  })()`, 30000);
  out.final.renders = requestLog.slice(markFinal).filter((q) => /\/render\b/.test(q)).length;
  step('done');
} catch (e) {
  out.error = String(e && e.message ? e.message : e).slice(0, 800);
  exitCode = 1;
}
await finish(exitCode);
