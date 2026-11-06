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
  ...extra, 'about:blank',
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
      return d;
    })()`, 60000).catch((e) => ({ diagError: String(e) }));
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

  // 6) 边框卡片：时间窗口交给 Python，对照服务器那边「选底图」跑了几次
  step('frame cards');
  out.cards = await evalIn(`(async () => { ${helpers}
    const t0 = Date.now();
    document.querySelector('#tab-frame').click();
    const n = await until(() => document.querySelectorAll('.fcard').length, 5000);
    await until(() => document.querySelectorAll('.fcard .thumb img').length >= n, 60000);
    return { cards: n, imgs: document.querySelectorAll('.fcard .thumb img').length, from: t0 / 1000, to: Date.now() / 1000 };
  })()`, 75000);
  step('done');
} catch (e) {
  out.error = String(e && e.message ? e.message : e).slice(0, 800);
  exitCode = 1;
}
await finish(exitCode);
