// 电脑浏览器上没有一句话讲 iPhone、相册、共享面板、隔空投送、「文件」或触摸手势（desktop-copy，2026-11-26）。
// 同一套屏幕走三遍：Chromium 1440×900（没有触摸点、指针精确）、WebKit 手机 390×844 和 iPad 1032×1376（有触摸）。
// 每一屏收可见文字（innerText）和每个元素的 aria-label、title、placeholder。Python 那边对电脑查禁词，对手机和 iPad
// 和 3b5b535d 上抓下来的原话逐字比。默认校准由开发服务器 404（GXR_MOBILE_DEFAULT_CALIB=off），未校准那几句才会出来。
// 隐私：日期和钟点在页面里就换成「#」，不收照片名；stdout 只有一行 JSON。

import { existsSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, pinnedRuntime, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();
const BROWSER_DIRS = [process.env.PLAYWRIGHT_BROWSERS_PATH, pinnedRuntime.browsersPath].filter(Boolean);

const out = { pageErrors: [], runs: {}, error: null };
const GPU_ARGS = ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'];
const IPAD_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15';

/** iPadOS Safari：Mac 的 UA，但有 5 个触摸点。 */
function ipadTouch() {
  Object.defineProperty(Navigator.prototype, 'maxTouchPoints', { configurable: true, get: () => 5 });
  Object.defineProperty(Navigator.prototype, 'platform', { configurable: true, get: () => 'MacIntel' });
}

/** 手机有一个以上触摸点（WebKit 开 hasTouch 后本来就有，这里定成 1，和别的手机驱动一样）。 */
function phoneTouch() {
  Object.defineProperty(Navigator.prototype, 'maxTouchPoints', { configurable: true, get: () => 1 });
}

/** 在页面里收一块的可见文字和属性；日期、钟点、星期换成 #。 */
async function grab(page, selector = null) {
  return page.evaluate((sel) => {
    const mask = (s) => s.replace(/\d{4}年|\d{1,2}月\d{1,2}日|\d{1,2}:\d{2}(:\d{2})?|周[一二三四五六日]/g, '#');
    const roots = sel ? [...document.querySelectorAll(sel)].filter((n) => n.getClientRects().length > 0) : [document.body];
    if (!roots.length) return null;
    const attrs = [];
    const text = [];
    for (const root of roots) {
      text.push(root.innerText.trim());
      for (const node of [root, ...root.querySelectorAll('*')]) {
        for (const name of ['aria-label', 'title', 'placeholder']) {
          const value = node.getAttribute(name);
          if (value) attrs.push(`${name}=${value}`);
        }
        if (node.tagName.toLowerCase() === 'title' && node.textContent) attrs.push(`svg-title=${node.textContent}`);
      }
    }
    return { text: mask(text.join('\n---\n')), attrs: attrs.map(mask) };
  }, selector);
}

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

/** 关掉最上面那张表：先点它自己的「完成 / 取消 / 关闭」，不行就 Esc，再不行点遮罩。 */
async function closeTop(page) {
  const count = () => page.evaluate(() => document.querySelectorAll('.ms').length);
  const before = await count();
  if (!before) return;
  const done = page.locator('.ms').last().locator('button').filter({ hasText: /^(完成|取消|关闭)$|^先不用/ });
  if (await done.count()) await done.first().click().catch(() => undefined);
  if ((await count()) >= before) await page.keyboard.press('Escape');
  if ((await count()) >= before) await page.locator('.scrim').last().click({ position: { x: 5, y: 5 } }).catch(() => undefined);
  await page.waitForFunction((n) => document.querySelectorAll('.ms').length < n, before, { timeout: 5_000 }).catch(() => undefined);
}

async function visibleClick(page, selector, timeout = 15_000) {
  const loc = page.locator(selector).filter({ visible: true }).first();
  await loc.waitFor({ state: 'visible', timeout });
  await loc.click();
}

async function waitGrid(page, n) {
  await page.waitForFunction(
    (want) => {
      const sub = document.querySelector('.nav small');
      return sub && !sub.textContent.includes('正在读取') && document.querySelectorAll('.cell').length >= want;
    },
    n,
    { timeout: 180_000 },
  );
}

async function openRaw(page) {
  await page.locator('.cell[aria-label*="RAW"]').first().evaluate((node) => node.click());
  await page.waitForSelector('[data-screen="editor"]', { timeout: 120_000 });
  await page.locator('.editor .toolbar [data-act="export"]').first().waitFor({ state: 'visible', timeout: 60_000 });
}

async function editorMore(page) {
  await visibleClick(page, '.editor .nav [data-act="editor-more"]');
  await page.waitForSelector('[data-act="more-export-options"]', { state: 'visible', timeout: 15_000 });
}

/** 手动截图（GXR_DESKTOP_COPY_SHOTS 给了文件夹时才截，不进仓库）。 */
async function shot(page, label, name) {
  if (manifest.shotDir) await page.screenshot({ path: path.join(manifest.shotDir, `${label}-${name}.png`) }).catch(() => undefined);
}

/** 一个浏览器上下文从头走一遍。`label` 是 computer / phone / ipad。 */
async function walk(browser, label, options, inits) {
  const screens = {};
  const fails = {};
  const step = async (name, fn) => {
    try {
      screens[name] = await fn();
    } catch (err) {
      fails[name] = String(err?.message ?? err).split('\n')[0].slice(0, 200);
      const current = livePage;
      if (current && manifest.debugDir) {
        await current.screenshot({ path: path.join(manifest.debugDir, `${label}-${name}.png`) }).catch(() => undefined);
        fails[`${name}:sheets`] = await current.evaluate(() => [...document.querySelectorAll('.ms, .scrim')].map((n) => n.className).join('|')).catch(() => '');
      }
    }
  };
  let livePage = null;
  const newPage = async () => {
    const ctx = await browser.newContext(options);
    for (const init of inits) await ctx.addInitScript(init);
    const page = await ctx.newPage();
    page.on('pageerror', (err) => out.pageErrors.push(`${label}: ${err.message.slice(0, 200)}`));
    await page.goto(url);
    await settle(page);
    livePage = page;
    return { ctx, page };
  };

  // A：第一次打开，没有相机配方文件，默认校准下载不下来。
  const a = await newPage();
  const page = a.page;
  screens.env = await page.evaluate(() => ({
    touch: navigator.maxTouchPoints,
    fine: matchMedia('(pointer: fine)').matches,
    layout: document.querySelector('.app')?.dataset.layout ?? '',
  }));
  await page.waitForFunction(() => window.__gxrBook && !window.__gxrBook.loadingDefault, null, { timeout: 60_000 }).catch(() => undefined);
  await shot(page, label, 'start');
  await step('start', async () => ({
    ...(await grab(page, '.start')),
    hero: await page.evaluate(() => {
      const svg = document.querySelector('.start .hero svg');
      return { device: svg?.dataset.device ?? null, phone: Boolean(svg?.querySelector('rect[rx="22"]')) };
    }),
  }));
  await step('help', async () => {
    await visibleClick(page, '.start .help button');
    await page.waitForSelector('.ms', { timeout: 5_000 });
    await page.waitForTimeout(400);
    await shot(page, label, 'help');
    const seen = await grab(page, '.ms');
    await closeTop(page);
    return seen;
  });
  await step('grid', async () => {
    await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.photos);
    await waitGrid(page, manifest.count);
    return { ok: true };
  });
  await step('gridMore', async () => {
    await page.getByRole('button', { name: '更多', exact: true }).first().click();
    await page.waitForSelector('.ms', { timeout: 5_000 });
    const seen = await grab(page, '.ms');
    await closeTop(page);
    return seen;
  });
  await step('calib', async () => {
    await page.getByRole('button', { name: '更多', exact: true }).first().click();
    await page.locator('.ms .lrow', { hasText: '校准数据' }).click();
    await page.waitForSelector('.calib-sheet .calib[data-calib]', { timeout: 10_000 });
    await page.waitForTimeout(400);
    await shot(page, label, 'calib');
    const seen = await grab(page, '.calib-sheet');
    await closeTop(page);
    return seen;
  });
  // 没选相机配方文件就点开一张 RAW：「还差相机配方文件」那张表；在它的按钮上选 GXRCE.DAT 和 GXRFX.BIN。
  await step('askDat', async () => {
    await openRaw(page);
    await visibleClick(page, '.editor .toolbar [data-tool="recipe"]');
    if (!(await page.waitForSelector('.ms.ask-sheet', { timeout: 15_000 }).catch(() => null))) {
      await page.locator('.editor button').filter({ hasText: '选配方文件' }).filter({ visible: true }).first().click();
      await page.waitForSelector('.ms.ask-sheet', { timeout: 30_000 });
    }
    const seen = await grab(page, '.ms.ask-sheet');
    const chooser = page.waitForEvent('filechooser', { timeout: 10_000 });
    await page.locator('.ms.ask-sheet button.primary').click();
    await (await chooser).setFiles(manifest.dat);
    await page.waitForFunction(() => window.__gxrBook?.dat && !window.__gxrBook.datBusy, null, { timeout: 120_000 });
    await page.waitForFunction(() => !document.querySelector('.ms.ask-sheet'), null, { timeout: 10_000 }).catch(() => undefined);
    await closeTop(page);
    return seen;
  });
  await step('editor', async () => {
    await visibleClick(page, '.editor .toolbar [data-tool="recipe"]');
    await page.locator('.editor [data-state="no-calibration"]').filter({ visible: true }).first().waitFor({ timeout: 60_000 });
    return grab(page, '.editor [data-state="no-calibration"]');
  });
  await step('recipeStatus', async () => {
    await visibleClick(page, '.editor [data-state="no-calibration"]');
    await page.waitForSelector('.recipe-info-sheet', { timeout: 10_000 });
    await page.waitForTimeout(400);
    await shot(page, label, 'recipe-status');
    const seen = await grab(page, '.recipe-info-sheet');
    await closeTop(page);
    return seen;
  });
  await step('adjust', async () => {
    await visibleClick(page, '.editor .toolbar [data-tool="adjust"]');
    // 「点两下圆点回到 0」只在 iPad 版式（电脑 1440×900 也是）显示；手机上读 DOM 里那一句。
    await page.waitForSelector('.editor .adj-hint', { state: 'attached', timeout: 10_000 });
    await page.waitForTimeout(300);
    const seen = (await grab(page, '.editor .adj-hint')) ?? { text: '', attrs: [] };
    seen.dom = await page.evaluate(() => document.querySelector('.editor .adj-hint span')?.textContent ?? null);
    return seen;
  });
  await step('lut', async () => {
    await visibleClick(page, '.editor .toolbar [data-tool="recipe"]');
    await visibleClick(page, '[data-mode="lut"]');
    // 手机竖屏和横屏在 LUT 甲板里显示这句；iPad 版式（电脑 1440×900 也是）不显示，就读 DOM 里那一句。
    await page.waitForSelector('.lut-foot', { state: 'attached', timeout: 15_000 });
    await page.waitForTimeout(500);
    const seen = (await grab(page, '.lut-foot')) ?? { text: '', attrs: [] };
    seen.dom = await page.evaluate(() => document.querySelector('.lut-foot')?.textContent ?? null);
    await visibleClick(page, '[data-mode="recipe"]').catch(() => undefined);
    return seen;
  });
  await step('editorMore', async () => {
    await editorMore(page);
    const seen = await grab(page, '.ms[data-state="editor-more"], .more-panel');
    await closeTop(page);
    return seen;
  });
  await step('exportOptions', async () => {
    await editorMore(page);
    await visibleClick(page, '[data-act="more-export-options"]');
    await page.waitForSelector('.ms[data-state="export-sheet"]', { timeout: 15_000 });
    await page.waitForTimeout(300);
    const seen = await grab(page, '.ms[data-state="export-sheet"]');
    const glyphs = await page.evaluate((share) => {
      const sheet = document.querySelector('.ms[data-state="export-sheet"]');
      const btns = [...(sheet?.querySelectorAll('button') ?? [])].filter((b) => /下载|存到相册/.test(b.textContent ?? ''));
      return btns.map((b) => ({ text: b.textContent.trim(), share: [...b.querySelectorAll('svg path')].some((p) => p.getAttribute('d') === share) }));
    }, manifest.shareGlyph);
    await closeTop(page);
    return { ...seen, glyphs };
  });
  await step('offImage', async () => {
    await editorMore(page);
    await visibleClick(page, '[data-act="more-off-image"]');
    await page.waitForSelector('.ms.off-image-sheet', { timeout: 15_000 });
    await page.waitForTimeout(300);
    const seen = await grab(page, '.ms.off-image-sheet .oi-notes');
    await closeTop(page);
    return seen;
  });
  await step('logo', async () => {
    await visibleClick(page, '.editor .toolbar [data-tool="more"]');
    await visibleClick(page, '[data-more="frame"]');
    await page.waitForSelector('.panel.frame .rc[data-style="leica"]', { timeout: 15_000 });
    // 选「徕卡水印」，再点一次选中的样式就打开它的设置（手机和 iPad 版式都一样）。直接点节点：iPad 面板会滚动。
    const tap = (sel) => page.locator(sel).filter({ visible: true }).first().evaluate((node) => node.click());
    await tap('.panel.frame .rc[data-style="leica"]');
    await page.waitForSelector('.panel.frame .rc[data-style="leica"].on', { timeout: 15_000 }).catch(() => undefined);
    await page.waitForTimeout(500);
    await tap('.panel.frame .rc[data-style="leica"]');
    await page.locator('.panel.frame [data-act="frame-logo"]').filter({ visible: true }).first().waitFor({ timeout: 15_000 });
    await tap('.panel.frame [data-act="frame-logo"]');
    await page.waitForSelector('.ms.logo-sheet[data-state="logo"]', { timeout: 15_000 });
    const list = await grab(page, '.ms.logo-sheet');
    // 「添加标志」那一页才有「标志只保存在这台…上」。
    await page.locator('.ms.logo-sheet [data-logo-action="upload"]').evaluate((node) => node.click());
    await page.waitForSelector('.ms.logo-sheet[data-state="logo-upload"]', { timeout: 15_000 });
    const upload = await grab(page, '.ms.logo-sheet');
    await closeTop(page);
    return { text: `${list.text}\n===\n${upload.text}`, attrs: [...list.attrs, ...upload.attrs] };
  });
  // 整页（含编辑器）再查一遍属性和文字，电脑上用。
  await step('editorPage', async () => grab(page));
  await a.ctx.close();

  // B：选过相机配方文件以后重开：起始页「记住了」那一版、帮助、校准表里的相机配方文件卡。
  const b = await newPage();
  await step('remembered', async () => {
    await b.page.locator('input.filepick:not(.calibpick)').setInputFiles([...manifest.photos.slice(0, 2), ...manifest.dat]);
    await b.page.waitForFunction(() => window.__gxrBook?.dat && !window.__gxrBook.datBusy, null, { timeout: 120_000 });
    await b.page.reload();
    await settle(b.page);
    await b.page.waitForSelector('[data-camfiles="remembered"]', { timeout: 30_000 });
    await shot(b.page, label, 'start-remembered');
    return grab(b.page, '.start');
  });
  await step('rememberedHelp', async () => {
    await visibleClick(b.page, '.start .help button');
    await b.page.waitForSelector('.ms', { timeout: 5_000 });
    const seen = await grab(b.page, '.ms');
    await closeTop(b.page);
    return seen;
  });
  await step('rememberedCalib', async () => {
    await b.page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.photos.slice(0, 2));
    await waitGrid(b.page, 1);
    await b.page.getByRole('button', { name: '更多', exact: true }).first().click();
    const more = await grab(b.page, '.ms');
    await b.page.locator('.ms .lrow', { hasText: '校准数据' }).click();
    await b.page.waitForSelector('.calib-sheet .calib[data-calib]', { timeout: 10_000 });
    const seen = await grab(b.page, '.calib-sheet');
    await closeTop(b.page);
    return { text: `${more.text}\n===\n${seen.text}`, attrs: [...more.attrs, ...seen.attrs] };
  });
  await b.ctx.close();

  // C：读完缩略图以后拔卡（把复制出来的文件删掉），再点一张 RAW。
  if (manifest.cards?.[label]) {
    const card = manifest.cards[label];
    const c = await newPage();
    await step('pulled', async () => {
      await c.page.locator('input.filepick:not(.calibpick)').setInputFiles([...card.files, ...manifest.dat]);
      await waitGrid(c.page, card.count);
      await c.page.waitForTimeout(1500);
      rmSync(card.dir, { recursive: true, force: true });
      await c.page.locator('.cell[aria-label*="RAW"]').nth(1).evaluate((node) => node.click());
      // 编辑器先显示相机 JPG；换一张配方才去读 RAW，这时发现卡拔了。
      const removed = c.page.waitForSelector('[data-state="card-removed"]', { timeout: 60_000 });
      await c.page.waitForSelector('[data-screen="editor"]', { timeout: 30_000 }).catch(() => undefined);
      if (await c.page.$('[data-screen="editor"]')) {
        await c.page.locator('.editor .strip .rc').filter({ visible: true }).nth(2).click({ timeout: 30_000 }).catch(() => undefined);
      }
      await removed;
      return grab(c.page, '[data-state="card-removed"]');
    });
    rmSync(card.dir, { recursive: true, force: true });
    await c.ctx.close();
  }
  out.runs[label] = { screens, fails };
}

function webkitExecutable(webkit) {
  const direct = webkit.executablePath();
  if (direct && existsSync(direct)) return direct;
  const base = direct ? path.basename(direct) : 'pw_run.sh';
  for (const dir of BROWSER_DIRS) {
    if (!existsSync(dir)) continue;
    for (const entry of readdirSync(dir)) {
      if (!entry.startsWith('webkit-')) continue;
      const candidate = path.join(dir, entry, base);
      if (existsSync(candidate)) return candidate;
    }
  }
  return null;
}

try {
  const only = new Set(manifest.only ?? ['computer', 'phone', 'ipad']);
  if (only.has('computer')) {
    const chromium = await launchPinnedChromium({ args: GPU_ARGS });
    try {
      await walk(chromium, 'computer', { viewport: { width: 1440, height: 900 } }, []);
    } finally {
      await chromium.close();
    }
  }
  if (only.has('phone') || only.has('ipad')) {
    const { webkit } = await import(pathToFileURL(pinnedRuntime.modulePath).href);
    const executablePath = webkitExecutable(webkit);
    if (!executablePath) {
      out.webkitMissing = true;
    } else {
      const browser = await webkit.launch({ headless: true, executablePath });
      try {
        if (only.has('phone')) await walk(browser, 'phone', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true }, [phoneTouch]);
        if (only.has('ipad')) await walk(browser, 'ipad', { viewport: { width: 1032, height: 1376 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true, userAgent: IPAD_UA }, [ipadTouch]);
      } finally {
        await browser.close();
      }
    }
  }
} catch (err) {
  out.error = String(err?.message ?? err).split('\n')[0].slice(0, 300);
}
for (const card of Object.values(manifest.cards ?? {})) rmSync(card.dir, { recursive: true, force: true });
writeFileSync(manifest.reportPath, JSON.stringify(out));
process.stdout.write(`${JSON.stringify({ done: true, error: out.error !== null })}\n`);
