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
          const rewrite = window.__rewrites.get(kind);
          if (rewrite) {
            window.__rewrites.delete(kind);
            event.stopImmediatePropagation();
            const next = new MessageEvent('message', { data: { ...event.data, ...rewrite } });
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
  window.__hold = (kind) => window.__holdKinds.add(kind);
  window.__release = (override) => {
    const all = window.__held.splice(0);
    for (const run of all) run(override);
    return all.length;
  };
  window.__rewrite = (kind, data) => window.__rewrites.set(kind, data);
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
