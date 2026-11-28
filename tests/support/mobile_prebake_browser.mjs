// 网格空闲预烘焙（prebake.ts）的真实浏览器验收：手机竖屏和 iPad，WebGPU（SwiftShader）。
// 网格上静下来以后 `.app[data-prebake]` 要变成 done；打开第一张时舞台用的表、点第一屏看得见的每一张卡用的表都必须是打开
// 之前就在 Book 缓存里的，而且点下去的同一个同步回调里舞台就换好（没有等烘焙）。另外数一数网格阶段同时在核心 Worker 上
// 跑的烘焙不超过 workers - 2 张（3 个和 4 个 Worker）。只输出结构化 JSON，不打印照片名、拍摄时间或 EXIF。

import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const [runtime, mobileUrl, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const failures = [];
const pageErrors = [];
const summary = {};
const check = (ok, msg) => {
  if (!ok) failures.push(String(msg));
  return Boolean(ok);
};

/** 数核心 Worker 上的烘焙：交出去几张、同时最多几张（按请求 id 配对回包）。 */
const COUNT_BAKES = (hc) => {
  if (hc > 0) Object.defineProperty(Navigator.prototype, 'hardwareConcurrency', { configurable: true, get: () => hc });
  const st = { posted: 0, inflight: 0, maxGrid: 0 };
  window.__pbBakes = st;
  const Orig = window.Worker;
  window.Worker = class extends Orig {
    constructor(url, opts) {
      super(url, opts);
      if (!/core\/worker|worker-/.test(String(url)) || /export_worker|off_image_worker|thumb/.test(String(url))) return;
      const ids = new Set();
      const post = this.postMessage.bind(this);
      this.postMessage = (msg, ...rest) => {
        if (msg && typeof msg === 'object' && msg.op === 'bake') {
          st.posted += 1;
          st.inflight += 1;
          ids.add(msg.id);
          if (document.querySelector('.app')?.dataset.screen === 'grid') st.maxGrid = Math.max(st.maxGrid, st.inflight);
        }
        return post(msg, ...rest);
      };
      this.addEventListener('message', (ev) => {
        const id = ev.data && typeof ev.data === 'object' ? ev.data.id : undefined;
        if (ids.delete(id)) st.inflight -= 1;
      });
    }
  };
};

async function importAll(page) {
  await page.goto(`${mobileUrl}/`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.app[data-screen="start"][data-core="1"]', { timeout: 45_000 });
  await page.locator('input.calibpick').setInputFiles(manifest.pack);
  await page.waitForFunction(() => window.__gxrBook?.pack?.kind === 'user' && !window.__gxrBook.packBusy, null, { timeout: 60_000 });
  await page.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.files);
  await page.waitForFunction(
    (n) => !(document.querySelector('.nav small')?.textContent ?? '').includes('正在读取') && document.querySelectorAll('.cell').length >= n,
    manifest.photos,
    { timeout: 90_000 },
  );
}

/** 等网格上的预烘焙做完；返回打开前 Book 缓存里的表。 */
async function waitPrebaked(page, label) {
  const t0 = Date.now();
  const done = await page
    .waitForFunction(() => document.querySelector('.app')?.dataset.prebake === 'done', null, { timeout: 60_000 })
    .then(() => true, () => false);
  const state = await page.evaluate(() => ({
    prebake: document.querySelector('.app')?.dataset.prebake ?? null,
    cached: [...(window.__gxrBook?.done?.keys?.() ?? [])],
    bakes: { ...window.__pbBakes },
  }));
  check(done, `${label}: grid pre-bake never reported done (data-prebake=${state.prebake})`);
  summary[`${label}PrebakeMs`] = Date.now() - t0;
  summary[`${label}Prebaked`] = state.cached.length;
  summary[`${label}MaxGridBakes`] = state.bakes.maxGrid;
  return state;
}

async function openFirst(page) {
  await page.locator('.cell').first().click();
  await page.waitForFunction(
    () => document.querySelector('.app')?.dataset.gpu === '1' && document.querySelector('.app')?.dataset.frame === '1',
    null,
    { timeout: 45_000 },
  );
  await page.waitForSelector('.strip[data-recipes][data-state="calibrated"]', { timeout: 30_000 });
  await page.waitForFunction(() => (document.querySelector('.app')?.dataset.recipeKey ?? '').startsWith('bake:'), null, { timeout: 30_000 });
}

/** 第一屏看得见、能用的卡（打开以后、没滚动过）。 */
async function visibleCards(page) {
  return page.evaluate(() => {
    const strip = document.querySelector('.strip[data-recipes]');
    const sr = strip.getBoundingClientRect();
    return [...strip.querySelectorAll('.rc[data-card]')]
      .filter((node) => {
        const r = node.getBoundingClientRect();
        return r.width > 0 && r.left >= sr.left - 1 && r.right <= sr.right + 1;
      })
      .map((node) => ({ id: node.dataset.card, usable: !node.classList.contains('lockd') }));
  });
}

/** 点一张卡：同一个回调里舞台的表就换好了，换上的表在打开前就做好了。 */
async function tapWarm(page, id, cached, label) {
  const res = await page.evaluate((card) => {
    const app = document.querySelector('.app');
    const node = document.querySelector(`.strip[data-recipes] .rc[data-card="${card}"]`);
    const before = app.dataset.recipeKey;
    node.click();
    return { before, after: app.dataset.recipeKey, on: node.classList.contains('on') };
  }, id);
  check(res.on, `${label}: tapping ${id} did not select it`);
  check(res.after !== res.before && res.after.startsWith('bake:'), `${label}: tapping ${id} did not switch the stage synchronously (waited for a bake)`);
  check(cached.includes(res.after), `${label}: ${id} used a table that was not pre-baked on the grid`);
  await page.waitForTimeout(80);
  return res;
}

async function layoutCase(browser, label, viewport, hc) {
  const ctx = await browser.newContext({ viewport, hasTouch: true, isMobile: true, deviceScaleFactor: 2 });
  try {
    const page = await ctx.newPage();
    page.on('pageerror', (err) => pageErrors.push(`${label}:${err.message}`));
    await page.addInitScript(COUNT_BAKES, hc);
    await importAll(page);
    const pre = await waitPrebaked(page, label);
    await openFirst(page);
    const stageKey = await page.evaluate(() => document.querySelector('.app').dataset.recipeKey);
    check(pre.cached.includes(stageKey), `${label}: the stage's first table was not pre-baked on the grid`);
    const cards = await visibleCards(page);
    summary[`${label}VisibleCards`] = cards.length;
    const usable = cards.filter((card) => card.usable).map((card) => card.id);
    check(usable.length >= 2, `${label}: expected at least two usable cards on the first screen, got ${usable.length}`);
    const on = await page.evaluate(() => document.querySelector('.strip[data-recipes] .rc.on')?.dataset.card ?? null);
    if (process.env.GXR_PREBAKE_DEBUG) summary[`${label}Debug`] = { stageKey, cached: pre.cached, on, cards };
    for (const id of usable) if (id !== on) await tapWarm(page, id, pre.cached, label);
    return pre;
  } finally {
    await ctx.close();
  }
}

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});
try {
  // 手机竖屏，4 个 Worker：网格上同时最多 2 张。
  const phone = await layoutCase(browser, 'phone', { width: 390, height: 844 }, 5);
  check(phone.bakes.maxGrid >= 1 && phone.bakes.maxGrid <= 2, `phone: ${phone.bakes.maxGrid} bakes ran at once on the grid with 4 workers (cap 2)`);
  // iPad 竖屏，3 个 Worker：网格上同时最多 1 张，第一屏的卡更多。
  const ipad = await layoutCase(browser, 'ipad', { width: 1024, height: 1366 }, 4);
  check(ipad.bakes.maxGrid === 1, `ipad: ${ipad.bakes.maxGrid} bakes ran at once on the grid with 3 workers (cap 1)`);
} finally {
  await browser.close();
}

console.log(JSON.stringify({ ok: failures.length === 0 && pageErrors.length === 0, failures, pageErrors, summary }));
process.exit(failures.length === 0 && pageErrors.length === 0 ? 0 : 1);
