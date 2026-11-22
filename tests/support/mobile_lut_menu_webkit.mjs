// 手机竖屏 390×844 的 LUT 合并菜单 WebKit 触控验收（polish-2 G1 / G2）。
// 在 Playwright WebKit（hasTouch: true, isMobile: true）里用真实触摸 tap 测原生 invoker 与无 invoker 回退两条路径。
// 若本机未安装 Playwright WebKit，打印 skipped JSON 并以 0 退出，由 pytest 跳过。stdout 不含照片名、拍摄时间或 EXIF。

import { existsSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { pinnedRuntime, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const { webkit } = await import(pathToFileURL(pinnedRuntime.modulePath).href);

function resolveWebKitExecutable() {
  const direct = webkit.executablePath();
  if (direct && existsSync(direct)) return direct;
  const baseName = direct ? path.basename(direct) : 'pw_run.sh';
  if (existsSync(pinnedRuntime.browsersPath)) {
    for (const entry of readdirSync(pinnedRuntime.browsersPath)) {
      if (!entry.startsWith('webkit-')) continue;
      const candidate = path.join(pinnedRuntime.browsersPath, entry, baseName);
      if (existsSync(candidate)) return candidate;
    }
  }
  return null;
}

const executablePath = resolveWebKitExecutable();
if (!executablePath) {
  process.stdout.write(`${JSON.stringify({ skipped: 'Playwright WebKit 未安装（需运行 playwright install webkit）' })}\n`);
  process.exit(0);
}

async function waitPopover(page, wantOpen, timeout = 3_000) {
  try {
    await page.waitForFunction(
      (expected) => Boolean(document.querySelector('.lut-menu-pop:popover-open')) === expected,
      wantOpen,
      { timeout },
    );
    return true;
  } catch {
    return false;
  }
}

async function popoverState(page) {
  return page.evaluate(() => ({
    open: Boolean(document.querySelector('.lut-menu-pop:popover-open')),
    expanded: document.querySelector('.editor .panel.lut [data-act="lut-menu"]')?.getAttribute('aria-expanded') ?? null,
    hasInvokers: 'commandForElement' in HTMLButtonElement.prototype,
    hasCommandForAttr: Boolean(document.querySelector('.editor .panel.lut [data-act="lut-menu"]')?.hasAttribute('commandfor')),
  }));
}

async function openLutPanel(page) {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.files);
  await page.waitForFunction(
    () => !document.querySelector('.nav small')?.textContent?.includes('正在读取') && document.querySelectorAll('.cell').length >= 1,
    null,
    { timeout: 60_000 },
  );
  await page.locator('.cell').first().tap();
  await page.waitForSelector('.editor', { timeout: 30_000 });
  const askLater = page.locator('.ask-sheet button', { hasText: '先不用' });
  if (await askLater.isVisible({ timeout: 2_000 }).catch(() => false)) {
    await askLater.tap();
    await page.waitForFunction(() => !document.querySelector('.ask-sheet'), null, { timeout: 5_000 });
  }
  await page.locator('.editor .toolbar [data-tool="recipe"]').tap();
  if (await askLater.isVisible({ timeout: 500 }).catch(() => false)) {
    await askLater.tap();
    await page.waitForFunction(() => !document.querySelector('.ask-sheet'), null, { timeout: 5_000 });
  }
  await page.locator('.editor .panel.recipe .mode-switch [data-mode="lut"]').tap();
  await page.waitForFunction(
    () => {
      const panel = document.querySelector('.editor .panel.lut');
      return Boolean(panel && !panel.hidden);
    },
    null,
    { timeout: 15_000 },
  );
}

async function runScenario(browser, { disableInvokers }) {
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 1,
    hasTouch: true,
    isMobile: true,
  });
  if (disableInvokers) {
    await ctx.addInitScript(() => {
      try {
        delete HTMLButtonElement.prototype.commandForElement;
        delete HTMLButtonElement.prototype.command;
      } catch {
        // 某些引擎属性不可删时忽略
      }
      const origSetAttribute = Element.prototype.setAttribute;
      Element.prototype.setAttribute = function patchedSetAttribute(name, value) {
        const lower = String(name).toLowerCase();
        if (lower === 'commandfor' || lower === 'command') return;
        return origSetAttribute.call(this, name, value);
      };
    });
  }

  const page = await ctx.newPage();
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(String(err).slice(0, 200)));

  const res = {
    disableInvokers,
    initial: null,
    touchCycles: [],
    mouseCycles: [],
    outsideTapClosed: false,
    importWorks: false,
    spaceWorks: false,
    keyboardWorks: false,
    pageErrors,
  };

  try {
    await openLutPanel(page);
    res.initial = await popoverState(page);
    const tile = page.locator('.editor .panel.lut [data-act="lut-menu"]');

    // 触摸点开 + 再次触摸关闭（3 轮）
    for (let i = 0; i < 3; i += 1) {
      await tile.tap();
      const opened = await waitPopover(page, true);
      const stOpen = await popoverState(page);
      if (!opened) {
        res.touchCycles.push({ opened: false, closed: false, expandedOpen: stOpen.expanded, expandedClose: null });
        return res;
      }
      await tile.tap();
      const closed = await waitPopover(page, false);
      const stClose = await popoverState(page);
      res.touchCycles.push({ opened, closed, expandedOpen: stOpen.expanded, expandedClose: stClose.expanded });
      if (!closed) {
        // 清理现场，避免后续步骤卡死
        await page.keyboard.press('Escape');
        await waitPopover(page, false);
      }
    }

    // 鼠标点击开关（2 轮，确认 mouse pointerType 路径与无 invoker 回退同样成立）
    for (let i = 0; i < 2; i += 1) {
      await tile.click();
      const opened = await waitPopover(page, true);
      if (!opened) {
        res.mouseCycles.push({ opened: false, closed: false });
        continue;
      }
      await tile.click();
      const closed = await waitPopover(page, false);
      res.mouseCycles.push({ opened, closed });
      if (!closed) {
        await page.keyboard.press('Escape');
        await waitPopover(page, false);
      }
    }

    // 点开后轻触控制带标题栏空白处（菜单与触发瓷砖之外，且不触发点照片收起 HUD）关闭
    await tile.tap();
    if (await waitPopover(page, true)) {
      const outside = await page.evaluate(() => {
        const hd = document.querySelector('.editor .band-hd');
        const r = hd?.getBoundingClientRect();
        return r ? { x: Math.round(r.right - 24), y: Math.round(r.top + r.height / 2) } : { x: 350, y: 682 };
      });
      await page.touchscreen.tap(outside.x, outside.y);
      res.outsideTapClosed = await waitPopover(page, false);
    }

    // 触摸点开 → 触摸「导入 LUT」触发文件选择器并完成导入
    await tile.tap();
    if (await waitPopover(page, true)) {
      const [chooser] = await Promise.all([
        page.waitForEvent('filechooser', { timeout: 10_000 }),
        page.locator('.lut-menu-pop:popover-open [data-act="lut-menu-import"]').tap(),
      ]);
      await chooser.setFiles([manifest.lutA]);
      await page.waitForFunction(() => (window.__gxrLutBook?.list?.()?.length ?? 0) >= 1, null, { timeout: 30_000 });
      res.importWorks = await waitPopover(page, false);
    }

    // 选中导入 LUT → 触摸点开菜单 → 触摸「色彩空间」打开工作表再关闭
    const firstCard = page.locator('.editor .panel.lut .strip .rc:not([data-lut="none"])').first();
    await firstCard.tap({ timeout: 5_000 });
    await page.waitForSelector('.editor .panel.lut .strip .rc.on:not([data-lut="none"])', { timeout: 10_000 });
    await tile.tap();
    if (await waitPopover(page, true)) {
      await page.locator('.lut-menu-pop:popover-open [data-act="lut-menu-space"]').tap();
      await page.waitForSelector('.lut-space-sheet[data-state="lut-space"]', { timeout: 10_000 });
      res.spaceWorks = true;
      await page.locator('.lut-space-sheet .hd .nb').tap();
      await page.waitForFunction(() => !document.querySelector('.lut-space-sheet'), null, { timeout: 10_000 });
    }

    // 键盘 Enter 打开并聚焦首项，Escape 关闭
    await tile.focus();
    await page.keyboard.press('Enter');
    const kbOpen = await waitPopover(page, true);
    const firstFocused = await page.evaluate(() => document.activeElement?.getAttribute?.('data-act') === 'lut-menu-import');
    await page.keyboard.press('Escape');
    const kbClosed = await waitPopover(page, false);
    res.keyboardWorks = Boolean(kbOpen && firstFocused && kbClosed);
  } finally {
    await ctx.close();
  }
  return res;
}

const out = { error: null, webkitVersion: null, native: null, noInvokers: null };
const browser = await webkit.launch({ headless: true, executablePath });
try {
  out.webkitVersion = browser.version();
  out.native = await runScenario(browser, { disableInvokers: false });
  out.noInvokers = await runScenario(browser, { disableInvokers: true });
} catch (err) {
  const text = String(err && err.stack ? err.stack : err).replace(/\/(?:Volumes|Users|private|var\/folders|tmp)\/\S+/g, '[path]');
  out.error = text.slice(0, 1500);
} finally {
  await browser.close();
}

process.stdout.write(`${JSON.stringify(out)}\n`);
if (out.error) process.exit(1);
