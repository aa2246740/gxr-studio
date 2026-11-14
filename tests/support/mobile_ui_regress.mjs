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
          window.__held.push(() => {
            const next = new MessageEvent('message', { data: event.data });
            next.__replayed = true;
            this.dispatchEvent(next);
          });
        },
        true,
      );
    }
  };
  window.__hold = (kind) => window.__holdKinds.add(kind);
  window.__release = () => {
    const all = window.__held.splice(0);
    for (const run of all) run();
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

async function pick(page, files) {
  await page.locator('input[type=file]').setInputFiles(files);
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

const CASES = {
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
