// 本地实图拖动对照：node scripts/measure_curves.mjs <实例 URL> <照片 id> <main 的 web 副本> <结果 JSON>
// 只在隔离的试用图库运行。每轮交错旧主曲线、新蓝曲线和新主曲线；截图、原始数据仅保存在结果目录。
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { waitForPage } from '../tests/support/browser_wait.mjs';

const [url, photo, baselineWeb, output] = process.argv.slice(2);
assert(url && photo && baselineWeb && output, '需要 URL、照片 id、旧版 web 目录和结果文件');
const runtime = path.join(homedir(), '.codex/playwright-runtime/runtime.mjs');
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
const pin = verifyPinnedRuntime();
// macOS 的实际预览用 Metal；无头默认的 SwiftShader 合成器会把实图拖动变成 CPU 压测。
const args = ['--enable-unsafe-webgpu', ...(process.platform === 'darwin' ? ['--use-angle=metal'] : [])];
const browser = await launchPinnedChromium({ args });
const browserVersion = browser.version();
const target = new URL(url);
const endpoint = `/api/photos/${photo}/edit`;
const token = target.searchParams.get('token');
const results = [];
const samples = 36;
try {
  for (let trial = 0; trial < 6; trial++) {
    const variants = trial % 2 ? ['new-master', 'new-b', 'old-master'] : ['old-master', 'new-b', 'new-master'];
    for (const variant of variants) {
      const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message));
      page.on('response', (r) => {
        if (r.status() >= 400 && /\/(edit|render)(\?|$)/.test(new URL(r.url()).pathname)) errors.push(`HTTP ${r.status()} ${new URL(r.url()).pathname}`);
      });
      if (variant === 'old-master') {
        await page.route(`${target.origin}/assets/**`, async (route) => {
          const name = new URL(route.request().url()).pathname.slice(1);
          const type = name.endsWith('.css') ? 'text/css' : name.endsWith('.js') ? 'text/javascript' : undefined;
          await route.fulfill({ body: await readFile(path.join(baselineWeb, name)), contentType: type });
        });
      }
      await page.addInitScript(() => localStorage.setItem('gxr-studio.gpu-preview', 'on'));
      const cleared = await page.request.put(new URL(endpoint, target).href, { headers: { 'X-GXR-Token': token }, data: { recipe: null, lut: null, adjust: null, curve: null, geometry: null, frame: null, stamp: null } });
      assert(cleared.ok(), `清曲线失败：${cleared.status()}`);
      target.hash = `#/photo/${photo}`;
      await page.goto(target.href);
      await waitForPage(page, async () => Boolean((await import('/assets/viewer.js')).currentView()?.ed));
      await page.getByRole('tab', { name: '调整', exact: true }).click();
      await waitForPage(page, async () => Boolean((await import('/assets/viewer.js')).currentView()?.gpuActive), null, { timeout: 60000 });
      const channel = variant === 'new-b' ? 'b' : 'master';
      if (variant !== 'old-master') await page.locator(`.curve-channels [data-channel="${channel}"]`).click();
      const svg = page.locator('svg.curve');
      await svg.scrollIntoViewIfNeeded();
      const box = await svg.boundingBox();
      const xy = (y) => [box.x + 96 / 255 * box.width, box.y + (255 - y) / 255 * box.height];
      // 预热同样的 LUT 和颜色渲染路径，之后测的是热编辑器。
      await page.mouse.move(...xy(96));
      await page.mouse.down();
      await page.mouse.move(...xy(106), { steps: 6 });
      await page.mouse.up();
      await page.evaluate(async () => {
        const view = (await import('/assets/viewer.js')).currentView();
        await (await import('/assets/preview.js')).flushEdits(view);
        await (await import('/assets/gpu_stage.js')).gpuEngineForTests().readback();
      });
      const before = await page.evaluate(async () => {
        const { data } = await (await import('/assets/gpu_stage.js')).gpuEngineForTests().readback();
        const colors = new Set();
        for (let i = 0; i < 1024; i++) {
          const at = Math.floor(i * (data.length / 4 - 1) / 1023) * 4;
          colors.add((data[at] << 16) | (data[at + 1] << 8) | data[at + 2]);
        }
        if (colors.size < 128) throw new Error('预热图像缺少实图颜色，不能计时');
        return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', data))).join(',');
      });
      await page.evaluate(async () => {
        const eng = (await import('/assets/gpu_stage.js')).gpuEngineForTests();
        const svg = document.querySelector('svg.curve');
        window.curveMeasure = { frames: [], raf: [], inputs: 0, pending: null, lastRaf: performance.now(), running: true, info: eng.info };
        const m = window.curveMeasure;
        const tick = (now) => { if (!m.running) return; m.raf.push(now - m.lastRaf); m.lastRaf = now; requestAnimationFrame(tick); };
        requestAnimationFrame(tick);
        svg.addEventListener('pointermove', (e) => {
          if (e.buttons !== 1) return;
          m.inputs++;
          m.pending = performance.now();
        }, { capture: true });
        const prior = eng.onFrame;
        eng.onFrame = (frame) => {
          prior?.(frame);
          if (!frame.color || m.pending === null) return;
          const input = m.pending;
          m.pending = null;
          eng.device.queue.onSubmittedWorkDone().then(() => m.frames.push({ cpuMs: frame.cpuMs, submitMs: frame.submitted - input, completeMs: performance.now() - input, gpuMs: performance.now() - frame.submitted }));
        };
      });
      const cdp = trial === 5 ? await page.context().newCDPSession(page) : null;
      if (cdp) { await cdp.send('Profiler.enable'); await cdp.send('Profiler.start'); }
      await page.mouse.move(...xy(106));
      await page.mouse.down();
      for (let i = 0; i < samples; i++) {
        await page.mouse.move(...xy(116 + 12 * Math.sin(i * 0.4)));
        await page.waitForTimeout(14);
      }
      await page.mouse.up();
      const measurement = await page.evaluate(async () => {
        const v = (await import('/assets/viewer.js')).currentView();
        await (await import('/assets/preview.js')).flushEdits(v);
        const eng = (await import('/assets/gpu_stage.js')).gpuEngineForTests();
        await eng.device.queue.onSubmittedWorkDone();
        const { data } = await eng.readback();
        const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', data))).join(',');
        window.curveMeasure.running = false;
        return { ...window.curveMeasure, digest, curve: v.ed.curve, source: v.gpuActive, size: eng.finalSize() };
      });
      assert.notEqual(measurement.digest, before, '定时区域内实际渲染的像素没有改变');
      assert(measurement.inputs >= samples && measurement.frames.length > 10, '拖动或画面帧不足');
      assert.equal(errors.length, 0, JSON.stringify(errors));
      const saved = await page.request.get(new URL(endpoint, target).href, { headers: { 'X-GXR-Token': token } });
      const savedCurve = (await saved.json()).curve;
      assert.deepEqual(Object.keys(savedCurve), [channel], '保存错了通道');
      if (cdp) {
        const { profile } = await cdp.send('Profiler.stop');
        await writeFile(path.join(path.dirname(output), `${variant}.cpuprofile`), JSON.stringify(profile));
      } else {
        results.push({ trial, variant, errors, savedCurve, ...measurement });
        await writeFile(`${output}.partial`, JSON.stringify({ args, results }, null, 2));
      }
      console.error(JSON.stringify({ trial, variant, adapter: measurement.info, frames: measurement.frames.length }));
      if (trial === 0) await page.screenshot({ path: path.join(path.dirname(output), `${variant}.png`) });
      await page.close();
    }
  }
} finally {
  await browser.close();
}
const median = (values) => {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
};
const summary = Object.fromEntries(['old-master', 'new-master', 'new-b'].map((variant) => {
  const runs = results.filter((r) => r.variant === variant);
  const medians = runs.map((r) => median(r.frames.map((f) => f.completeMs)));
  return [variant, { runs: runs.length, medianMs: median(medians), rangeMs: [Math.min(...medians), Math.max(...medians)], cpuMs: median(runs.flatMap((r) => r.frames.map((f) => f.cpuMs))), gpuMs: median(runs.flatMap((r) => r.frames.map((f) => f.gpuMs))), rafMs: median(runs.flatMap((r) => r.raf)), inputs: runs.reduce((n, r) => n + r.inputs, 0), frames: runs.reduce((n, r) => n + r.frames.length, 0) }];
}));
await writeFile(output, JSON.stringify({ pin: { playwright: pin.playwrightVersion, chromium: pin.chromiumRevision, browserVersion }, args, samples, summary, results }, null, 2));
console.log(JSON.stringify(summary));
