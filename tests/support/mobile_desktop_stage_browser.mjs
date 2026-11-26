// 电脑上的舞台（2026-11-26）：鼠标和触控板缩放、平移、看原图、全屏，曲线用鼠标删点，HDR 开关的读屏状态。
// Chromium（Metal，1440×900，没有触摸）量缩放和曲线；WebKit 无触摸量 Safari 的触控板捏合（GestureEvent）；
// WebKit 手机和 iPad（有触摸）确认单击照片仍收起面板、没有新提示、\ 和 F 不起作用。
// 用法：node mobile_desktop_stage_browser.mjs <runtime.mjs> <base url> <manifest.json>
// 隐私：报告只有数字和布尔，不含照片名、拍摄时间或 EXIF。截图只写进 manifest.shotDir。

import { existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtime, base, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, pinnedRuntime, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();
const BROWSER_DIRS = [process.env.PLAYWRIGHT_BROWSERS_PATH, pinnedRuntime.browsersPath].filter(Boolean);
const URL0 = base.endsWith('/') ? base : `${base}/`;
const METAL_ARGS = ['--enable-unsafe-webgpu', '--enable-gpu', '--ignore-gpu-blocklist', '--use-angle=metal'];
const IPAD_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15';
const out = { error: null, pageErrors: [], steps: {} };

function redact(text) {
  return String(text).replace(/\/(?:Volumes|Users|private|var|tmp)\/\S+/g, '[path]').slice(0, 300);
}

function ipadTouch() {
  Object.defineProperty(Navigator.prototype, 'maxTouchPoints', { configurable: true, get: () => 5 });
  Object.defineProperty(Navigator.prototype, 'platform', { configurable: true, get: () => 'MacIntel' });
}

/** 每个 wheel 事件冒到 window 时记下有没有被拦（舞台的处理器先跑）。 */
function wheelLog() {
  window.__wheel = [];
  window.addEventListener('wheel', (e) => window.__wheel.push({ prevented: e.defaultPrevented, ctrl: e.ctrlKey, dy: e.deltaY, mode: e.deltaMode }), { passive: true });
}

async function frames(page, n = 2) {
  for (let i = 0; i < n; i += 1) await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => done(null))));
}

async function openPage(browser, label, options, inits) {
  const ctx = await browser.newContext({ deviceScaleFactor: 2, ...options });
  await ctx.addInitScript(wheelLog);
  for (const init of inits) await ctx.addInitScript(init);
  const page = await ctx.newPage();
  page.on('pageerror', (err) => out.pageErrors.push(`${label}: ${redact(err.message)}`));
  await page.goto(URL0);
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 90_000 });
    const again = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!again) break;
  }
  await page.evaluate(() => localStorage.clear());
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.files);
  await page.waitForFunction(
    (n) => {
      const sub = document.querySelector('.nav small');
      return sub && !sub.textContent.includes('正在读取') && document.querySelectorAll('.cell canvas').length >= n;
    },
    manifest.count,
    { timeout: 120_000 },
  );
  return { ctx, page };
}

async function openCell(page, id) {
  await page.locator(`.cell[data-id="${id}"]`).evaluate((node) => node.click());
  await page.waitForSelector('.screen.editor .stage', { timeout: 120_000 });
  await page.waitForFunction(() => {
    const s = document.querySelector('.app')?.dataset.settled;
    return Boolean(s && s !== '0');
  }, null, { timeout: 180_000 });
  await frames(page, 3);
}

/** 舞台上的照片画布（有显卡时是 canvas.gpu，否则 canvas.still）和舞台状态。 */
async function facts(page) {
  return page.evaluate(() => {
    const stage = document.querySelector('.editor .stage');
    const gpu = stage?.querySelector('canvas.gpu');
    const still = stage?.querySelector('canvas.still');
    const cv = gpu && !gpu.hidden ? gpu : still;
    const r = cv?.getBoundingClientRect();
    const s = stage?.getBoundingClientRect();
    const num = document.querySelector('.editor .nav .t small.num');
    return {
      zoom: Number(stage?.dataset.zoom ?? 'NaN'),
      collapsed: document.querySelector('.screen.editor')?.getAttribute('data-collapsed') ?? null,
      title: stage?.getAttribute('title') ?? '',
      holding: Boolean(num?.classList.contains('holding')),
      sub: num?.textContent ?? '',
      img: r ? { left: r.left, top: r.top, width: r.width, height: r.height } : null,
      stage: s ? { left: s.left, top: s.top, width: s.width, height: s.height } : null,
      wheel: window.__wheel.slice(-1)[0] ?? null,
      wheels: window.__wheel.length,
      dpr: devicePixelRatio,
      touch: navigator.maxTouchPoints,
      fine: matchMedia('(pointer: fine)').matches,
      layout: document.querySelector('.app')?.dataset.layout ?? '',
    };
  });
}

function fracOf(img, x, y) {
  return { u: (x - img.left) / img.width, v: (y - img.top) / img.height };
}

function screenOf(img, f) {
  return { x: img.left + f.u * img.width, y: img.top + f.v * img.height };
}

function drift(img, f, x, y) {
  const p = screenOf(img, f);
  return Math.round(Math.hypot(p.x - x, p.y - y) * 100) / 100;
}

/** 舞台中心偏右上的一点（照片里面，不贴边）。 */
function probe(f) {
  return { x: f.stage.left + f.stage.width * 0.62, y: f.stage.top + f.stage.height * 0.4 };
}

/** 等每次改动后的 data-collapsed（MutationObserver 记下所有变化）。 */
async function watchCollapsed(page) {
  await page.evaluate(() => {
    window.__collapses = [];
    const screen = document.querySelector('.screen.editor');
    window.__collapseObs?.disconnect();
    window.__collapseObs = new MutationObserver(() => window.__collapses.push(screen.getAttribute('data-collapsed')));
    window.__collapseObs.observe(screen, { attributes: true, attributeFilter: ['data-collapsed'] });
  });
}

async function collapses(page) {
  return page.evaluate(() => window.__collapses.slice());
}

async function shot(page, name) {
  if (!manifest.shotDir) return;
  await page.screenshot({ path: path.join(manifest.shotDir, `${name}.png`) });
}

async function chromiumZoom(page) {
  const step = {};
  const f0 = await facts(page);
  step.env = { touch: f0.touch, fine: f0.fine, layout: f0.layout, dpr: f0.dpr, title: f0.title, collapsed: f0.collapsed };
  step.adapter = await page.evaluate(async () => {
    const a = await navigator.gpu?.requestAdapter?.();
    return a ? { vendor: a.info?.vendor ?? '', arch: a.info?.architecture ?? '', fallback: Boolean(a.info?.isFallbackAdapter ?? a.isFallbackAdapter) } : null;
  });
  await shot(page, 'desktop-stage-fit');
  const p = probe(f0);
  await page.mouse.move(p.x, p.y);

  // 1× 时普通滚轮：不缩放、不拦。
  await page.mouse.wheel(0, 120);
  await frames(page);
  const plain = await facts(page);
  step.plainAtFit = { zoom: plain.zoom, prevented: plain.wheel?.prevented ?? null, seen: plain.wheels > f0.wheels };

  // Ctrl + 滚轮（Chrome 的触控板捏合也是这个）：以指针为中心放大，拦下。
  const before = await facts(page);
  const frac = fracOf(before.img, p.x, p.y);
  await page.keyboard.down('Control');
  for (let i = 0; i < 3; i += 1) await page.mouse.wheel(0, -100);
  await page.keyboard.up('Control');
  await frames(page);
  const zoomed = await facts(page);
  step.ctrlWheel = { zoom: zoomed.zoom, prevented: zoomed.wheel?.prevented ?? null, ctrl: zoomed.wheel?.ctrl ?? null, drift: drift(zoomed.img, frac, p.x, p.y) };

  // Ctrl + 滚轮往回：缩小，指针下的点仍不动。
  await page.keyboard.down('Control');
  await page.mouse.wheel(0, 40);
  await page.keyboard.up('Control');
  await frames(page);
  const less = await facts(page);
  step.ctrlWheelOut = { zoom: less.zoom, drift: drift(less.img, frac, p.x, p.y) };

  // 放大后普通滚轮：平移。
  await page.mouse.wheel(0, 60);
  await frames(page);
  const panned = await facts(page);
  // 设备像素比 2 时 Chromium 发出的 deltaY 是 Playwright 给的一半：按页面真正收到的量比。
  step.wheelPan = {
    zoom: panned.zoom,
    dy: Math.round((panned.img.top - less.img.top) * 10) / 10,
    dx: Math.round((panned.img.left - less.img.left) * 10) / 10,
    deltaY: panned.wheel?.mode === 0 ? panned.wheel.dy : null,
    prevented: panned.wheel?.prevented ?? null,
  };

  // 放大后鼠标拖动：平移，不换照片。
  const photoBefore = await page.evaluate(() => document.querySelector('.screen.editor')?.dataset.photo ?? '');
  await page.mouse.move(p.x, p.y);
  await page.mouse.down();
  for (let i = 1; i <= 8; i += 1) await page.mouse.move(p.x - i * 15, p.y - i * 5);
  await page.mouse.up();
  await frames(page);
  const dragged = await facts(page);
  const photoAfter = await page.evaluate(() => document.querySelector('.screen.editor')?.dataset.photo ?? '');
  step.mousePan = { zoom: dragged.zoom, dx: Math.round((dragged.img.left - panned.img.left) * 10) / 10, dy: Math.round((dragged.img.top - panned.img.top) * 10) / 10, samePhoto: photoBefore === photoAfter };

  // 0：回到适合。
  await page.keyboard.press('0');
  await frames(page);
  step.zeroKey = { zoom: (await facts(page)).zoom };

  // 双击：适合 → 100%（一个照片像素对一个设备像素），点击处不动；再双击回到适合。面板都不收起。
  await watchCollapsed(page);
  const fit = await facts(page);
  const q = { x: fit.stage.left + fit.stage.width * 0.4, y: fit.stage.top + fit.stage.height * 0.6 };
  const fracQ = fracOf(fit.img, q.x, q.y);
  await page.mouse.dblclick(q.x, q.y);
  await page.waitForTimeout(600);
  const actual = await facts(page);
  step.dblIn = {
    zoom: actual.zoom,
    devicePxPerImagePx: Math.round(((actual.img.width * actual.dpr) / manifest.imageSize[0]) * 1000) / 1000,
    wantZoom: Math.min(5, manifest.imageSize[0] / (fit.img.width * fit.dpr)),
    drift: drift(actual.img, fracQ, q.x, q.y),
    collapsed: actual.collapsed,
  };
  await shot(page, 'desktop-stage-100');
  await page.mouse.dblclick(q.x, q.y);
  await page.waitForTimeout(600);
  const back = await facts(page);
  step.dblOut = { zoom: back.zoom, collapsed: back.collapsed, collapses: await collapses(page) };

  // + / = / - / 0 缩放；焦点在滑块上时什么都不做。
  const keys = [];
  for (const key of ['+', 'Equal', 'Minus', '0']) {
    await page.keyboard.press(key);
    await frames(page);
    keys.push((await facts(page)).zoom);
  }
  step.keys = keys;
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  const slider = page.locator('.editor input[data-key="exposure_ev"]');
  await slider.waitFor({ state: 'visible', timeout: 10_000 });
  await slider.focus();
  const sliderKeys = [];
  for (const key of ['+', 'Equal', 'Minus']) {
    await page.keyboard.press(key);
    await frames(page);
    sliderKeys.push((await facts(page)).zoom);
  }
  step.sliderKeys = { zooms: sliderKeys, focused: await page.evaluate(() => document.activeElement?.getAttribute('data-key') ?? '') };
  // 不点回「配方」：样本没有相机配方文件，点它会弹出「还差相机配方文件」盖住舞台。
  await page.evaluate(() => document.activeElement?.blur());
  await frames(page);
  return step;
}

async function chromiumClick(page) {
  const step = {};
  await page.keyboard.press('0');
  await watchCollapsed(page);
  const f = await facts(page);
  const c = { x: f.stage.left + f.stage.width / 2, y: f.stage.top + f.stage.height / 2 };
  await page.mouse.click(c.x, c.y);
  await page.waitForTimeout(700);
  step.click = { collapsed: (await facts(page)).collapsed, collapses: await collapses(page) };
  const seq = [];
  for (let i = 0; i < 3; i += 1) {
    await page.keyboard.press('f');
    await frames(page);
    seq.push((await facts(page)).collapsed);
  }
  step.fKey = seq;
  // Esc 由 desktop-keys 接：这里直接调 stage_zoom 导出的方法（开发服务器里和应用是同一个模块）。
  step.leave = await page.evaluate(async () => {
    const mod = await import('/src/app/stage_zoom.ts');
    if (typeof mod.leaveStageFullView !== 'function') return { missing: true };
    const first = mod.leaveStageFullView();
    const collapsed = document.querySelector('.screen.editor')?.getAttribute('data-collapsed');
    const second = mod.leaveStageFullView();
    return { first, collapsed, second };
  });
  return step;
}

async function chromiumCompare(page) {
  const f = await facts(page);
  const step = { title: f.title, restSub: f.sub };
  await page.keyboard.down('Backslash');
  await page.waitForTimeout(150);
  const held = await facts(page);
  step.held = { holding: held.holding, sub: held.sub };
  // 按住不放会连发 keydown：仍是原图。
  await page.keyboard.down('Backslash');
  await page.waitForTimeout(100);
  step.repeat = (await facts(page)).holding;
  await page.keyboard.up('Backslash');
  await page.waitForTimeout(150);
  const after = await facts(page);
  step.released = { holding: after.holding, sub: after.sub };
  // 鼠标长按看原图照旧。
  const c = { x: f.stage.left + f.stage.width / 2, y: f.stage.top + f.stage.height / 2 };
  step.hit = await page.evaluate(({ x, y }) => {
    const n = document.elementFromPoint(x, y);
    window.__downs = [];
    document.addEventListener('pointerdown', (e) => window.__downs.push([e.pointerType, e.pointerId, e.target?.className ?? '']), true);
    return n ? `${n.tagName}.${n.className}` : null;
  }, c);
  await page.mouse.move(c.x, c.y);
  await page.mouse.down();
  await page.waitForTimeout(700);
  step.downs = await page.evaluate(() => window.__downs);
  step.mouseHold = (await facts(page)).holding;
  await page.mouse.up();
  await page.waitForTimeout(150);
  step.mouseHoldReleased = (await facts(page)).holding;
  return step;
}

// ---- 曲线 ----

async function curveFacts(page) {
  return page.evaluate(() => {
    const svg = document.querySelector('.curves-sheet svg.curve');
    const r = svg.getBoundingClientRect();
    const pts = [...svg.querySelectorAll('circle.curve-point')].map((n) => [Number(n.getAttribute('cx')), 255 - Number(n.getAttribute('cy'))]);
    const resetAll = [...document.querySelectorAll('.curves-sheet button')].find((b) => b.textContent === '重置全部曲线');
    return { box: { left: r.left, top: r.top, width: r.width, height: r.height }, points: pts, modelEmpty: Boolean(resetAll?.disabled) };
  });
}

function curveScreen(box, x, y) {
  return { x: box.left + (x / 255) * box.width, y: box.top + ((255 - y) / 255) * box.height };
}

/** 舞台的平均亮度（截图解码后在页面里算），用来看预览有没有跟着曲线变。 */
async function stageMean(page) {
  await page.waitForTimeout(400);
  await frames(page, 3);
  const f = await facts(page);
  const clip = { x: f.img.left + 4, y: f.img.top + 4, width: Math.max(8, f.img.width - 8), height: Math.max(8, f.img.height - 8) };
  const png = await page.screenshot({ clip });
  return page.evaluate(async (b64) => {
    const img = new Image();
    img.src = `data:image/png;base64,${b64}`;
    await img.decode();
    const cv = document.createElement('canvas');
    cv.width = img.width;
    cv.height = img.height;
    const g = cv.getContext('2d');
    g.drawImage(img, 0, 0);
    const d = g.getImageData(0, 0, cv.width, cv.height).data;
    let sum = 0;
    for (let i = 0; i < d.length; i += 4) sum += 0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2];
    return Math.round((sum / (d.length / 4)) * 100) / 100;
  }, png.toString('base64'));
}

async function addPoint(page, x, y) {
  const c = await curveFacts(page);
  const at = curveScreen(c.box, x, y);
  await page.mouse.click(at.x, at.y);
  await frames(page);
  // 等过双击窗口，免得下一次按下被当成第二下。
  await page.waitForTimeout(450);
  return curveFacts(page);
}

async function resetCurves(page) {
  const btn = page.locator('.curves-sheet button', { hasText: '重置全部曲线' });
  if (await btn.isEnabled()) await btn.click();
  await page.waitForFunction(() => document.querySelectorAll('.curves-sheet svg.curve circle.curve-point').length === 2);
  await page.waitForTimeout(450);
}

async function dragTo(page, from, to, steps = 10) {
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  for (let i = 1; i <= steps; i += 1) await page.mouse.move(from.x + ((to.x - from.x) * i) / steps, from.y + ((to.y - from.y) * i) / steps);
  const mid = await page.evaluate(() => Boolean(document.querySelector('.curves-sheet svg.curve circle[data-off]')));
  await page.mouse.up();
  await frames(page);
  await page.waitForTimeout(450);
  return mid;
}

async function chromiumCurves(page) {
  const step = {};
  // 舞台在曲线工作表下面也露着：先量恒等曲线时的亮度。
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  await page.locator('.editor [data-act="open-curves"]').click();
  await page.locator('.curves-sheet svg.curve').waitFor({ state: 'visible', timeout: 10_000 });
  await resetCurves(page);
  step.meanIdentity = await stageMean(page);

  // 1. 加点，鼠标双击删掉。
  const added = await addPoint(page, 128, 200);
  step.meanWithPoint = await stageMean(page);
  const pt = added.points.find((p, i) => i > 0 && i < added.points.length - 1) ?? [128, 200];
  const at = curveScreen(added.box, pt[0], pt[1]);
  await page.mouse.dblclick(at.x, at.y);
  await frames(page);
  await page.waitForTimeout(450);
  const afterDbl = await curveFacts(page);
  step.dblDelete = { before: added.points.length, after: afterDbl.points.length, modelEmpty: afterDbl.modelEmpty, mean: await stageMean(page) };

  // 2. 端点双击删不掉（有中间点时也不行）。
  const again = await addPoint(page, 128, 200);
  for (const [x, y] of [[0, 0], [255, 255]]) {
    const e = curveScreen(again.box, x, y);
    await page.mouse.dblclick(e.x, e.y);
    await frames(page);
    await page.waitForTimeout(450);
  }
  const ends = await curveFacts(page);
  step.endsDbl = { before: again.points.length, after: ends.points.length, first: ends.points[0], last: ends.points[ends.points.length - 1] };

  // 3. 拖出框外 60px 再松手：中间点删掉，模型和预览都回到恒等。
  await resetCurves(page);
  const third = await addPoint(page, 128, 200);
  const mp = third.points.find((p, i) => i > 0 && i < third.points.length - 1) ?? [128, 200];
  const from = curveScreen(third.box, mp[0], mp[1]);
  const offMark = await dragTo(page, from, { x: third.box.left - 60, y: from.y + 20 });
  const afterOff = await curveFacts(page);
  step.dragOff = { before: third.points.length, after: afterOff.points.length, modelEmpty: afterOff.modelEmpty, offMark, mean: await stageMean(page) };

  // 4. 拖出去又拖回框里松手：点留着。
  const fourth = await addPoint(page, 128, 200);
  const fp = fourth.points.find((p, i) => i > 0 && i < fourth.points.length - 1) ?? [128, 200];
  const f4 = curveScreen(fourth.box, fp[0], fp[1]);
  await page.mouse.move(f4.x, f4.y);
  await page.mouse.down();
  for (let i = 1; i <= 6; i += 1) await page.mouse.move(f4.x, f4.y + i * 30);
  await page.mouse.move(f4.x, fourth.box.top + fourth.box.height * 0.6);
  await page.mouse.up();
  await frames(page);
  await page.waitForTimeout(450);
  step.dragBack = { before: fourth.points.length, after: (await curveFacts(page)).points.length };

  // 5. 端点拖出框外：留着。
  const five = await curveFacts(page);
  const last = five.points[five.points.length - 1];
  const e5 = curveScreen(five.box, last[0], last[1]);
  await dragTo(page, e5, { x: five.box.left + five.box.width + 60, y: e5.y - 60 });
  const ends5 = await curveFacts(page);
  step.endDragOff = { before: five.points.length, after: ends5.points.length, lastX: ends5.points[ends5.points.length - 1][0] };

  await resetCurves(page);
  await page.locator('.curves-sheet [data-act="close"]').click();
  await page.waitForFunction(() => !document.querySelector('.curves-sheet'), null, { timeout: 10_000 }).catch(() => {});
  return step;
}

/** HDR 开关：读屏看到的角色、状态和描述（Chromium 无障碍树）。 */
async function hdrAx(page, ctx) {
  const btn = page.locator('.editor .nav [data-act="hdr-toggle"]');
  const attrs = await btn.evaluate((b) => ({
    role: b.getAttribute('role'),
    checked: b.getAttribute('aria-checked'),
    pressed: b.getAttribute('aria-pressed'),
    title: b.getAttribute('title'),
    disabled: b.disabled,
    describedby: b.getAttribute('aria-describedby'),
  }));
  const cdp = await ctx.newCDPSession(page);
  const { nodes } = await cdp.send('Accessibility.getFullAXTree');
  const node = nodes.find((n) => n.name?.value === 'HDR' && ['switch', 'button', 'checkbox'].includes(n.role?.value));
  await cdp.detach();
  const prop = (name) => node?.properties?.find((p) => p.name === name)?.value?.value ?? null;
  return { ...attrs, ax: node ? { role: node.role.value, description: node.description?.value ?? null, checked: prop('checked'), pressed: prop('pressed'), disabled: prop('disabled') } : null };
}

async function chromiumHdr(page, ctx) {
  const step = {};
  step.raw = await hdrAx(page, ctx);
  await page.locator('.editor .nav [data-act="hdr-toggle"]').click();
  step.rawOff = await hdrAx(page, ctx);
  await page.locator('.editor .nav [data-act="hdr-toggle"]').click();
  step.rawOn = await hdrAx(page, ctx);
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
  await openCell(page, manifest.jpgOnlyId);
  step.jpgOnly = await hdrAx(page, ctx);
  return step;
}

async function chromium() {
  const browser = await launchPinnedChromium({ args: METAL_ARGS });
  try {
    const { ctx, page } = await openPage(browser, 'chromium', { viewport: { width: 1440, height: 900 }, hasTouch: false, isMobile: false }, []);
    await openCell(page, manifest.rawId);
    await page.waitForFunction(() => document.querySelector('.app')?.dataset.frame === '1', null, { timeout: 120_000 }).catch(() => {});
    // 单击先量：照片刚打开、还没缩放过。
    out.steps.click = await chromiumClick(page);
    out.steps.zoom = await chromiumZoom(page);
    out.steps.compare = await chromiumCompare(page);
    out.steps.curves = await chromiumCurves(page);
    out.steps.hdr = await chromiumHdr(page, ctx);
    await ctx.close();
  } finally {
    await browser.close();
  }
}

function webkitExecutable(webkit) {
  const direct = webkit.executablePath();
  if (direct && existsSync(direct)) return direct;
  const name = direct ? path.basename(direct) : 'pw_run.sh';
  for (const dir of BROWSER_DIRS) {
    if (!existsSync(dir)) continue;
    for (const entry of readdirSync(dir)) {
      if (!entry.startsWith('webkit-')) continue;
      const candidate = path.join(dir, entry, name);
      if (existsSync(candidate)) return candidate;
    }
  }
  return null;
}

/** Safari 触控板捏合：能造 GestureEvent 就用它，否则用带 scale/clientX/clientY 的普通事件，并记下用了哪种。 */
async function gesture(page, type, x, y, scale) {
  return page.evaluate(({ type, x, y, scale }) => {
    const stage = document.querySelector('.editor .stage');
    let ev = null;
    let kind = 'event';
    try {
      if (typeof GestureEvent === 'function') {
        ev = document.createEvent('GestureEvent');
        ev.initGestureEvent(type, true, true, window, 0, x, y, x, y, false, false, false, false, stage, scale, 0);
        kind = 'GestureEvent';
      }
    } catch {
      ev = null;
    }
    if (!ev || ev.scale !== scale || ev.clientX !== x) {
      ev = new Event(type, { bubbles: true, cancelable: true });
      for (const [k, v] of Object.entries({ scale, clientX: x, clientY: y })) Object.defineProperty(ev, k, { value: v });
      kind = typeof GestureEvent === 'function' ? 'event (GestureEvent not constructible)' : 'event (no GestureEvent)';
    }
    stage.dispatchEvent(ev);
    return { kind, prevented: ev.defaultPrevented };
  }, { type, x, y, scale });
}

/** Safari 把同一次捏合也发成带 ctrlKey 的 wheel（WebKit PlatformWheelEvent::createFromGesture）：和 gesture 事件成对发。 */
async function pairedWheel(page, x, y, dy) {
  return page.evaluate(({ x, y, dy }) => {
    const stage = document.querySelector('.editor .stage');
    const ev = new WheelEvent('wheel', { bubbles: true, cancelable: true, ctrlKey: true, clientX: x, clientY: y, deltaY: dy, deltaMode: 0 });
    stage.dispatchEvent(ev);
    return ev.defaultPrevented;
  }, { x, y, dy });
}

async function webkitDesktop(browser) {
  const { ctx, page } = await openPage(browser, 'webkitDesktop', { viewport: { width: 1440, height: 900 }, hasTouch: false, isMobile: false }, []);
  const step = {};
  await openCell(page, manifest.rawId);
  const f = await facts(page);
  step.env = { touch: f.touch, fine: f.fine, title: f.title };
  const p = probe(f);
  const frac = fracOf(f.img, p.x, p.y);
  const g = [];
  const w = [];
  g.push(await gesture(page, 'gesturestart', p.x, p.y, 1));
  g.push(await gesture(page, 'gesturechange', p.x, p.y, 1.5));
  w.push(await pairedWheel(page, p.x, p.y, -20));
  g.push(await gesture(page, 'gesturechange', p.x, p.y, 2));
  w.push(await pairedWheel(page, p.x, p.y, -20));
  g.push(await gesture(page, 'gestureend', p.x, p.y, 2));
  w.push(await pairedWheel(page, p.x, p.y, -20));
  await frames(page);
  const z = await facts(page);
  step.gesture = { kind: g[1].kind, prevented: g.map((r) => r.prevented), pairedWheelPrevented: w, zoom: z.zoom, drift: drift(z.img, frac, p.x, p.y) };
  await page.keyboard.press('0');
  await watchCollapsed(page);
  await page.mouse.click(p.x, p.y);
  await page.waitForTimeout(700);
  step.click = { collapsed: (await facts(page)).collapsed, collapses: await collapses(page) };
  await ctx.close();
  return step;
}

/** 有触摸的 WebKit（手机、iPad）：单击照片仍收起面板；没有新提示；\ 和 F 不起作用。 */
async function webkitTouch(browser, options, inits) {
  const { ctx, page } = await openPage(browser, 'webkitTouch', { ...options, hasTouch: true, isMobile: true }, inits);
  const step = {};
  await openCell(page, manifest.rawId);
  const f = await facts(page);
  step.env = { touch: f.touch, fine: f.fine, layout: f.layout, title: f.title, collapsed: f.collapsed };
  await page.keyboard.down('Backslash');
  await page.waitForTimeout(150);
  step.backslashHolding = (await facts(page)).holding;
  await page.keyboard.up('Backslash');
  await page.keyboard.press('f');
  await frames(page);
  step.fCollapsed = (await facts(page)).collapsed;
  const c = { x: f.stage.left + f.stage.width / 2, y: f.stage.top + f.stage.height / 2 };
  await page.touchscreen.tap(c.x, c.y);
  await page.waitForTimeout(800);
  step.tapCollapsed = (await facts(page)).collapsed;
  await page.touchscreen.tap(c.x, c.y);
  await page.waitForTimeout(800);
  step.tapAgain = (await facts(page)).collapsed;
  await ctx.close();
  return step;
}

async function webkit() {
  const { webkit: wk } = await import(pathToFileURL(pinnedRuntime.modulePath).href);
  const executablePath = webkitExecutable(wk);
  if (!executablePath) {
    out.steps.webkitMissing = true;
    return;
  }
  const browser = await wk.launch({ headless: true, executablePath });
  try {
    out.steps.webkitDesktop = await webkitDesktop(browser);
    out.steps.webkitPhone = await webkitTouch(browser, { viewport: { width: 390, height: 844 } }, []);
    out.steps.webkitIpad = await webkitTouch(browser, { viewport: { width: 1032, height: 1376 }, userAgent: IPAD_UA }, [ipadTouch]);
  } finally {
    await browser.close();
  }
}

try {
  if (manifest.part !== 'webkit') await chromium();
  if (manifest.part !== 'chromium') await webkit();
} catch (err) {
  out.error = redact(String(err?.message ?? err).split('\n')[0]);
}
writeFileSync(manifest.reportPath, JSON.stringify(out));
process.stdout.write(`${JSON.stringify({ done: true, error: out.error !== null })}\n`);
