// perf-interact 真实浏览器回归驱动：
// 1) WebGPU 手机编辑器：拖动滑块 0 次同步布局回流；横屏焦点顺序先到工具栏/存到相册再到配方面板，转回竖屏还原；
// 2) 无 WebGPU 手机/iPad：调整滑块呈现禁用样式，单行原因可见且在 390×844 竖屏下不被 .band-body 裁切；
// 3) 桌面端图库与标志弹窗：前 16 张缩略图 loading="eager" + fetchpriority="high"，后续 lazy；
//    非首组 .grid .day 具备 content-visibility: auto；标志重命名 IME 组合态按 Enter 不发 PUT 请求。

import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const [runtime, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const failures = [];
const pageErrors = [];
const summary = {};

function check(ok, msg) {
  if (!ok) failures.push(String(msg));
  return Boolean(ok);
}

async function frame(page, n = 2) {
  await page.evaluate(
    (count) => new Promise((done) => {
      const step = (left) => (left <= 0 ? done() : requestAnimationFrame(() => step(left - 1)));
      step(count);
    }),
    n,
  );
}

// ---------- A. 手机 WebGPU：拖动 0 同步布局 + 手机横屏焦点顺序 ----------
{
  const browser = await launchPinnedChromium({
    args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
  });
  try {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 });
    const page = await ctx.newPage();
    page.on('pageerror', (err) => pageErrors.push(`gpu:${err.message}`));

    await page.goto(`${manifest.mobileUrl}/`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.app[data-screen="start"][data-core="1"]', { timeout: 45_000 });
    await page.locator('input.calibpick').setInputFiles(manifest.pack);
    await page.waitForFunction(() => window.__gxrBook?.pack?.kind === 'user' && !window.__gxrBook.packBusy, null, { timeout: 60_000 });
    await page.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.files);
    await page.waitForFunction(
      () => {
        const sub = document.querySelector('.nav small')?.textContent ?? '';
        return !sub.includes('正在读取') && document.querySelectorAll('.cell').length >= 2;
      },
      null,
      { timeout: 90_000 },
    );

    await page.locator('.cell').first().click();
    await page.waitForFunction(
      () => document.querySelector('.app')?.dataset.gpu === '1' && document.querySelector('.app')?.dataset.frame === '1',
      null,
      { timeout: 45_000 },
    );
    await frame(page, 4);

    // 1) 切到调整，拖动滑块并计数 input 处理期间的同步布局读取
    await page.locator('.toolbar [data-tool="adjust"]').click();
    await page.waitForSelector('.panel.adjust input[type="range"]:not([disabled])', { timeout: 10_000 });
    await frame(page, 3);

    const dragProbe = await page.evaluate(() => {
      const slider = document.querySelector('.panel.adjust input[type="range"]');
      if (!slider) return { ok: false, reason: 'no slider' };
      const rec = { inputs: 0, reads: 0, forced: 0, inInput: false, dirty: false };
      const wrapGetter = (proto, prop) => {
        const d = Object.getOwnPropertyDescriptor(proto, prop);
        if (!d || !d.get) return () => {};
        Object.defineProperty(proto, prop, {
          configurable: true,
          get() {
            if (rec.inInput) {
              rec.reads += 1;
              if (rec.dirty) {
                rec.forced += 1;
                rec.dirty = false;
              }
            }
            return d.get.call(this);
          },
        });
        return () => Object.defineProperty(proto, prop, d);
      };
      const origRect = Element.prototype.getBoundingClientRect;
      Element.prototype.getBoundingClientRect = function (...a) {
        if (rec.inInput) {
          rec.reads += 1;
          if (rec.dirty) {
            rec.forced += 1;
            rec.dirty = false;
          }
        }
        return origRect.apply(this, a);
      };
      const origSetProp = CSSStyleDeclaration.prototype.setProperty;
      CSSStyleDeclaration.prototype.setProperty = function (...a) {
        if (rec.inInput) rec.dirty = true;
        return origSetProp.apply(this, a);
      };
      const restores = [
        wrapGetter(Element.prototype, 'clientWidth'),
        wrapGetter(Element.prototype, 'clientHeight'),
        wrapGetter(HTMLElement.prototype, 'offsetWidth'),
        wrapGetter(HTMLElement.prototype, 'offsetHeight'),
        () => { Element.prototype.getBoundingClientRect = origRect; },
        () => { CSSStyleDeclaration.prototype.setProperty = origSetProp; },
      ];
      const onCap = (e) => {
        if (e.target === slider) {
          rec.inputs += 1;
          rec.inInput = true;
          rec.dirty = false;
        }
      };
      const onBub = (e) => {
        if (e.target === slider) rec.inInput = false;
      };
      document.addEventListener('input', onCap, true);
      document.addEventListener('input', onBub, false);
      try {
        slider.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
        for (let i = 1; i <= 10; i += 1) {
          slider.value = String((i * 0.2).toFixed(1));
          slider.dispatchEvent(new Event('input', { bubbles: true }));
        }
        slider.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
      } finally {
        document.removeEventListener('input', onCap, true);
        document.removeEventListener('input', onBub, false);
        for (const fn of restores) fn();
      }
      return { ok: true, ...rec };
    });
    summary.dragProbe = dragProbe;
    check(dragProbe.ok && dragProbe.inputs === 10, `drag probe expected 10 inputs, got ${JSON.stringify(dragProbe)}`);
    check(dragProbe.reads === 0 && dragProbe.forced === 0, `slider drag triggered synchronous layout reads: ${JSON.stringify(dragProbe)}`);

    // 2) 手机横屏焦点顺序：切回配方页，旋转到 844×390，确认 .toolbar 在 DOM 与 Tab 顺序上先于 .band
    await page.locator('.toolbar [data-tool="recipe"]').click();
    await page.setViewportSize({ width: 844, height: 390 });
    await page.waitForFunction(() => document.querySelector('.app')?.dataset.layout === 'phone-landscape', null, { timeout: 10_000 });
    await frame(page, 3);

    const landscapeOrder = await page.evaluate(() => {
      const screen = document.querySelector('.screen.editor');
      const bar = screen?.querySelector(':scope > .toolbar');
      const band = screen?.querySelector(':scope > .band');
      if (!screen || !bar || !band) return null;
      const children = [...screen.children];
      const barRect = bar.getBoundingClientRect();
      const bandRect = band.getBoundingClientRect();
      const focusables = [...screen.querySelectorAll('button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])')]
        .filter((el) => {
          if (el.closest('[hidden]')) return false;
          const cs = getComputedStyle(el);
          if (cs.display === 'none' || cs.visibility === 'hidden') return false;
          const r = el.getBoundingClientRect();
          return r.width > 0 && r.height > 0;
        });
      const firstBarIdx = focusables.findIndex((el) => bar.contains(el));
      const firstBandIdx = focusables.findIndex((el) => band.contains(el));
      return {
        barChildIdx: children.indexOf(bar),
        bandChildIdx: children.indexOf(band),
        barBottom: Math.round(barRect.bottom),
        bandTop: Math.round(bandRect.top),
        firstBarIdx,
        firstBandIdx,
      };
    });
    summary.landscapeOrder = landscapeOrder;
    check(Boolean(landscapeOrder), 'missing landscape editor elements');
    if (landscapeOrder) {
      check(landscapeOrder.barBottom <= landscapeOrder.bandTop + 2, `toolbar not above band visually: ${JSON.stringify(landscapeOrder)}`);
      check(
        landscapeOrder.barChildIdx < landscapeOrder.bandChildIdx && landscapeOrder.firstBarIdx >= 0 && landscapeOrder.firstBandIdx > landscapeOrder.firstBarIdx,
        `phone landscape focus reaches band before toolbar: ${JSON.stringify(landscapeOrder)}`,
      );
    }

    // 真 Tab 键从顶栏最后一个按钮（⋯）按一次 Tab，焦点必须先落进 .toolbar 而不是 .band
    await page.locator('.editor .nav [data-act="editor-more"]').focus();
    await page.keyboard.press('Tab');
    const tabLandedInBar = await page.evaluate(() => Boolean(document.querySelector('.editor > .toolbar')?.contains(document.activeElement)));
    summary.tabLandedInBar = tabLandedInBar;
    check(tabLandedInBar, 'Tab from nav more button in phone landscape did not land in toolbar');

    // 转回手机竖屏 390×844，.band 恢复在 .toolbar 之前
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForFunction(() => document.querySelector('.app')?.dataset.layout === 'phone-portrait', null, { timeout: 10_000 });
    await frame(page, 3);
    const portraitRestored = await page.evaluate(() => {
      const screen = document.querySelector('.screen.editor');
      const children = screen ? [...screen.children] : [];
      const bar = screen?.querySelector(':scope > .toolbar');
      const band = screen?.querySelector(':scope > .band');
      return Boolean(bar && band && children.indexOf(band) < children.indexOf(bar));
    });
    summary.portraitRestored = portraitRestored;
    check(portraitRestored, 'phone portrait did not restore band before toolbar');

    await ctx.close();
  } finally {
    await browser.close();
  }
}

// ---------- B. 无 WebGPU：调整滑块禁用样式 + 单行说明可见且不裁切 ----------
{
  const browser = await launchPinnedChromium({
    args: ['--disable-webgpu'],
  });
  try {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 });
    const page = await ctx.newPage();
    page.on('pageerror', (err) => pageErrors.push(`nogpu:${err.message}`));
    await page.addInitScript(() => {
      try {
        Object.defineProperty(Navigator.prototype, 'gpu', { configurable: true, get: () => undefined });
      } catch {}
    });

    await page.goto(`${manifest.mobileUrl}/`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.app[data-screen="start"][data-core="1"]', { timeout: 45_000 });
    await page.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.files);
    await page.waitForFunction(
      () => {
        const sub = document.querySelector('.nav small')?.textContent ?? '';
        return !sub.includes('正在读取') && document.querySelectorAll('.cell').length >= 2;
      },
      null,
      { timeout: 90_000 },
    );

    await page.locator('.cell').first().click();
    await page.waitForSelector('.screen.editor', { timeout: 20_000 });
    await page.waitForFunction(() => document.querySelector('.app')?.dataset.settled && document.querySelector('.app')?.dataset.settled !== '0', null, { timeout: 45_000 });
    await page.locator('.toolbar [data-tool="adjust"]').click();
    await frame(page, 3);

    const noGpuAdjust = await page.evaluate(() => {
      const panel = document.querySelector('.panel.adjust');
      const body = document.querySelector('.band-body');
      const input = panel?.querySelector('input[type="range"]');
      const reason = panel?.querySelector('[data-adjust-disabled-reason="1"]');
      if (!panel || !body || !input) return { ok: false };
      const bodyRect = body.getBoundingClientRect();
      const inputCs = getComputedStyle(input);
      const reasonCs = reason ? getComputedStyle(reason) : null;
      const reasonRect = reason ? reason.getBoundingClientRect() : null;
      return {
        ok: true,
        gpuAttr: document.querySelector('.app')?.dataset.gpu ?? null,
        panelDisabled: panel.dataset.disabled ?? null,
        inputDisabled: input.disabled,
        inputCursor: inputCs.cursor,
        inputOpacity: Number(inputCs.opacity),
        reasonFound: Boolean(reason),
        reasonHidden: reason ? reason.hidden : true,
        reasonText: reason ? reason.textContent.trim() : '',
        reasonDisplay: reasonCs?.display ?? null,
        reasonInBody: Boolean(reasonRect && reasonRect.height >= 10 && reasonRect.top >= bodyRect.top - 1 && reasonRect.bottom <= bodyRect.bottom + 1),
      };
    });
    summary.noGpuAdjust = noGpuAdjust;
    check(noGpuAdjust.gpuAttr === '0', `expected no-GPU mode, got ${JSON.stringify(noGpuAdjust)}`);
    check(noGpuAdjust.panelDisabled === '1' && noGpuAdjust.inputDisabled, `adjust panel/input not marked disabled: ${JSON.stringify(noGpuAdjust)}`);
    check(noGpuAdjust.inputCursor === 'not-allowed' && noGpuAdjust.inputOpacity <= 0.4, `disabled slider styling missing: ${JSON.stringify(noGpuAdjust)}`);
    check(
      noGpuAdjust.reasonFound && !noGpuAdjust.reasonHidden && noGpuAdjust.reasonText.length > 0 && noGpuAdjust.reasonInBody,
      `disabled adjust reason missing or clipped in phone portrait: ${JSON.stringify(noGpuAdjust)}`,
    );

    await ctx.close();
  } finally {
    await browser.close();
  }
}

// ---------- C. 桌面端：首屏缩略图 eager/high + content-visibility: auto + 标志重命名 IME ----------
{
  const browser = await launchPinnedChromium({ args: ['--disable-webgpu'] });
  try {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await ctx.newPage();
    page.on('pageerror', (err) => pageErrors.push(`desktop:${err.message}`));

    let logoPutCount = 0;
    page.on('request', (req) => {
      if (req.method() === 'PUT' && req.url().includes('/api/logos/')) logoPutCount += 1;
    });

    await page.goto(manifest.desktopUrl, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#grid-wrap .cell[role="option"] img', { timeout: 30_000 });
    await frame(page, 3);

    // 构造 24 张测试图走真实 gallery.js 渲染（检查前 16 eager+high、后续 lazy，以及非首组 .day 的 content-visibility: auto）
    const desktopGallery = await page.evaluate(async () => {
      const { state } = await import('/assets/state.js');
      const { renderGallery } = await import('/assets/gallery.js');
      const first = state.photos[0];
      if (!first) return { ok: false };
      const synthetic = Array.from({ length: 24 }, (_, i) => ({
        ...first,
        id: `syn_${i}`,
        name: `SYN_${i}`,
        date: i < 12 ? '2026-11-24' : '2026-11-23',
        pair: null,
      }));
      const realFetch = window.fetch.bind(window);
      window.fetch = (input, opts) => {
        const u = new URL(input.url || input, location.href);
        if (u.pathname === '/api/dates') {
          return Promise.resolve(new Response(JSON.stringify([
            { date: '2026-11-24', count: 12 },
            { date: '2026-11-23', count: 12 },
          ]), { headers: { 'Content-Type': 'application/json' } }));
        }
        if (u.pathname === '/api/photos') {
          const off = Number(u.searchParams.get('offset') || 0);
          return Promise.resolve(new Response(JSON.stringify(off === 0 ? synthetic : []), { headers: { 'Content-Type': 'application/json' } }));
        }
        return realFetch(input, opts);
      };
      try {
        await renderGallery(document.getElementById('view'));
        const imgs = [...document.querySelectorAll('#grid-wrap .cell img')];
        const days = [...document.querySelectorAll('#grid-wrap .day')];
        return {
          ok: true,
          count: imgs.length,
          first16EagerHigh: imgs.slice(0, 16).every((im) => im.getAttribute('loading') === 'eager' && im.getAttribute('fetchpriority') === 'high'),
          restLazyNoHigh: imgs.slice(16).every((im) => im.getAttribute('loading') === 'lazy' && !im.hasAttribute('fetchpriority')),
          day0Cv: days[0] ? getComputedStyle(days[0]).contentVisibility : null,
          day1Cv: days[1] ? getComputedStyle(days[1]).contentVisibility : null,
          day1Cis: days[1] ? getComputedStyle(days[1]).containIntrinsicBlockSize : null,
        };
      } finally {
        window.fetch = realFetch;
      }
    });
    summary.desktopGallery = desktopGallery;
    check(desktopGallery.ok && desktopGallery.count === 24, `desktop gallery render failed: ${JSON.stringify(desktopGallery)}`);
    check(desktopGallery.first16EagerHigh, `first 16 thumbs must be loading=eager + fetchpriority=high: ${JSON.stringify(desktopGallery)}`);
    check(desktopGallery.restLazyNoHigh, `thumbs after 16 must stay loading=lazy without fetchpriority: ${JSON.stringify(desktopGallery)}`);
    check(desktopGallery.day0Cv === 'visible' && desktopGallery.day1Cv === 'auto', `off-screen .day must use content-visibility: auto (day0 visible): ${JSON.stringify(desktopGallery)}`);

    // 桌面标志重命名弹窗：IME Enter（isComposing / keyCode 229）绝不触发 PUT，正常 Enter 才提交
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#grid-wrap .cell[role="option"]', { timeout: 30_000 });
    await page.locator('#grid-wrap .cell[role="option"]').first().dblclick();
    await page.locator('.pv > .pv-img, .pv > .pv-gpu').first().waitFor({ state: 'visible', timeout: 20_000 });
    await page.getByRole('tab', { name: '边框' }).or(page.locator('button', { hasText: '边框' }).first()).first().click();
    await page.waitForSelector('.fcard[data-style="leica"]', { timeout: 10_000 });
    await page.locator('.fcard[data-style="leica"]').click();
    await page.waitForSelector('.logo-manage-link', { timeout: 10_000 });
    await page.locator('.logo-manage-link').click();
    const manageDlg = page.locator('#modal-root .dialog');
    await manageDlg.waitFor({ state: 'visible', timeout: 10_000 });
    await manageDlg.locator('.logo-manage-row').getByRole('button', { name: '重命名', exact: true }).click();
    const renameInput = manageDlg.getByLabel('标志名称', { exact: true });
    await renameInput.waitFor({ state: 'visible', timeout: 5_000 });

    logoPutCount = 0;
    await page.evaluate(() => {
      const inp = document.querySelector('#modal-root .dialog .logo-manage-row input');
      if (!inp) return;
      inp.value = '拼音组合中';
      const e1 = new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true, cancelable: true });
      inp.dispatchEvent(e1);
      const e2 = new KeyboardEvent('keydown', { key: 'Enter', isComposing: false, keyCode: 229, bubbles: true, cancelable: true });
      Object.defineProperty(e2, 'keyCode', { value: 229 });
      inp.dispatchEvent(e2);
    });
    await page.waitForTimeout(300);
    const putDuringIme = logoPutCount;
    const stillEditing = await renameInput.isVisible();
    summary.desktopLogoIme = { putDuringIme, stillEditing };
    check(putDuringIme === 0 && stillEditing, `desktop frame_logo_ui submitted during IME composing: ${JSON.stringify(summary.desktopLogoIme)}`);

    // 正常非组合 Enter 必须提交
    if (stillEditing) {
      await renameInput.fill('新名字');
      await renameInput.press('Enter');
      await page.waitForFunction(() => !document.querySelector('#modal-root .dialog .logo-manage-row input'), null, { timeout: 10_000 });
      check(logoPutCount === 1, `desktop frame_logo_ui did not submit on normal Enter: puts=${logoPutCount}`);
    }

    await ctx.close();
  } finally {
    await browser.close();
  }
}

const ok = failures.length === 0 && pageErrors.length === 0;
process.stdout.write(`${JSON.stringify({ ok, failures, pageErrors, summary })}\n`);
process.exit(ok ? 0 : 1);
