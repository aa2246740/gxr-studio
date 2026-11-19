// 手机曲线拖动：手指按屏幕像素走，点必须跟着走。
// 用法：node mobile_ui_curve_drag_browser.mjs <runtime.mjs> <base url> <manifest.json>
// stdout 只有一行 JSON，不含照片名、拍摄时间或 EXIF。

import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';

const [runtime, base, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const VIEWPORTS = [
  { width: 390, height: 844 },
  { width: 430, height: 932 },
];
const BOX_SLOP = 1;
const VALUE_SLOP = 3;
const OFFSET_SLOP = 2;

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const failures = [];
const runs = [];
const pointerTypes = new Set();

function redact(text) {
  return String(text).replace(/\/(?:Volumes|Users|private|tmp)\/\S+/g, '[path]').slice(0, 220);
}

function check(ok, message) {
  if (!ok) failures.push(redact(message));
}

function num(value) {
  return Math.round(value * 10) / 10;
}

function screenOf(box, x, y) {
  return {
    x: box.left + (x / 255) * box.width,
    y: box.top + ((255 - y) / 255) * box.height,
  };
}

function pointerCurve(box, pointer) {
  return {
    x: ((pointer.x - box.left) / box.width) * 255,
    y: 255 - ((pointer.y - box.top) / box.height) * 255,
  };
}

function mid(points) {
  return points.find((point) => point.i > 0 && point.i < points.length - 1) ?? null;
}

function boxShift(a, b) {
  return Math.max(Math.abs(a.left - b.left), Math.abs(a.top - b.top), Math.abs(a.width - b.width), Math.abs(a.height - b.height));
}

async function frame(page) {
  await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
}

async function readCurve(page) {
  return page.evaluate(() => {
    const svg = document.querySelector('.editor svg.curve');
    const box = svg.getBoundingClientRect();
    const points = [...svg.querySelectorAll('circle.curve-point')].map((node) => ({
      i: Number(node.getAttribute('data-i')),
      x: Number(node.getAttribute('cx')),
      y: 255 - Number(node.getAttribute('cy')),
    }));
    return {
      drag: document.querySelector('.app')?.getAttribute('data-drag') ?? '',
      box: { left: box.left, top: box.top, width: box.width, height: box.height },
      points,
    };
  });
}

async function touch(session, type, x, y) {
  await session.send('Input.dispatchTouchEvent', {
    type,
    touchPoints: type === 'touchEnd' || type === 'touchCancel' ? [] : [{ x, y, id: 1 }],
  });
}

async function stroke(page, session, from, to, steps) {
  const before = await readCurve(page);
  const samples = [];
  await touch(session, 'touchStart', from.x, from.y);
  await frame(page);
  samples.push({ ...(await readCurve(page)), pointer: { x: from.x, y: from.y }, phase: 'down' });
  for (let i = 1; i <= steps; i += 1) {
    const pointer = {
      x: from.x + ((to.x - from.x) * i) / steps,
      y: from.y + ((to.y - from.y) * i) / steps,
    };
    await touch(session, 'touchMove', pointer.x, pointer.y);
    await frame(page);
    samples.push({ ...(await readCurve(page)), pointer, phase: 'move' });
  }
  await touch(session, 'touchEnd');
  await frame(page);
  const after = await readCurve(page);
  return { before, samples, after };
}

function moves(strokeResult) {
  return strokeResult.samples.filter((sample) => sample.phase === 'move');
}

function assertBoxStill(label, before, samples) {
  let worst = 0;
  for (const sample of samples) worst = Math.max(worst, boxShift(before.box, sample.box));
  check(worst <= BOX_SLOP, `${label}: curve box moved ${num(worst)}px during the drag`);
  return num(worst);
}

function assertCommitted(label, after, pointer) {
  const point = mid(after.points);
  const mapped = pointerCurve(after.box, pointer);
  if (!point || !(after.box.width > 0)) {
    check(false, `${label}: missing point after release`);
    return null;
  }
  const dx = Math.abs(point.x - mapped.x);
  const dy = Math.abs(point.y - mapped.y);
  check(dx <= VALUE_SLOP && dy <= VALUE_SLOP, `${label}: after release point (${point.x},${point.y}) is ${num(dx)},${num(dy)} curve units from the pointer`);
  return num(Math.max(dx, dy));
}

function assertTracks(label, samples) {
  const deltas = [];
  for (const sample of samples) {
    const point = mid(sample.points);
    const mapped = pointerCurve(sample.box, sample.pointer);
    if (!point || !(sample.box.width > 0)) {
      check(false, `${label}: missing point while tracking`);
      return deltas;
    }
    const dx = Math.abs(point.x - mapped.x);
    const dy = Math.abs(point.y - mapped.y);
    deltas.push(num(Math.max(dx, dy)));
    check(dx <= VALUE_SLOP && dy <= VALUE_SLOP, `${label}: point (${point.x},${point.y}) is ${num(dx)},${num(dy)} curve units from the pointer`);
  }
  return deltas;
}

function assertMonotonic(label, startY, samples, direction) {
  const ys = [startY, ...samples.map((sample) => mid(sample.points)?.y)];
  for (let i = 1; i < ys.length; i += 1) {
    const ok = direction < 0 ? ys[i] < ys[i - 1] : ys[i] > ys[i - 1];
    check(ok, `${label}: curve y ${ys.join(',')} is not monotonic ${direction < 0 ? 'down' : 'up'}`);
    if (!ok) break;
  }
  return ys;
}

async function resetCurve(page) {
  const reset = page.getByRole('button', { name: '重置全部曲线', exact: true });
  if (await reset.isEnabled()) await reset.click();
  await page.waitForFunction(() => document.querySelectorAll('.editor svg.curve circle.curve-point').length === 2);
}

async function placePoint(page, session) {
  await page.locator('.editor svg.curve').scrollIntoViewIfNeeded();
  await frame(page);
  const laid = await readCurve(page);
  check(laid.box.width > 40 && laid.box.height > 40, 'curve box has no size');
  const at = screenOf(laid.box, 128, 140);
  const placed = await stroke(page, session, at, at, 0);
  const point = mid(placed.after.points);
  check(
    placed.after.points.length === 3 && !!point && Math.abs(point.x - 128) <= 1 && Math.abs(point.y - 140) <= 1,
    `add point landed on ${point ? `${point.x},${point.y}` : 'nothing'}`,
  );
  return point ? placed.after : null;
}

async function openCurves(page) {
  const sheet = page.locator('.curves-sheet');
  if ((await sheet.count()) && (await sheet.isVisible())) return;
  await page.locator('.editor .toolbar [data-tool="more"]').click();
  await page.locator('.more-sheet [data-more="curves"]').click();
  await page.waitForSelector('.curves-sheet svg.curve');
}

async function runViewport(page, session, viewport) {
  const label = `${viewport.width}x${viewport.height}`;
  await page.setViewportSize(viewport);
  await frame(page);
  await openCurves(page);
  await page.locator('.editor .curve-btn[data-channel="master"]').click();
  await page.waitForSelector('.editor svg.curve[data-channel="master"]');
  const pressed = await page.locator('.editor .curve-btn[data-channel="master"]').getAttribute('aria-pressed');
  check(pressed === 'true', `${label}: master channel is not selected`);
  await resetCurve(page);

  const summary = { viewport: label };

  const downFrom = await placePoint(page, session);
  const downPoint = downFrom ? mid(downFrom.points) : null;
  check(!!downPoint, `${label} down: no point to grab`);
  const downOrigin = downPoint ? screenOf(downFrom.box, downPoint.x, downPoint.y) : { x: 0, y: 0 };
  const down = await stroke(page, session, downOrigin, { x: downOrigin.x, y: downOrigin.y + 80 }, 10);
  const downMoves = moves(down);
  summary.down = {
    ys: assertMonotonic(`${label} down`, downPoint ? downPoint.y : 0, downMoves, -1),
    boxShift: assertBoxStill(`${label} down`, down.before, down.samples),
    endDelta: assertCommitted(`${label} down`, down.after, { x: downOrigin.x, y: downOrigin.y + 80 }),
  };
  assertTracks(`${label} down`, downMoves);

  await resetCurve(page);
  const upFrom = await placePoint(page, session);
  const upPoint = upFrom ? mid(upFrom.points) : null;
  check(!!upPoint, `${label} up: no point to grab`);
  const upOrigin = upPoint ? screenOf(upFrom.box, upPoint.x, upPoint.y) : { x: 0, y: 0 };
  const up = await stroke(page, session, upOrigin, { x: upOrigin.x, y: upOrigin.y - 80 }, 10);
  const upMoves = moves(up);
  summary.up = {
    ys: assertMonotonic(`${label} up`, upPoint ? upPoint.y : 0, upMoves, 1),
    boxShift: assertBoxStill(`${label} up`, up.before, up.samples),
    endDelta: assertCommitted(`${label} up`, up.after, { x: upOrigin.x, y: upOrigin.y - 80 }),
  };
  assertTracks(`${label} up`, upMoves);

  await resetCurve(page);
  const holdFrom = await placePoint(page, session);
  const heldPoint = holdFrom ? mid(holdFrom.points) : null;
  check(!!heldPoint, `${label} offset: no point to grab`);
  const holdPoint = heldPoint ? screenOf(holdFrom.box, heldPoint.x, heldPoint.y) : { x: 0, y: 0 };
  const holdOrigin = { x: holdPoint.x, y: holdPoint.y + 15 };
  const held = await stroke(page, session, holdOrigin, { x: holdOrigin.x, y: holdOrigin.y + 60 }, 8);
  const offsets = [];
  for (const sample of moves(held)) {
    const point = mid(sample.points);
    if (!point) {
      check(false, `${label} offset: missing point while tracking`);
      break;
    }
    const screenY = sample.box.top + ((255 - point.y) / 255) * sample.box.height;
    const offset = sample.pointer.y - screenY;
    offsets.push(num(offset));
    check(Math.abs(offset - 15) <= OFFSET_SLOP, `${label} offset: grab offset is ${num(offset)}px, want 15`);
  }
  summary.offset = { offsets, boxShift: assertBoxStill(`${label} offset`, held.before, held.samples) };

  await resetCurve(page);
  const lowFrom = await placePoint(page, session);
  const lowPoint = lowFrom ? mid(lowFrom.points) : null;
  const lowOrigin = lowPoint ? screenOf(lowFrom.box, lowPoint.x, lowPoint.y) : { x: 0, y: 0 };
  const low = await stroke(page, session, lowOrigin, { x: lowOrigin.x, y: lowOrigin.y + (lowFrom ? lowFrom.box.height : 0) + 160 }, 8);
  summary.bottom = { y: mid(low.after.points)?.y ?? null, boxShift: assertBoxStill(`${label} bottom`, low.before, low.samples) };
  check(summary.bottom.y === 0, `${label} bottom: point y is ${summary.bottom.y}, want 0`);

  await resetCurve(page);
  const highFrom = await placePoint(page, session);
  const highPoint = highFrom ? mid(highFrom.points) : null;
  const highOrigin = highPoint ? screenOf(highFrom.box, highPoint.x, highPoint.y) : { x: 0, y: 0 };
  const high = await stroke(page, session, highOrigin, { x: highOrigin.x, y: highOrigin.y - (highFrom ? highFrom.box.height : 0) - 160 }, 8);
  summary.top = { y: mid(high.after.points)?.y ?? null, boxShift: assertBoxStill(`${label} top`, high.before, high.samples) };
  check(summary.top.y === 255, `${label} top: point y is ${summary.top.y}, want 255`);

  // 离线按下：桌面 curve_editor.js 在离现有点超过 14 个曲线单位时插入新点。
  // 新点没有抓取偏移，拖动时跟着指针（端点锁横坐标，中间点夹在相邻点之间）。
  // 手机热区大约 22px，按下位置也要在这个热区外，才和桌面是同一种手势。
  await resetCurve(page);
  await page.locator('.editor svg.curve').scrollIntoViewIfNeeded();
  await frame(page);
  const emptyFrom = await readCurve(page);
  const emptyStart = screenOf(emptyFrom.box, 40, 200);
  const emptyEnd = { x: emptyStart.x + 36, y: emptyStart.y + 48 };
  const empty = await stroke(page, session, emptyStart, emptyEnd, 8);
  const emptyMoves = moves(empty);
  const emptyPoint = mid(empty.after.points);
  const emptyMapped = pointerCurve(empty.after.box, emptyEnd);
  summary.empty = {
    count: empty.after.points.length,
    point: emptyPoint ? { x: emptyPoint.x, y: emptyPoint.y } : null,
    pointer: { x: num(emptyMapped.x), y: num(emptyMapped.y) },
    boxShift: assertBoxStill(`${label} empty`, empty.before, empty.samples),
    endDelta: assertCommitted(`${label} empty`, empty.after, emptyEnd),
  };
  assertTracks(`${label} empty`, emptyMoves);
  check(summary.empty.count === 3, `${label} empty: point count is ${summary.empty.count}, want 3`);

  runs.push(summary);
}

async function dragAttr(page) {
  return page.evaluate(() => document.querySelector('.app')?.getAttribute('data-drag') ?? '');
}

/** 拖到一半关掉「曲线」工作表，再在别处松手。草稿必须丢掉，之后滑块仍能进入 03d。 */
async function runInterrupted(page, session) {
  await page.setViewportSize(VIEWPORTS[0]);
  await frame(page);
  await openCurves(page);
  await page.locator('.editor .curve-btn[data-channel="master"]').click();
  await resetCurve(page);
  await page.locator('.editor svg.curve').scrollIntoViewIfNeeded();
  await frame(page);
  const laid = await readCurve(page);
  const at = screenOf(laid.box, 128, 140);
  await touch(session, 'touchStart', at.x, at.y);
  await frame(page);
  await touch(session, 'touchMove', at.x, at.y + 48);
  await frame(page);
  const midDrag = await readCurve(page);
  check(mid(midDrag.points) != null, 'interrupt: drag did not leave the identity line');
  await page.locator('.curves-sheet [data-act="close"]').evaluate((node) => node.click());
  await touch(session, 'touchEnd', 24, 80);
  await frame(page);
  const drag = await dragAttr(page);
  await openCurves(page);
  const after = await readCurve(page);
  await page.locator('.curves-sheet [data-act="close"]').click();
  check(after.points.length === 2, `interrupt: curve kept ${after.points.length} points after leaving the tab`);
  check(drag === '0', `interrupt: data-drag is ${JSON.stringify(drag)} after release`);
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  const slider = page.locator('input[data-key="exposure_ev"]');
  await slider.scrollIntoViewIfNeeded();
  const box = await slider.boundingBox();
  check(box != null && box.width > 0, 'interrupt: exposure slider has no box');
  let sliderDrag = '';
  if (box && box.width > 0) {
    const x = box.x + box.width / 2;
    const y = box.y + box.height / 2;
    await touch(session, 'touchStart', x, y);
    await frame(page);
    sliderDrag = await dragAttr(page);
    check(sliderDrag === '1', `interrupt: slider drag left data-drag ${JSON.stringify(sliderDrag)}`);
    await touch(session, 'touchEnd', x, y);
    await frame(page);
    const released = await dragAttr(page);
    check(released === '0', `interrupt: slider release left data-drag ${JSON.stringify(released)}`);
  }
  return { points: after.points.length, drag, sliderDrag };
}

const out = { ok: false, pointerTypes: [], failures, runs, interrupt: null };
let exitCode = 1;
try {
  const context = await browser.newContext({ viewport: VIEWPORTS[0], deviceScaleFactor: 2, hasTouch: true });
  const page = await context.newPage();
  const pageErrors = [];
  const notes = [];
  page.on('pageerror', (error) => pageErrors.push(redact(error.message)));
  page.on('console', (msg) => {
    if (msg.type() === 'error') notes.push(redact(msg.text()));
  });
  page.on('response', (res) => {
    if (res.status() < 400) return;
    const path = new URL(res.url()).pathname;
    notes.push(`${res.status()} ${path}`);
    void res.text().then(
      (body) => notes.push(redact(body).slice(0, 240)),
      () => undefined,
    );
  });
  await page.addInitScript(() => {
    window.__curvePointers = [];
    document.addEventListener(
      'pointerdown',
      (event) => {
        if (event.target instanceof Element && event.target.closest('svg.curve')) window.__curvePointers.push(event.pointerType);
      },
      true,
    );
  });
  await page.goto(base.endsWith('/') ? base : `${base}/`);
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      await page.waitForSelector('.app[data-core="1"]', { timeout: 90_000 });
    } catch (error) {
      await new Promise((done) => setTimeout(done, 400));
      const snap = await page
        .evaluate(() => ({
          ready: document.readyState,
          core: document.querySelector('.app')?.dataset.core ?? '',
          gpu: document.querySelector('.app')?.dataset.gpu ?? '',
          text: (document.body?.innerText || '').replace(/\s+/g, ' ').slice(0, 160),
        }))
        .catch(() => ({ ready: 'gone' }));
      throw new Error(`core not ready ${JSON.stringify(snap)} errors=${pageErrors.join(' | ')} notes=${notes.slice(0, 8).join(' | ')}`);
    }
    const again = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!again) break;
  }
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.pairs);
  await page.waitForFunction(() => {
    const sub = document.querySelector('.nav small');
    return sub && !sub.textContent.includes('正在读取') && document.querySelector('.cell canvas');
  }, null, { timeout: 120_000 });
  await page.locator('.cell').first().evaluate((node) => node.click());
  await page.waitForSelector('.screen.editor', { timeout: 120_000 });
  await page.waitForFunction(() => {
    const settled = document.querySelector('.app')?.dataset.settled;
    return Boolean(settled && settled !== '0');
  }, null, { timeout: 180_000 });
  const session = await context.newCDPSession(page);
  for (const viewport of VIEWPORTS) await runViewport(page, session, viewport);
  out.interrupt = await runInterrupted(page, session);
  for (const kind of await page.evaluate(() => window.__curvePointers || [])) pointerTypes.add(kind);
  check(pageErrors.length === 0, `page errors: ${pageErrors.join(' | ') || '0'}`);
  check(pointerTypes.has('touch'), `pointer type was ${[...pointerTypes].join(',') || 'missing'}`);
  out.pointerTypes = [...pointerTypes];
  out.ok = failures.length === 0;
  exitCode = out.ok ? 0 : 1;
  await context.close();
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  failures.push(redact(message.split('\n')[0]));
  out.pointerTypes = [...pointerTypes];
  out.ok = false;
  exitCode = 1;
} finally {
  await browser.close();
}

console.log(JSON.stringify(out));
process.exit(exitCode);
