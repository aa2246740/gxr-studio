// 真 Chrome 里的复制 / 粘贴调整：快捷键、图库批量粘贴（进度、结果、缩略图、撤销）、编辑器粘贴的一次撤销、输入框里不触发。
// tests/test_paste_browser.py 起服务器和 Chrome，这里用 DevTools 协议驱动。
// 用法：node paste_browser.mjs <chrome 路径> <端口> <令牌> <用户目录>；额外的 Chrome 开关在环境变量 CHROME_FLAGS（JSON 数组）。
// 输出一行 JSON，断言在 Python 那边。每一步都有超时，总时限在 PASTE_DEADLINE_MS。
import { spawn } from 'node:child_process';
import { closeSync, openSync, readFileSync } from 'node:fs';

const [chromePath, port, token, profile] = process.argv.slice(2);
const extra = JSON.parse(process.env.CHROME_FLAGS || '[]');
const deadlineMs = Number(process.env.PASTE_DEADLINE_MS || 200000);
const debugPort = 19900 + Math.floor(Math.random() * 400);
const chromeLogPath = `${profile}.log`;
const chromeLog = openSync(chromeLogPath, 'w');
const chrome = spawn(chromePath, [
  '--headless=new', `--user-data-dir=${profile}`, `--remote-debugging-port=${debugPort}`, '--no-first-run', '--no-default-browser-check',
  '--disable-extensions', '--disable-background-networking', '--use-mock-keychain', '--password-store=basic', '--window-size=1500,950',
  ...extra, 'about:blank',
], { stdio: ['ignore', chromeLog, chromeLog] });

const started = Date.now();
const out = { step: 'start' };
const step = (name) => {
  out.step = name;
  process.stderr.write(`[${((Date.now() - started) / 1000).toFixed(1)}s] ${name}\n`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws;
let finished = false;
async function finish(code) {
  if (finished) return;
  finished = true;
  if (code !== 0) out.chromeLog = (() => { try { return readFileSync(chromeLogPath, 'utf8').slice(-2000); } catch { return ''; } })();
  console.log(JSON.stringify(out));
  try { ws.close(); } catch { /* 已经关了 */ }
  chrome.kill('SIGTERM');
  await sleep(1000);
  chrome.kill('SIGKILL');
  try { closeSync(chromeLog); } catch { /* 已经关了 */ }
  process.exit(code);
}
setTimeout(() => { out.error = `超过总时限 ${deadlineMs} ms，卡在：${out.step}`; finish(1); }, deadlineMs);

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
    } else if (msg.method) listeners.forEach((fn) => fn(msg));
  });
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  const call = (method, params, ms) => send(method, params, sessionId, ms);
  const evalIn = async (expression, ms) => {
    const r = await call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, ms);
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 800));
    return r.result.value;
  };
  const waitFor = async (expression, what, ms = 20000) => {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      const v = await evalIn(expression);
      if (v) return v;
      await sleep(80);
    }
    throw new Error(`等不到：${what}`);
  };
  out.console = [];
  listeners.push((msg) => {
    if (msg.sessionId !== sessionId) return;
    let text = null;
    if (msg.method === 'Runtime.exceptionThrown') text = msg.params.exceptionDetails?.exception?.description || msg.params.exceptionDetails?.text;
    else if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') text = msg.params.args.map((a) => a.value ?? a.description ?? '').join(' ');
    if (text) out.console = [...out.console.slice(-19), String(text).slice(0, 300)];
  });
  const requestLog = [];
  const paused = [];
  let holdPaste = false;
  listeners.push(async (msg) => {
    if (msg.sessionId !== sessionId) return;
    if (msg.method === 'Network.requestWillBeSent') requestLog.push(`${msg.params.request.method} ${new URL(msg.params.request.url).pathname}`);
    if (msg.method === 'Fetch.requestPaused') {
      if (holdPaste) paused.push(msg.params.requestId);
      else await call('Fetch.continueRequest', { requestId: msg.params.requestId }).catch(() => {});
    }
  });
  await call('Network.enable');
  await call('Runtime.enable');
  await call('Page.enable');
  await call('Fetch.enable', { patterns: [{ urlPattern: '*/api/edits/paste', requestStage: 'Request' }] });
  const posts = () => requestLog.filter((r) => r === 'POST /api/edits/paste').length;
  const release = async () => { for (const requestId of paused.splice(0)) await call('Fetch.continueRequest', { requestId }); };

  // 按键：modifiers 的 meta 是 4、shift 是 8
  const press = async (key, code, modifiers = 0) => {
    const base = { key, code, modifiers, windowsVirtualKeyCode: key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0 };
    await call('Input.dispatchKeyEvent', { type: 'rawKeyDown', ...base });
    await call('Input.dispatchKeyEvent', { type: 'keyUp', ...base });
  };
  const META_SHIFT = 12;
  const toastTexts = `[...document.querySelectorAll('#toast-root .toast-msg')].map((e) => e.textContent)`;
  const api = (path, opts = '{}') => evalIn(`import('/assets/api.js').then((m) => m.api(${JSON.stringify(path)}, ${opts}))`);

  const adjustViaApi = (photo) => api(`/api/photos/${photo.id}/edit`).then((e) => ({ contrast: e.adjust.contrast || 0, slot: e.recipe && e.recipe.slot, curve: !!e.curve }));
  const origin = `http://127.0.0.1:${port}`;
  step('open with token');
  await call('Page.navigate', { url: `${origin}/?token=${token}` });
  await sleep(1500);
  await evalIn(`localStorage.setItem('gxr-studio.gpu-preview', 'off'); localStorage.removeItem('gxr-studio.paste-groups'); true`);
  await call('Page.navigate', { url: `${origin}/#/photos` });
  await waitFor(`document.querySelectorAll('[role="option"]').length > 5`, 'the gallery grid');

  const photos = await api('/api/photos');
  const byName = Object.fromEntries(photos.map((p) => [p.name, p]));
  const source = byName.R0000101;
  const target = byName.R0000102;
  const jpgOnly = byName.R0000106;
  const later = byName.R0000103;
  out.photoCount = photos.length;
  // 来源照片：配方 + 调整 + 曲线
  await api(`/api/photos/${source.id}/edit`, JSON.stringify({
    method: 'PUT',
    body: { recipe: { kind: 'builtin', unit: 'jlaunch', slot: 2, levels: {} }, adjust: { exposure_ev: 0.4, wb: { warm: 6, tint: 0 }, contrast: 8, highlight: 0, shadow: 0 }, curve: { master: [[0, 0], [128, 118], [255, 255]] } },
  }));

  step('open the source in the editor and copy');
  await evalIn(`location.hash = '#/photo/${source.id}'; true`);
  await waitFor(`!!document.querySelector('#tb-edit') && [...document.querySelectorAll('#tb-edit button')].some((b) => b.textContent.includes('复制调整'))`, 'the editor titlebar');
  await waitFor(`(async () => { const m = await import('/assets/paste_core.js'); return !!m && document.querySelector('#tab-recipe') && document.querySelector('.editor [aria-busy="true"]') === null; })()`, 'the editor to load');
  await sleep(400);
  out.titlebar = await evalIn(`document.querySelector('#tb-edit').textContent`);
  out.pasteDisabledBeforeCopy = await evalIn(`[...document.querySelectorAll('#tb-edit button')].find((b) => b.textContent.includes('粘贴调整')).getAttribute('aria-disabled')`);
  await press('C', 'KeyC', META_SHIFT);
  await waitFor(`${toastTexts}.some((t) => t.includes('已复制'))`, 'the copy toast');
  out.copyToast = (await evalIn(toastTexts)).find((t) => t.includes('已复制'));
  out.clipFrom = await evalIn(`import('/assets/paste_core.js').then((m) => m.clipboard() && m.clipboard().from.name)`);
  out.pasteDisabledAfterCopy = await evalIn(`[...document.querySelectorAll('#tb-edit button')].find((b) => b.textContent.includes('粘贴调整')).getAttribute('aria-disabled')`);

  step('shortcuts do not fire while typing');
  await evalIn(`location.hash = '#/photo/${later.id}'; true`);
  await waitFor(`document.querySelector('#tb-edit .name') && document.querySelector('#tb-edit .name').textContent === '${later.name}'`, 'the second photo');
  await sleep(500);
  await evalIn(`(() => { const i = document.createElement('input'); i.id = 'scratch'; i.type = 'text'; document.body.append(i); i.focus(); return true; })()`);
  const toastsBefore = (await evalIn(toastTexts)).length;
  const postsBefore = posts();
  await press('C', 'KeyC', META_SHIFT);
  await press('V', 'KeyV', META_SHIFT);
  await sleep(600);
  out.typing = { newToasts: (await evalIn(toastTexts)).length - toastsBefore, newPosts: posts() - postsBefore, clipFrom: await evalIn(`import('/assets/paste_core.js').then((m) => m.clipboard().from.name)`) };
  await evalIn(`document.getElementById('scratch').remove(); document.activeElement && document.activeElement.blur(); true`);

  step('the shortcut list names the keys');
  await press('?', 'Slash', 8);
  await waitFor(`document.querySelector('.keys-grid')`, 'the shortcut list');
  out.helpText = await evalIn(`document.querySelector('.keys-grid').textContent`);
  await press('Escape', 'Escape');
  await waitFor(`!document.querySelector('.keys-grid')`, 'the shortcut list to close');

  step('paste in the editor is one undo step');
  await evalIn(`document.getElementById('toast-root').replaceChildren(); true`);
  await press('V', 'KeyV', META_SHIFT);
  await waitFor(`${toastTexts}.some((t) => t.includes('已套用'))`, 'the editor paste toast');
  out.editorToast = (await evalIn(toastTexts)).find((t) => t.includes('已套用'));
  out.editorPosts = posts() - postsBefore;
  out.afterEditorPaste = await adjustViaApi(later);
  out.panelAfterPaste = await evalIn(`document.querySelector('#tb-edit').textContent`);
  await press('z', 'KeyZ', 4);
  await waitFor(`import('/assets/api.js').then((m) => m.api('/api/photos/${later.id}/edit')).then((e) => e.recipe === null && !e.adjust.contrast)`, 'one undo to restore everything');
  out.afterOneUndo = await adjustViaApi(later);

  step('gallery batch paste');
  await evalIn(`document.getElementById('toast-root').replaceChildren(); location.hash = '#/photos'; true`);
  await waitFor(`document.querySelectorAll('[role="option"]').length > 5 && !document.querySelector('#view .editor')`, 'the gallery again');
  const thumbOf = (name) => `(() => { const c = [...document.querySelectorAll('[role="option"]')].find((o) => o.textContent.includes('${name}') || (o.getAttribute('aria-label') || '').includes('${name}')); const i = c && c.querySelector('img'); return i ? i.getAttribute('src') : null; })()`;
  out.thumbBefore = await evalIn(thumbOf(target.name));
  await evalIn(`document.getElementById('grid-wrap').focus(); true`);
  await press('a', 'KeyA', 4);
  await waitFor(`document.querySelector('#selbar') && !document.querySelector('#selbar').hidden`, 'the selection bar');
  out.selbar = await evalIn(`document.querySelector('#selbar').textContent`);
  holdPaste = true;
  await press('V', 'KeyV', META_SHIFT);
  await waitFor(`${toastTexts}.some((t) => t.includes('正在粘贴调整'))`, 'the progress toast');
  const total = photos.length - 1;
  out.progress = [];
  for (let chunk = 0; chunk < Math.ceil(total / 10); chunk += 1) {
    const end = Date.now() + 15000;
    while (!paused.length && Date.now() < end) await sleep(50);
    out.progress.push((await evalIn(toastTexts)).find((t) => t.includes('正在粘贴调整')));
    out.progressBar = await evalIn(`!!document.querySelector('#toast-root .progress')`);
    await release();
    await sleep(150);
  }
  holdPaste = false;
  await waitFor(`${toastTexts}.some((t) => t.includes('已套用到'))`, 'the result toast');
  out.resultToast = (await evalIn(toastTexts)).find((t) => t.includes('已套用到'));
  out.resultHasUndo = await evalIn(`[...document.querySelectorAll('#toast-root .toast button')].some((b) => b.textContent === '撤销')`);
  out.batchPosts = posts() - postsBefore - out.editorPosts;
  out.progressGone = await waitFor(`${toastTexts}.every((t) => !t.includes('正在粘贴调整'))`, 'the progress toast to go', 3000).then(() => true, () => false);
  out.afterBatch = {
    target: await adjustViaApi(target),
    jpgOnly: await adjustViaApi(jpgOnly),
    source: await adjustViaApi(source),
  };
  await waitFor(`${thumbOf(target.name)} !== ${JSON.stringify(out.thumbBefore)}`, 'the thumbnail to refresh', 10000).catch(() => null);
  out.thumbAfter = await evalIn(thumbOf(target.name));

  step('undo the batch');
  await evalIn(`[...document.querySelectorAll('#toast-root .toast button')].find((b) => b.textContent === '撤销').click(); true`);
  await waitFor(`${toastTexts}.some((t) => t.includes('已撤销'))`, 'the undo toast');
  out.undoToast = (await evalIn(toastTexts)).find((t) => t.includes('已撤销'));
  out.afterUndo = { target: await adjustViaApi(target), jpgOnly: await adjustViaApi(jpgOnly) };
  await sleep(300);
  out.thumbRestored = await evalIn(thumbOf(target.name));
  out.selectionKept = await evalIn(`document.querySelector('#selbar') && !document.querySelector('#selbar').hidden`);
  out.console = out.console.filter((l) => !/favicon|404/.test(l));

  step('done');
  out.ready = true;
  await finish(0);
} catch (e) {
  out.error = String(e && e.stack ? e.stack : e).slice(0, 1500);
  await finish(1);
}
