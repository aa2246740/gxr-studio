// Run against an isolated real-photo instance. Images and reports stay in the supplied evidence folder.
// GXR_STUDIO_TOKEN=... node scripts/verify_output_ratio.mjs <port> <photo-id> <evidence-folder> [pinned-runtime.mjs]
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { homedir, loadavg, availableParallelism } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [port, id, folder, runtime = path.join(homedir(), '.codex/playwright-runtime/runtime.mjs')] = process.argv.slice(2);
assert(port && id && folder, 'Provide port, photo id and a private evidence directory');
assert(![60887, 18772, 18801, 18802, 18803].includes(Number(port)), 'Protected port');
await mkdir(folder, { recursive: true });
const { launchPinnedChromium } = await import(pathToFileURL(runtime).href);
const browser = await launchPinnedChromium();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
const requests = [];
const errors = [];
const httpFailures = [];
page.on('request', (r) => requests.push({ method: r.method(), path: new URL(r.url()).pathname }));
page.on('pageerror', (e) => errors.push(e.message));
page.on('response', (r) => { if (r.status() >= 400) httpFailures.push({ status: r.status(), path: new URL(r.url()).pathname }); });
const report = { browser: browser.version(), machine: { cores: availableParallelism(), load: loadavg() }, clicks: [], outputs: [], tiers: [], errors, httpFailures };
const styles = [['gallery', '白边'], ['leica', '徕卡水印'], ['hasselblad', '哈苏水印'], ['film', '胶片'], ['instant', '拍立得']];

async function call(route, body, method = 'POST') {
  return page.evaluate(async ({ route, body, method }) => {
    const { api } = await import('/assets/api.js');
    return api(route, { method, body });
  }, { route, body, method });
}

async function flush() {
  await page.evaluate(async (id) => (await import('/assets/saves.js')).flushPhotos([id]), id);
}

async function openFrame() {
  await page.reload();
  await page.waitForFunction(async () => {
    const v = (await import('/assets/viewer.js')).currentView();
    const { state } = await import('/assets/state.js');
    return v?.ed && v.crop && state.camdata !== null && document.querySelector('#tab-frame');
  });
  await page.locator('#tab-frame').click();
  await page.waitForFunction(() => /成品 \d/.test(document.querySelector('.frame-size')?.textContent || '')
    && document.querySelectorAll('.fcard canvas').length === 7 && document.querySelector('.pv-deco'));
  await page.evaluate(async () => {
    const fonts = await import('/assets/finish_fonts.js');
    await Promise.all(['atkinson', 'inter', 'courier', 'patrick', 'dseg'].map(fonts.loadFont));
  });
}

async function clickFrame(name) {
  const marker = requests.length;
  const result = await page.evaluate(async (name) => {
    const button = [...document.querySelectorAll('.fcard')].find((b) => b.textContent.trim() === name);
    if (!button) throw new Error(`Missing frame ${name}`);
    const before = document.querySelector('.pv-deco')?.dataset.key;
    const started = performance.now();
    button.click();
    const immediate = document.querySelector('.frame-size')?.textContent;
    await new Promise(requestAnimationFrame);
    const ms = performance.now() - started;
    const v = (await import('/assets/viewer.js')).currentView();
    const box = document.querySelector('.pv.framed').getBoundingClientRect();
    const card = document.querySelector('.fcard[aria-pressed="true"] canvas');
    return { name, ms, immediate, changed: before !== document.querySelector('.pv-deco')?.dataset.key,
      width: box.width, height: box.height, cardSize: [card.width, card.height], style: v.ed.frame.style, gpu: !!v.gpuActive };
  }, name);
  const network = requests.slice(marker);
  result.renders = network.filter((r) => r.path.endsWith('/render')).length;
  result.sizeQueries = network.filter((r) => r.path.endsWith('/output-size')).length;
  assert(result.changed, `Frame did not change on the next animation frame: ${name}`);
  assert.match(result.immediate, /成品 \d+ × \d+ 像素/);
  assert.equal(result.renders, 0, 'Frame change requested a server render');
  assert.equal(result.sizeQueries, 0, 'Frame change requested another size query');
  report.clicks.push(result);
  return result;
}

async function readJpegSize(filename) {
  const bytes = await readFile(filename);
  assert.equal(bytes.readUInt16BE(0), 0xffd8);
  let i = 2;
  while (i < bytes.length) {
    assert.equal(bytes[i++], 0xff);
    while (bytes[i] === 0xff) i++;
    const marker = bytes[i++];
    const length = bytes.readUInt16BE(i);
    if ([0xc0, 0xc1, 0xc2].includes(marker)) return [bytes.readUInt16BE(i + 5), bytes.readUInt16BE(i + 3)];
    i += length;
  }
  throw new Error(`No JPEG size in ${filename}`);
}

async function clickOption(group, name) {
  const marker = requests.length;
  const click = await page.evaluate(async ({ group, name }) => {
    const holder = [...document.querySelectorAll('[role="group"]')].find((node) => node.getAttribute('aria-label') === group);
    const button = [...holder.querySelectorAll('button')].find((node) => node.textContent.trim() === name);
    const before = document.querySelector('.pv-deco')?.dataset.key;
    const started = performance.now();
    button.click();
    const immediate = document.querySelector('.frame-size')?.textContent;
    await new Promise(requestAnimationFrame);
    return { name: `${group} ${name}`, ms: performance.now() - started, immediate, changed: before !== document.querySelector('.pv-deco')?.dataset.key };
  }, { group, name });
  assert(click.changed, 'Frame option did not change the stage');
  assert.match(click.immediate, /成品 \d+ × \d+ 像素/);
  click.renders = requests.slice(marker).filter((r) => r.path.endsWith('/render')).length;
  click.sizeQueries = requests.slice(marker).filter((r) => r.path.endsWith('/output-size')).length;
  assert.equal(click.renders, 0);
  assert.equal(click.sizeQueries, 0);
  report.clicks.push(click);
}

async function exported(tier = 'original') {
  await flush();
  const preflight = await call(`/api/photos/${id}/output-size`, {});
  const out = await call(`/api/photos/${id}/export`, { size: tier, quality_tier: 'high', hdr: false });
  const actual = await readJpegSize(out.path);
  assert.deepEqual(actual, preflight.sizes[tier]);
  assert.deepEqual(actual, [out.width, out.height]);
  const stage = await page.evaluate(async () => {
    const v = (await import('/assets/viewer.js')).currentView();
    return [v.decor.deco.width, v.decor.deco.height];
  });
  assert.deepEqual(stage, preflight.sizes.original, 'Stage layout dimensions differ from export');
  return { size: actual, path: out.path, preflight };
}

try {
  const url = new URL(`http://127.0.0.1:${port}/`);
  url.searchParams.set('token', process.env.GXR_STUDIO_TOKEN || '');
  url.hash = `/photo/${id}`;
  await page.goto(url.href);
  await page.waitForSelector('#tab-frame');
  await call('/api/prefs', { export_hdr: false }, 'PUT');
  for (const [preset, orientation, rw, rh] of [['4:3', 'portrait', 3, 4], ['xpan-65-24', 'landscape', 65, 24], ['16:9', 'portrait', 9, 16]]) {
    await flush();
    await call(`/api/photos/${id}/edit`, { recipe: null, lut: null, geometry: {}, frame: { style: 'gallery' }, stamp: { enabled: true } }, 'PUT');
    await call(`/api/photos/${id}/crop`, { preset, orientation }, 'PUT');
    await openFrame();
    for (const name of ['细', '粗', '中']) await clickOption('宽度', name);
    await clickOption('成品比例', '边框原样');
    await clickOption('成品比例', `跟裁剪 ${rw}:${rh}`);
    // Five interleaved rounds per style. The timed region includes the click and next rendered frame.
    for (let round = 0; round < 5; round++) {
      for (const [style, name] of [...styles.slice(1), styles[0]]) {
        const click = await clickFrame(name);
        assert.equal(click.style, style);
        assert(Math.abs(click.width * rh - click.height * rw) <= rw + rh, 'Stage aspect differs from export');
        assert(Math.abs(click.cardSize[0] * rh - click.cardSize[1] * rw) <= rw + rh, 'Frame card aspect differs from export');
      }
    }
    for (const [style, name] of styles) {
      if (style !== 'gallery') await clickFrame(name);
      const after = await exported();
      assert.equal(after.size[0] * rh, after.size[1] * rw);
      await page.screenshot({ path: path.join(folder, `${preset}-${style}.png`) });
      await page.getByRole('group', { name: '成品比例', exact: true }).getByRole('button', { name: '边框原样', exact: true }).click();
      const before = await exported();
      report.outputs.push({ preset, style, before: before.size, after: after.size, path: after.path });
      await page.getByRole('group', { name: '成品比例', exact: true }).getByRole('button', { name: /^跟裁剪/ }).click();
    }
    if (preset === '4:3') {
      await clickFrame('画布');
      const standard = await exported();
      report.outputs.push({ preset, style: 'canvas', before: standard.size, after: standard.size, path: standard.path });
      await clickOption('画布比例', '3:4');
      const social = await exported();
      assert.equal(social.size[0] * 4, social.size[1] * 3);
      report.outputs.push({ preset, style: 'canvas-3:4', after: social.size, path: social.path });
      await page.screenshot({ path: path.join(folder, '3-by-4-canvas.png') });
    }
  }
  await clickFrame('画布');
  for (const [ratio, rw, rh] of [['3:4', 3, 4], ['2:3', 2, 3]]) {
    await clickOption('画布比例', ratio);
    const out = await exported();
    assert.equal(out.size[0] * rh, out.size[1] * rw);
    report.outputs.push({ preset: '16:9', style: `canvas-${ratio}`, after: out.size, path: out.path });
    await page.screenshot({ path: path.join(folder, `canvas-${rw}-${rh}.png`) });
  }
  // The actual export dialog updates its number at each tier and produces exactly that file.
  await flush();
  await call(`/api/photos/${id}/edit`, { frame: { style: 'gallery' } }, 'PUT');
  await call(`/api/photos/${id}/crop`, { preset: '4:3', orientation: 'portrait' }, 'PUT');
  await openFrame();
  await page.keyboard.press('Meta+e');
  const dialog = page.getByRole('dialog');
  await dialog.locator('.export-size').filter({ hasText: /\d+ × \d+/ }).waitFor();
  for (const [tier, name] of [['original', '原始尺寸'], ['large', '大'], ['medium', '中'], ['small', '小']]) {
    await dialog.getByRole('group', { name: '尺寸', exact: true }).getByRole('button', { name, exact: true }).click();
    const text = await dialog.locator('.export-size').textContent();
    const response = page.waitForResponse((r) => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith(`/${id}/export`));
    await dialog.getByRole('button', { name: '导出', exact: true }).click();
    const out = await (await response).json();
    const size = await readJpegSize(out.path);
    assert(text.includes(`${size[0]} × ${size[1]} 像素`), 'Dialog size differs from the actual file');
    assert.equal(size[0] * 4, size[1] * 3);
    report.tiers.push({ tier, text, size });
    await dialog.getByText('已导出 1 张', { exact: true }).waitFor();
  }
  await page.screenshot({ path: path.join(folder, 'export-size-dialog.png') });
  await page.keyboard.press('Escape');
  await call(`/api/photos/${id}/crop`, { preset: 'original' }, 'PUT');
  await call(`/api/photos/${id}/edit`, { frame: { style: 'gallery' }, stamp: { enabled: false } }, 'PUT');
  await openFrame();
  await page.screenshot({ path: path.join(folder, 'original-gallery.png') });
  await page.setViewportSize({ width: 1024, height: 850 });
  await page.screenshot({ path: path.join(folder, 'frame-panel-1024.png') });
  await clickFrame('画布');
  await clickOption('画布比例', '3:4');
  await page.getByRole('group', { name: '画布比例', exact: true }).scrollIntoViewIfNeeded();
  const clipped = await page.evaluate(() => [...document.querySelectorAll('[aria-label="画布比例"] button')].filter((button) => {
    const range = document.createRange();
    range.selectNodeContents(button);
    const style = getComputedStyle(button);
    return range.getBoundingClientRect().width > button.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) + 0.5;
  }).map((button) => button.textContent));
  assert.deepEqual(clipped, [], 'Canvas ratio labels are clipped in the narrow panel');
  await page.screenshot({ path: path.join(folder, 'canvas-panel-1024.png') });
  assert.deepEqual(errors, []);
  assert.deepEqual(httpFailures.filter((r) => /\/(output-size|edit|crop|render|export)$/.test(r.path)), [], 'A frame, crop, size or export request failed');
  const timings = report.clicks.map((c) => c.ms).sort((a, b) => a - b);
  report.timing = { runs: timings.length, median: timings[Math.floor(timings.length / 2)], min: timings[0], max: timings.at(-1), unit: 'ms', limiter: 'click handler plus next animation frame' };
  assert(report.timing.median <= 100, 'Frame click median exceeded 100 ms');
  await writeFile(path.join(folder, 'browser-report.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ outputs: report.outputs, tiers: report.tiers, timing: report.timing, errors }));
} catch (error) {
  const token = process.env.GXR_STUDIO_TOKEN || '';
  const message = String(error.stack || error);
  report.failure = token ? message.replaceAll(token, '[redacted]') : message;
  await writeFile(path.join(folder, 'browser-failure.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.error(report.failure);
  process.exitCode = 1;
} finally {
  await browser.close();
}
