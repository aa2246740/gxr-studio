// 真浏览器里的裁切点按和边框卡片（tests/test_viewer_browser.py 起服务器和 Chrome，这里用 DevTools 协议驱动）。
// 用法：node viewer_browser.mjs <chrome 路径> <端口> <令牌> <照片 id> <gpu|server> <用户目录>
// 额外的 Chrome 开关放在环境变量 CHROME_FLAGS（JSON 数组）。输出一行 JSON，断言在 Python 那边。
// 保存请求（PUT .../crop）用 Fetch 域挂住，模拟后端忙或慢：挂着的时候界面也必须马上有反应。
import { spawn } from 'node:child_process';

const [chromePath, port, token, photoId, mode, profile] = process.argv.slice(2);
const extra = JSON.parse(process.env.CHROME_FLAGS || '[]');
const debugPort = 19500 + Math.floor(Math.random() * 400);
const chrome = spawn(chromePath, [
  '--headless=new', `--user-data-dir=${profile}`, `--remote-debugging-port=${debugPort}`, '--no-first-run', '--no-default-browser-check',
  '--disable-extensions', '--disable-background-networking', '--window-size=1500,950', ...extra, 'about:blank',
], { stdio: 'ignore' });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function connect() {
  for (let i = 0; i < 150; i += 1) {
    try {
      const v = await (await fetch(`http://127.0.0.1:${debugPort}/json/version`)).json();
      return new WebSocket(v.webSocketDebuggerUrl);
    } catch {
      await sleep(200);
    }
  }
  throw new Error('Chrome 没有启动');
}

let id = 0;
const pending = new Map();
const listeners = [];
let ws;
const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
  const mid = (id += 1);
  pending.set(mid, (msg) => (msg.error ? reject(new Error(`${method}: ${JSON.stringify(msg.error)}`)) : resolve(msg.result)));
  ws.send(JSON.stringify({ id: mid, method, params, sessionId }));
});

const out = { mode };
let exitCode = 0;
try {
  ws = await connect();
  await new Promise((r) => ws.addEventListener('open', r, { once: true }));
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
  const call = (method, params) => send(method, params, sessionId);
  const evalIn = async (expression) => {
    const r = await call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 800));
    return r.result.value;
  };

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
  await call('Page.navigate', { url: `${origin}/?token=${token}` });
  await sleep(1500);
  await evalIn(`localStorage.setItem('gxr-studio.gpu-preview', '${mode === 'gpu' ? 'on' : 'off'}'); true`);
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
    for (let i = 0; i < 600; i += 1) {
      if (await evalIn(`!!document.querySelector('#tab-crop')`).catch(() => false)) return true;
      await sleep(100);
    }
    return false;
  };
  out.ready = await ready();
  if (!out.ready) {
    out.page = await evalIn(`location.href + ' | ' + document.body.innerText.slice(0, 300)`).catch((e) => String(e));
    throw new Error(`大图没有打开：${out.page}`);
  }
  await sleep(1500);
  // 进裁切页：框要出现（显卡预览时画面是画布，框也要有）
  await evalIn(`(async () => { ${helpers} document.querySelector('#tab-crop').click(); await until(() => document.querySelector('.crop-box'), 20000); await wait(600); return true; })()`);
  out.gpu = await evalIn(`!!document.querySelector('.gpu')`);
  out.boxAfterEnter = await evalIn(`(() => { ${helpers} return boxRect(); })()`);

  // 1) 保存挂着时点比例：框马上变；放行后换成后端算的准框
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
  out.cards = await evalIn(`(async () => { ${helpers}
    const t0 = Date.now();
    document.querySelector('#tab-frame').click();
    const n = await until(() => document.querySelectorAll('.fcard').length, 5000);
    await until(() => document.querySelectorAll('.fcard .thumb img').length >= n, 60000);
    return { cards: n, imgs: document.querySelectorAll('.fcard .thumb img').length, from: t0 / 1000, to: Date.now() / 1000 };
  })()`);
} catch (e) {
  out.error = String(e && e.message ? e.message : e).slice(0, 800);
  exitCode = 1;
} finally {
  console.log(JSON.stringify(out));
  try { ws.close(); } catch { /* 已经关了 */ }
  chrome.kill('SIGTERM');
  await sleep(1200);
  chrome.kill('SIGKILL');
  process.exit(exitCode);
}
