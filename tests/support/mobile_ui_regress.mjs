// 复审 r1 的回归用例。每次跑一个 case，结果写到 stdout 的一行 JSON。
// 用法：node mobile_ui_regress.mjs <runtime.mjs> <base url> <manifest.json> <case>
// 故障注入只在页面里做：拦 Worker 回复（延迟或改写），或者把 navigator.gpu 藏起来。真实解码不替换。

import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';

const [runtime, base, manifestPath, caseName] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

/** 页面里的 Worker 钩子：window.__hold(kind) 扣住下一条该类回复，window.__release() 再放出去；window.__rewrite(kind, data) 改写下一条。 */
function hooks({ noGpu }) {
  if (noGpu) Object.defineProperty(navigator, 'gpu', { value: undefined, configurable: true });
  window.__held = [];
  window.__holdKinds = new Set();
  window.__rewrites = new Map();
  window.__fullReads = [];
  const Native = Worker;
  window.Worker = class extends Native {
    constructor(url, options) {
      super(url, options);
      if (!String(url).includes('/workers/')) return;
      this.addEventListener(
        'message',
        (event) => {
          if (event.__replayed) return;
          const kind = event.data && event.data.kind;
          if (kind === 'full') window.__fullReads.push(event.data.name);
          const rewrite = window.__rewrites.get(kind);
          if (rewrite && (!rewrite.name || rewrite.name === event.data.name)) {
            if (!rewrite.sticky) window.__rewrites.delete(kind);
            event.stopImmediatePropagation();
            const next = new MessageEvent('message', { data: { ...event.data, ...rewrite.data } });
            next.__replayed = true;
            this.dispatchEvent(next);
            return;
          }
          if (!window.__holdKinds.has(kind)) return;
          window.__holdKinds.delete(kind);
          event.stopImmediatePropagation();
          window.__held.push((override) => {
            const next = new MessageEvent('message', { data: { ...event.data, ...(override || {}) } });
            next.__replayed = true;
            this.dispatchEvent(next);
          });
        },
        true,
      );
    }
  };
  // 扣住下一次真实的 GPUQueue.onSubmittedWorkDone()：只推迟它的完成，显卡的计算照常做。
  window.__gpuHold = false;
  window.__gpuHeld = [];
  if (typeof GPUQueue !== 'undefined') {
    const nativeDone = GPUQueue.prototype.onSubmittedWorkDone;
    GPUQueue.prototype.onSubmittedWorkDone = function held() {
      const done = nativeDone.call(this);
      if (!window.__gpuHold) return done;
      window.__gpuHold = false;
      return new Promise((resolve, reject) => window.__gpuHeld.push(() => done.then(resolve, reject)));
    };
  }
  window.__gpuRelease = () => {
    const all = window.__gpuHeld.splice(0);
    for (const run of all) run();
    return all.length;
  };
  window.__hold = (kind) => window.__holdKinds.add(kind);
  window.__release = (override) => {
    const all = window.__held.splice(0);
    for (const run of all) run(override);
    return all.length;
  };
  /** 改写下一条（sticky 时每一条）该类回复；给了 name 就只改这个文件的。 */
  window.__rewrite = (kind, data, name, sticky) => window.__rewrites.set(kind, { data, name, sticky: !!sticky });
}

async function makePage({ gpu = true, width = 390, height = 844 } = {}) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2 });
  await context.addInitScript(hooks, { noGpu: !gpu });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(`${base}/`);
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const again = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!again) break;
  }
  return { page, context, errors };
}

/** Playwright 不让路径和内存里的文件混着传：混着时把路径读成 buffer。 */
function asFiles(files) {
  if (files.every((file) => typeof file === 'string')) return files;
  return files.map((file) => (typeof file === 'string' ? { name: file.split('/').pop(), mimeType: 'application/octet-stream', buffer: readFileSync(file) } : file));
}

async function pick(page, files) {
  await page.locator('input[type=file]').setInputFiles(asFiles(files));
  await page.waitForFunction(() => {
    const sub = document.querySelector('.nav small');
    return sub && !sub.textContent.includes('正在读取') && document.querySelectorAll('.cell').length > 0 && !document.querySelector('.cell.skel');
  }, null, { timeout: 120_000 });
}

function gridIds(page) {
  return page.locator('.cell').evaluateAll((nodes) => nodes.map((node) => node.dataset.id || ''));
}

async function swipeLeft(page) {
  const box = await page.locator('.stage').boundingBox();
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x - 140, y, { steps: 8 });
  await page.mouse.up();
}

async function editorState(page) {
  return page.evaluate(() => ({
    screen: document.querySelector('.app')?.dataset.screen,
    title: document.querySelector('.editor .nav b')?.textContent ?? '',
    index: document.querySelector('.editor .nav small')?.textContent ?? '',
  }));
}

function le16(value) {
  return [value & 255, (value >> 8) & 255];
}

function le32(value) {
  return [value & 255, (value >> 8) & 255, (value >> 16) & 255, (value >> 24) & 255];
}

function ifdEntry(tag, type, count, value) {
  return [...le16(tag), ...le16(type), ...le32(count), ...le32(value)];
}

/** 结构过得了 Worker 检查、但 WASM 描述不了的小 DNG（没有原始数据）。 */
function tinyDng() {
  const end = 8 + 2 + 12 * 2 + 4;
  return Buffer.from([0x49, 0x49, 0x2a, 0x00, ...le32(8), ...le16(2), ...ifdEntry(0x0111, 4, 1, end), ...ifdEntry(0xc612, 1, 4, 0x00000401), ...le32(0), 0, 0, 0, 0]);
}

async function cellClass(page, id) {
  return page.locator(`.cell[data-id="${id}"]`).getAttribute('class');
}

async function openAndRead(page, id) {
  await page.locator(`.cell[data-id="${id}"]`).click();
  await page.waitForFunction(
    () => document.querySelector('[data-state="unreadable"]') || document.querySelector('.editor .lbl')?.textContent,
    null,
    { timeout: 60_000 },
  );
  await page.waitForTimeout(500);
  const sheet = await page.locator('[data-state="unreadable"]').count();
  const out = {
    screen: await page.locator('.app').getAttribute('data-screen'),
    unreadable: sheet ? await page.locator('[data-state="unreadable"]').innerText() : '',
    label: await page.locator('.editor .lbl').innerText().catch(() => ''),
  };
  if (sheet) await page.getByRole('button', { name: '从这次去掉', exact: true }).click();
  else if (out.screen === 'editor') await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
  return out;
}

const CASES = {
  /** R2-3：中性小图在清空照片之后才回来：不再造位图挂到已经去掉的照片上，造了的要 close。 */
  async 'neutral-after-clear'() {
    const { page, context, errors } = await makePage({ gpu: true });
    try {
      await pick(page, manifest.pairs.slice(0, 2));
      await page.evaluate(() => {
        const engine = window.__gxrEngine;
        window.__thumbHeld = [];
        const original = engine.thumb.bind(engine);
        engine.thumb = (opts) => original(opts).then((out) => new Promise((resolve) => window.__thumbHeld.push(() => resolve(out))));
        window.__made = [];
        const native = window.createImageBitmap.bind(window);
        window.createImageBitmap = (...args) =>
          native(...args).then((bitmap) => {
            window.__made.push(bitmap);
            return bitmap;
          });
        const close = ImageBitmap.prototype.close;
        ImageBitmap.prototype.close = function closed() {
          this.__closed = true;
          return close.call(this);
        };
      });
      await page.locator('.cell').first().click();
      await page.waitForFunction(() => window.__thumbHeld.length === 1, null, { timeout: 120_000 });
      await page.getByRole('button', { name: '照片', exact: true }).click();
      await page.waitForSelector('[data-screen="grid"]');
      await page.getByRole('button', { name: '更多', exact: true }).click();
      await page.getByRole('button', { name: /清空照片，重新选/ }).click();
      await page.waitForSelector('[data-screen="start"]');
      const mark = await page.evaluate(() => window.__made.length);
      await page.evaluate(() => window.__thumbHeld.splice(0).forEach((run) => run()));
      await page.waitForTimeout(1500);
      const after = await page.evaluate((from) => {
        const made = window.__made.slice(from);
        return { made: made.length, open: made.filter((bitmap) => !bitmap.__closed && bitmap.width > 0).length };
      }, mark);
      const screen = await page.locator('.app').getAttribute('data-screen');
      return { after, screen, errors };
    } finally {
      await context.close();
    }
  },

  /** R2-2：三张里一张已在显卡、两张只有缩略图。拔卡后只有那一张算能接着用，另外两张算没读完、点开要重选。 */
  async 'card-removed-three'() {
    const { page, context, errors } = await makePage({ gpu: true });
    try {
      await pick(page, manifest.pairs.slice(0, 6));
      const ids = await gridIds(page);
      const [a, b, c] = ids;
      await page.locator(`.cell[data-id="${a}"]`).click();
      await page.waitForFunction(() => document.querySelector('.editor .lbl')?.textContent.includes('开发用中性预览'), null, { timeout: 120_000 });
      await page.getByRole('button', { name: '照片', exact: true }).click();
      await page.waitForSelector('[data-screen="grid"]');
      // 卡拔了：之后所有整份读取都回 gone。
      await page.evaluate(() => window.__rewrite('full', { ok: false, gone: true, bytes: undefined }, undefined, true));
      await page.locator(`.cell[data-id="${b}"]`).click();
      await page.waitForSelector('[data-state="card-removed"]', { timeout: 60_000 });
      const dialog = await page.locator('[data-state="card-removed"]').innerText();
      await page.getByRole('button', { name: '先用读进来的', exact: true }).click();
      const subtitle = await page.locator('.nav small').innerText();
      const classes = { a: await cellClass(page, a), b: await cellClass(page, b), c: await cellClass(page, c) };
      // 只有缩略图的那张：点开不进编辑器，直接告诉要重新选，数字不变。
      await page.locator(`.cell[data-id="${c}"]`).click();
      await page.waitForSelector('[data-state="card-removed"]', { timeout: 10_000 });
      const againDialog = await page.locator('[data-state="card-removed"]').innerText();
      const screenAfterC = await page.locator('.app').getAttribute('data-screen');
      await page.getByRole('button', { name: '先用读进来的', exact: true }).click();
      await page.locator(`.cell[data-id="${a}"]`).click();
      await page.waitForFunction(() => document.querySelector('.editor .lbl')?.textContent.includes('开发用中性预览'), null, { timeout: 60_000 });
      const reopened = await page.locator('.app').getAttribute('data-screen');
      return { ids, dialog, subtitle, classes, againDialog, screenAfterC, reopened, errors };
    } finally {
      await context.close();
    }
  },

  /** R2-1：同一张照片（同一个源键）新旧两次上传按相反顺序完成，最后装着的必须是新版本。 */
  async 'upload-race'() {
    const { page, context, errors } = await makePage({ gpu: true });
    try {
      const name = 'R8051144.DNG';
      const oldBytes = readFileSync(manifest.dngs[0]);
      const newBytes = readFileSync(manifest.dngs[manifest.dngs.length - 1]);
      await pick(page, [{ name, mimeType: 'application/octet-stream', buffer: oldBytes }]);
      await page.evaluate(() => {
        const engine = window.__gxrEngine;
        window.__uploads = [];
        const original = engine.addRaw.bind(engine);
        engine.addRaw = async (key, layout, dng, scene) => {
          const row = { key, done: false, tex: null };
          window.__uploads.push(row);
          await original(key, layout, dng, scene);
          row.done = true;
          row.tex = engine.sources.get(key)?.tex ?? null;
        };
        window.__gpuHold = true;
      });
      const label = () => page.waitForFunction(() => document.querySelector('.editor .lbl')?.textContent.includes('开发用中性预览'), null, { timeout: 120_000 });
      await page.locator('.cell').first().click();
      await page.waitForFunction(() => window.__uploads.length === 1 && window.__gpuHeld.length === 1, null, { timeout: 120_000 });
      await page.getByRole('button', { name: '照片', exact: true }).click();
      await page.waitForSelector('[data-screen="grid"]');
      await pick(page, [{ name, mimeType: 'application/octet-stream', buffer: newBytes }]);
      await page.locator('.cell').first().click();
      // 给新版本足够时间：旧代码里它会先装好、先出第一帧。
      await page.waitForTimeout(4000);
      const beforeRelease = await page.evaluate(() => ({ uploads: window.__uploads.map((row) => row.done), label: document.querySelector('.editor .lbl')?.textContent ?? '' }));
      const released = await page.evaluate(() => window.__gpuRelease());
      await page.waitForFunction(() => window.__uploads.length >= 2 && window.__uploads.every((row) => row.done), null, { timeout: 120_000 });
      await label();
      await page.waitForTimeout(500);
      await page.getByRole('button', { name: '照片', exact: true }).click();
      await page.waitForSelector('[data-screen="grid"]');
      await page.locator('.cell').first().click();
      await label();
      const after = await page.evaluate(() => {
        const engine = window.__gxrEngine;
        const installed = engine.sources.get('raw:r8051144');
        return {
          uploads: window.__uploads.length,
          installedFrom: window.__uploads.findIndex((row) => row.tex && installed && row.tex === installed.tex),
          currentMatches: engine.current === installed,
          sources: engine.stats().sources,
        };
      });
      return { beforeRelease, released, after, errors };
    } finally {
      await context.close();
    }
  },

  /** F7：裁剪、边框、LUT、曲线四页有稿上的静态控件，全部停用，写「这项下一步做」。 */
  async 'later-tabs'() {
    const { page, context, errors } = await makePage({ gpu: false });
    try {
      await pick(page, [manifest.jpgs[0]]);
      await page.locator('.cell').first().click();
      await page.waitForSelector('[data-screen="editor"]');
      const tabs = {};
      for (const name of ['裁剪', '边框', 'LUT', '曲线']) {
        await page.getByRole('tab', { name, exact: true }).click();
        tabs[name] = await page.evaluate(() => {
          const panel = [...document.querySelectorAll('.later-panel')].find((node) => !node.hidden);
          if (!panel) return null;
          const controls = [...panel.querySelectorAll('button, input')];
          return {
            text: panel.innerText,
            buttons: panel.querySelectorAll('button').length,
            inputs: panel.querySelectorAll('input').length,
            svg: panel.querySelectorAll('svg').length,
            enabled: controls.filter((node) => !node.disabled).length,
            controls: controls.length,
          };
        });
      }
      return { tabs, errors };
    } finally {
      await context.close();
    }
  },

  /** F6：RAW 第一帧就有直方图，静止时不读显卡，拖动后更新并点亮裁切三角，换片后重画。 */
  async 'histogram'() {
    const { page, context, errors } = await makePage({ gpu: true });
    try {
      await pick(page, manifest.pairs.slice(0, 4));
      const histState = () =>
        page.evaluate(() => {
          const canvas = document.querySelector('.hist canvas');
          const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
          let painted = 0;
          for (let i = 3; i < data.length; i += 4) if (data[i] > 0) painted += 1;
          return {
            version: canvas.dataset.version ?? null,
            painted,
            low: document.querySelector('.hist .clip.l')?.classList.contains('lit') ?? null,
            high: document.querySelector('.hist .clip.r')?.classList.contains('lit') ?? null,
          };
        });
      const waitDrawn = (prev) =>
        page.waitForFunction((old) => {
          const v = document.querySelector('.hist canvas')?.dataset.version;
          return v !== undefined && v !== old;
        }, prev ?? null, { timeout: 120_000 });
      await page.locator('.cell').first().click();
      await waitDrawn(null);
      const first = await histState();
      await page.evaluate(() => {
        window.__histCalls = 0;
        const engine = window.__gxrEngine;
        const original = engine.histogram.bind(engine);
        engine.histogram = (...args) => {
          window.__histCalls += 1;
          return original(...args);
        };
      });
      await page.waitForTimeout(2000);
      const idleCalls = await page.evaluate(() => window.__histCalls);
      const slider = page.locator('input[data-key="exposure_ev"]');
      const box = await slider.boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
      for (let step = 1; step <= 20; step += 1) {
        await page.mouse.move(box.x + box.width / 2 + (box.width / 2 - 6) * (step / 20), box.y + box.height / 2);
        await page.waitForTimeout(50);
      }
      await page.mouse.up();
      await waitDrawn(first.version);
      await page.waitForTimeout(1500);
      const dragged = await histState();
      const dragCalls = await page.evaluate(() => window.__histCalls);
      await page.waitForTimeout(2000);
      const settledCalls = (await page.evaluate(() => window.__histCalls)) - dragCalls;
      const exposure = await page.locator('.val').innerText();
      const title = await page.locator('.editor .nav b').innerText();
      await swipeLeft(page);
      await page.waitForFunction((prev) => document.querySelector('.editor .nav b')?.textContent !== prev, title, { timeout: 30_000 });
      await waitDrawn(null);
      const next = await histState();
      return { first, idleCalls, dragged, dragCalls, settledCalls, exposure, next, errors };
    } finally {
      await context.close();
    }
  },

  /** F4：有显卡时配方卡是这张的中性小图，只做一次；没显卡时如实写相机 JPG / 内嵌预览。 */
  async 'recipe-cards'() {
    const gpuRun = await (async () => {
      const { page, context, errors } = await makePage({ gpu: true });
      try {
        await pick(page, manifest.pairs.slice(0, 4));
        await page.evaluate(() => {
          window.__thumbCalls = 0;
          const engine = window.__gxrEngine;
          const original = engine.thumb.bind(engine);
          window.__thumbOriginal = original;
          engine.thumb = (opts) => {
            window.__thumbCalls += 1;
            return original(opts);
          };
        });
        const waitCards = () =>
          page.waitForFunction(() => {
            const thumbs = [...document.querySelectorAll('.editor > .strip .rc .th')];
            return thumbs.length && thumbs.every((node) => node.dataset.source === '中性转换' && node.querySelector('canvas'));
          }, null, { timeout: 120_000 });
        await page.locator('.cell').first().click();
        await waitCards();
        const first = await editorState(page);
        const labels = await page.locator('.editor > .strip .rc .sub').allInnerTexts();
        // 卡片和同一个源用显卡现出的中性小图比，缩到 96×64 后逐通道平均差。
        const diff = await page.evaluate(async () => {
          const key = `raw:${document.querySelector('.editor').dataset.photo.toLowerCase()}`;
          const geometry = { straighten: 0, rotate90: 0, flip: false, vertical: 0, horizontal: 0, distortion: 0, box: null };
          const adjust = { exposure_ev: 0, wb: { warm: 0, tint: 0 }, contrast: 0, highlight: 0, shadow: 0, black_point: 0, saturation: 0, vibrance: 0, vignette: 0 };
          const out = await window.__thumbOriginal({ source: key, look: { recipe: 'dev-neutral', lut: null, lutStrength: 1, curve: null, adjust }, geometry, aspect: null, maxEdge: 192 });
          const scale = (source) => {
            const c = document.createElement('canvas');
            c.width = 96;
            c.height = 64;
            const ctx = c.getContext('2d');
            ctx.drawImage(source, 0, 0, 96, 64);
            return ctx.getImageData(0, 0, 96, 64).data;
          };
          const ref = document.createElement('canvas');
          ref.width = out.w;
          ref.height = out.h;
          ref.getContext('2d').putImageData(new ImageData(new Uint8ClampedArray(out.data), out.w, out.h), 0, 0);
          const a = scale(document.querySelector('.editor > .strip .rc .th canvas'));
          const b = scale(ref);
          let sum = 0;
          let n = 0;
          for (let i = 0; i < a.length; i += 4) for (let c = 0; c < 3; c += 1) { sum += Math.abs(a[i + c] - b[i + c]); n += 1; }
          return sum / n;
        });
        const callsAfterFirst = await page.evaluate(() => window.__thumbCalls);
        await swipeLeft(page);
        await page.waitForFunction((prev) => document.querySelector('.editor .nav b')?.textContent !== prev, first.title, { timeout: 30_000 });
        await waitCards();
        const swipeBack = async () => {
          const box = await page.locator('.stage').boundingBox();
          await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
          await page.mouse.down();
          await page.mouse.move(box.x + box.width / 2 + 140, box.y + box.height / 2, { steps: 8 });
          await page.mouse.up();
        };
        const second = await editorState(page);
        await swipeBack();
        await page.waitForFunction((want) => document.querySelector('.editor .nav b')?.textContent === want, first.title, { timeout: 30_000 });
        await waitCards();
        const calls = await page.evaluate(() => window.__thumbCalls);
        return { labels, diff, callsAfterFirst, calls, first, second, errors };
      } finally {
        await context.close();
      }
    })();
    const fallback = await (async () => {
      const { page, context, errors } = await makePage({ gpu: false });
      try {
        await pick(page, [manifest.dngs[0], ...manifest.pairs.slice(2, 4)]);
        const out = {};
        for (const id of await gridIds(page)) {
          await page.locator(`.cell[data-id="${id}"]`).click();
          await page.waitForFunction(() => document.querySelector('.editor .lbl')?.textContent, null, { timeout: 60_000 });
          out[id] = {
            labels: await page.locator('.editor > .strip .rc .sub').allInnerTexts(),
            drawn: await page.locator('.editor > .strip .rc .th canvas').count(),
          };
          await page.getByRole('button', { name: '照片', exact: true }).click();
          await page.waitForSelector('[data-screen="grid"]');
        }
        return { out, dngOnly: manifest.dngs[0].split('/').pop().replace(/\.DNG$/i, '').toLowerCase(), errors };
      } finally {
        await context.close();
      }
    })();
    return { gpuRun, fallback, errors: [...gpuRun.errors, ...fallback.errors] };
  },

  /** F2：已渲染过的 RAW 在拔卡后照样打开、调整；真读不了的计入没读完；状态一直留着；重新选后恢复。 */
  async 'card-removed'() {
    const { page, context, errors } = await makePage({ gpu: true });
    try {
      await pick(page, manifest.pairs.slice(0, 4));
      const ids = await gridIds(page);
      const [a, b] = ids;
      const bName = `${b.toUpperCase()}.DNG`;
      const aName = `${a.toUpperCase()}.DNG`;
      await page.locator(`.cell[data-id="${a}"]`).click();
      await page.waitForFunction(() => document.querySelector('.editor .lbl')?.textContent.includes('开发用中性预览'), null, { timeout: 120_000 });
      const before = await page.evaluate(() => window.__gxrEngine.stats());
      await page.getByRole('button', { name: '照片', exact: true }).click();
      await page.waitForSelector('[data-screen="grid"]');
      // 卡拔了：之后对这个文件的整份读取都报 NotReadableError 那一类（Worker 回 gone）。
      await page.evaluate((name) => window.__rewrite('full', { ok: false, gone: true, bytes: undefined }, name, true), bName);
      await page.evaluate(() => { window.__fullReads = []; });
      await page.locator(`.cell[data-id="${b}"]`).click();
      await page.waitForSelector('[data-state="card-removed"]', { timeout: 60_000 });
      const dialog = await page.locator('[data-state="card-removed"]').innerText();
      await page.getByRole('button', { name: '先用读进来的', exact: true }).click();
      const subtitle = await page.locator('.nav small').innerText();
      // 普通刷新（换顺序）不能把拔卡状态盖掉。
      await page.getByRole('button', { name: '从晚到早', exact: true }).click();
      await page.getByRole('button', { name: '从早到晚', exact: true }).click();
      const subtitleAfterRefresh = await page.locator('.nav small').innerText();
      const bClass = await cellClass(page, b);
      await page.locator(`.cell[data-id="${b}"]`).click();
      const againDialog = await page.locator('[data-state="card-removed"]').count();
      await page.getByRole('button', { name: '先用读进来的', exact: true }).click();
      // 已经在显卡里的那张：不读卡，直接打开、拖曝光。
      await page.locator(`.cell[data-id="${a}"]`).click();
      await page.waitForFunction(() => document.querySelector('.editor .lbl')?.textContent.includes('开发用中性预览'), null, { timeout: 60_000 });
      const slider = page.locator('input[data-key="exposure_ev"]');
      const box = await slider.boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width * 0.7, box.y + box.height / 2, { steps: 8 });
      await page.mouse.up();
      const exposure = await page.locator('.val').innerText();
      const reopened = { screen: await page.locator('.app').getAttribute('data-screen'), disabled: await slider.isDisabled(), exposure };
      const after = await page.evaluate(() => window.__gxrEngine.stats());
      const readsAfterCard = await page.evaluate(() => window.__fullReads.slice());
      await page.getByRole('button', { name: '照片', exact: true }).click();
      await page.waitForSelector('[data-screen="grid"]');
      // 插回卡、重新选同样的照片：拔卡状态清掉，那张能打开。
      await page.evaluate(() => window.__rewrites.clear());
      await pick(page, manifest.pairs.slice(2, 4));
      const subtitleRepicked = await page.locator('.nav small').innerText();
      const bClassRepicked = await cellClass(page, b);
      await page.locator(`.cell[data-id="${b}"]`).click();
      await page.waitForFunction(() => document.querySelector('.editor .lbl')?.textContent.includes('开发用中性预览'), null, { timeout: 120_000 });
      const repickedTitle = await page.locator('.editor .nav b').innerText();
      return { a, b, aName, bName, before, after, dialog, subtitle, subtitleAfterRefresh, bClass, againDialog, reopened, readsAfterCard, subtitleRepicked, bClassRepicked, repickedTitle, errors };
    } finally {
      await context.close();
    }
  },

  /** F1：旧的成功、旧的失败、旧的拔卡回复在重新选同名照片之后才到，都不能盖掉新结果。 */
  async 'repick-stale'() {
    const real = readFileSync(manifest.jpgs[0]);
    const other = readFileSync(manifest.jpgs[manifest.jpgs.length - 1]);
    const name = 'R8051144.JPG';
    const variants = {
      'old-failure': { first: Buffer.from('corrupt old JPG'), override: null },
      'old-success': { first: other, override: null },
      'old-card-removed': { first: Buffer.from('corrupt old JPG'), override: { ok: false, gone: true } },
    };
    const results = {};
    for (const [label, variant] of Object.entries(variants)) {
      const { page, context, errors } = await makePage({ gpu: false });
      try {
        await page.evaluate(() => window.__hold('thumb'));
        await page.locator('input[type=file]').setInputFiles({ name, mimeType: 'image/jpeg', buffer: variant.first });
        await page.waitForFunction(() => window.__held.length === 1, null, { timeout: 30_000 });
        await pick(page, [{ name, mimeType: 'image/jpeg', buffer: real }]);
        const read = () =>
          page.evaluate(() => ({
            cls: document.querySelector('.cell')?.className ?? '',
            canvas: !!document.querySelector('.cell canvas'),
            header: document.querySelector('[data-kind="group"] .rng')?.textContent ?? '',
            subtitle: document.querySelector('.nav small')?.textContent ?? '',
            unreadable: document.querySelectorAll('[data-state="unreadable"]').length,
            card: document.querySelectorAll('[data-state="card-removed"]').length,
          }));
        const fresh = await read();
        const released = await page.evaluate((override) => window.__release(override || undefined), variant.override);
        await page.waitForTimeout(1500);
        const after = await read();
        results[label] = { fresh, released, after, errors };
      } finally {
        await context.close();
      }
    }
    return { results, errors: Object.values(results).flatMap((row) => row.errors) };
  },

  /** 规格第 2 项：整份 DNG 和邻居预读都在 Worker 里读，主线程不调 File.arrayBuffer。 */
  async 'full-read-in-worker'() {
    const { page, context, errors } = await makePage({ gpu: true });
    try {
      await page.evaluate(() => {
        window.__mainReads = [];
        const original = Blob.prototype.arrayBuffer;
        Blob.prototype.arrayBuffer = function patched() {
          if (this instanceof File) window.__mainReads.push(this.name);
          return original.call(this);
        };
      });
      await pick(page, manifest.pairs.slice(0, 4));
      const gpu = await page.locator('.app').getAttribute('data-gpu');
      await page.locator('.cell').first().click();
      await page.waitForFunction(() => document.querySelector('.app')?.dataset.frame === '1', null, { timeout: 120_000 });
      const first = await editorState(page);
      await page.waitForTimeout(1500);
      await swipeLeft(page);
      await page.waitForFunction((prev) => document.querySelector('.editor .nav b')?.textContent !== prev, first.title, { timeout: 30_000 });
      await page.waitForFunction(() => document.querySelector('.editor .lbl')?.textContent.includes('开发用中性预览'), null, { timeout: 120_000 });
      const second = await editorState(page);
      return { gpu, first, second, mainReads: await page.evaluate(() => window.__mainReads), errors };
    } finally {
      await context.close();
    }
  },

  /** F5：没有显卡时，坏头、截断的 IFD、结构对但描述不了的 DNG 都走到 06b；真 DNG 照常打开。 */
  async 'no-gpu-bad-dng'() {
    const { page, context, errors } = await makePage({ gpu: false });
    try {
      const truncated = Buffer.from([0x49, 0x49, 0x2a, 0x00, ...le32(8), ...le16(20), ...ifdEntry(0xc612, 1, 4, 0x00000401)]);
      await pick(page, [
        manifest.dng,
        { name: 'R9990002.DNG', mimeType: 'application/octet-stream', buffer: Buffer.from('II-not-a-TIFF-header') },
        { name: 'R9990003.DNG', mimeType: 'application/octet-stream', buffer: truncated },
        { name: 'R9990004.DNG', mimeType: 'application/octet-stream', buffer: tinyDng() },
      ]);
      await page.locator('.scrim').click().catch(() => undefined);
      const gpu = await page.locator('.app').getAttribute('data-gpu');
      const classes = {};
      for (const id of ['r9990002', 'r9990003', 'r9990004']) classes[id] = await cellClass(page, id);
      const opened = {};
      for (const id of ['r9990002', 'r9990003', 'r9990004']) opened[id] = await openAndRead(page, id);
      const real = (await gridIds(page))[0];
      opened.real = await openAndRead(page, real);
      return { gpu, classes, opened, left: await gridIds(page), errors };
    } finally {
      await context.close();
    }
  },

  /** F3：同一时间的三张，正序倒序下网格、序号、滑动一致。 */
  async 'equal-time-order'() {
    const { page, context, errors } = await makePage({ gpu: false });
    try {
      const jpg = readFileSync(manifest.jpg);
      const files = ['R9700001.JPG', 'R9700002.JPG', 'R9700003.JPG'].map((name) => ({ name, mimeType: 'image/jpeg', buffer: jpg }));
      await pick(page, files);
      const runs = {};
      for (const [label, order] of [['从晚到早', 'newest'], ['从早到晚', 'oldest']]) {
        await page.getByRole('button', { name: label, exact: true }).click();
        await page.waitForFunction((want) => document.querySelector(`[data-order="${want}"]`)?.classList.contains('on'), order);
        const ids = await gridIds(page);
        await page.locator(`.cell[data-id="${ids[0]}"]`).click();
        await page.waitForSelector('[data-screen="editor"]');
        const seen = [await editorState(page)];
        for (let i = 1; i < ids.length; i += 1) {
          const before = seen[seen.length - 1].title;
          await swipeLeft(page);
          await page.waitForFunction((prev) => document.querySelector('.editor .nav b')?.textContent !== prev, before, { timeout: 10_000 }).catch(() => undefined);
          seen.push(await editorState(page));
        }
        runs[order] = { ids, seen };
        await page.getByRole('button', { name: '照片', exact: true }).click();
        await page.waitForSelector('[data-screen="grid"]');
      }
      return { runs, errors };
    } finally {
      await context.close();
    }
  },
};

let out;
try {
  const run = CASES[caseName];
  if (!run) throw new Error(`unknown case ${caseName}`);
  out = { case: caseName, ok: true, result: await run() };
} catch (err) {
  out = { case: caseName, ok: false, error: err instanceof Error ? `${err.message}\n${err.stack}` : String(err) };
} finally {
  await browser.close();
}
process.stdout.write(`${JSON.stringify(out)}\n`);
