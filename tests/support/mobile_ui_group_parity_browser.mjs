// UI-M3-GROUP 第 7 项：手机这一边在无头 Chrome 里用界面走一遍，和桌面对拍。
// 导入校准数据 → 选照片和 GXRCE.DAT → 来源：选 builtin:2、动一个旋钮、换到一个 Look、动它的一个旋钮、换回 builtin:2、
// 再动一次、调曝光和对比度 → 复制调整 → 选择三张目标 → 套用调整 → 读每张的导出请求（ui-m3 的 buildRequest，只取 side）
// → 撤销 → 再套用一次、改一张、撤销。报告写到 manifest.out（不打印到 stdout）：只有界面上的字、配方 id、档位和调整。

import { pathToFileURL } from 'node:url';
import { readFileSync, writeFileSync } from 'node:fs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();
const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const out = { pageErrors: [], steps: {}, actions: [] };
const text = (page, sel) => page.locator(sel).first().innerText();
const edits = (page) => page.evaluate(() => window.__gxrEdits());
const sides = (page, stems) => page.evaluate(async (list) => Object.fromEntries(await Promise.all(list.map(async (stem) => [stem, (await window.__gxrRequest(stem))?.side ?? null]))), stems);
const outcomes = (page) => page.evaluate(() => JSON.parse(JSON.stringify(window.__gxrPaste.lastOutcomes)));

async function grid(page) {
  await page.waitForSelector('[data-screen="grid"] .gscroll');
}

async function openPhoto(page, stem) {
  await page.locator(`.cell[data-id="${stem.toLowerCase()}"]`).evaluate((node) => node.click());
  await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, stem, { timeout: 120_000 });
}

async function back(page) {
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await grid(page);
}

async function pick(page, id) {
  await page.locator('.tabs button', { hasText: '配方' }).click();
  await page.locator(`.editor .strip[data-recipes] .rc[data-card="${id}"]`).click();
  await page.waitForSelector(`.panel.recipe .knobs[data-card="${id}"] .krow`, { timeout: 60_000 });
  out.actions.push({ pick: id });
}

/** 这张卡的第 `nth` 个旋钮挪到另一档（和现在不同的档），发 input 和 change（松手）。返回旋钮名和档位。 */
async function knob(page, id, nth) {
  const name = await page.locator(`.panel.recipe .knobs[data-card="${id}"] .krow`).nth(nth).getAttribute('data-knob');
  await page.locator(`.panel.recipe .knobs[data-card="${id}"] .krow[data-knob="${name}"] input`).evaluate((input) => {
    const max = Number(input.max);
    const now = Number(input.value);
    input.value = String(now > 0 ? now - 1 : Math.min(max, now + 1));
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  const row = (await edits(page)).find((item) => item.choice?.id === id);
  const level = row.levels[id][name];
  out.actions.push({ knob: { card: id, name, level } });
}

async function setAdjust(page, key, value) {
  await page.locator('.tabs button', { hasText: '调整' }).click();
  await page.locator(`.chips .chip[data-key="${key}"]`).click();
  await page.locator('.panel.adjust input[type="range"]').evaluate((input, v) => {
    input.value = String(v);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }, value);
}

async function select(page, stems) {
  await page.locator('.nav .nb.bold').click();
  for (const stem of stems) await page.locator(`.cell[data-id="${stem.toLowerCase()}"]`).evaluate((node) => node.click());
  await page.locator('[data-act="paste-selection"]').click();
  await page.waitForSelector('[data-state="confirm"]');
  await page.locator('[data-state="confirm"] [data-act="confirm"]').click();
  await page.waitForSelector('.pastebar[data-state="applied"]', { timeout: 30_000 });
  return text(page, '.pastebar[data-state="applied"] .tx b');
}

async function undo(page) {
  await page.locator('.pastebar [data-act="undo"]').click();
  await page.waitForSelector('.pastebar[data-state="info"]');
  return text(page, '.pastebar[data-state="info"]');
}

try {
  const page = await browser.newPage({ viewport: { width: 440, height: 956 } });
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  await page.goto(url);
  await page.waitForSelector('.app[data-core="1"]', { timeout: 30_000 });
  await page.locator('input.calibpick').setInputFiles(manifest.pack);
  await page.waitForSelector('.calib[data-calib="ready"]', { timeout: 60_000 });
  if (manifest.lut) {
    await page.locator('input.lutpick').setInputFiles(manifest.lut.path);
    await page.waitForFunction(
      (id) => Boolean(window.__gxrLutBook?.get(id)),
      manifest.lut.cubeId,
      { timeout: 60_000 },
    );
  }
  await page.locator('input.filepick:not(.calibpick):not(.lutpick)').setInputFiles([...manifest.files, manifest.dat]);
  await page.waitForFunction(
    (n) => {
      const sub = document.querySelector('.nav small');
      return sub && !sub.textContent.includes('正在读取') && document.querySelectorAll('.cell canvas').length >= n && window.__gxrBook?.dat;
    },
    manifest.photos,
    { timeout: 120_000 },
  );
  const { source, targets } = manifest;
  const all = [source, ...targets];

  // 来源的编辑：两个配方各记档位，再换回第一个（桌面 memory 里留着它切走时的旧档位）。
  await openPhoto(page, source);
  await page.waitForFunction(() => document.querySelector('.editor .strip[data-recipes]')?.dataset.state === 'calibrated', null, { timeout: 120_000 });
  await pick(page, 'builtin:2');
  await knob(page, 'builtin:2', 0);
  const look = await page.locator('.editor .strip[data-recipes] .rc[data-card^="look:"]').first().getAttribute('data-card');
  await pick(page, look);
  await knob(page, look, 0);
  await pick(page, 'builtin:2');
  await knob(page, 'builtin:2', 0);
  await setAdjust(page, 'exposure_ev', 0.7);
  await setAdjust(page, 'contrast', 12);
  if (manifest.lut) {
    await page.locator('.tabs button[data-tab="lut"]').click();
    await page.locator(`.panel.lut .rc[data-lut="${manifest.lut.cubeId}"]`).click();
    await page.waitForFunction(
      ({ stem, cubeId }) =>
        Boolean(document.querySelector('.tabs button[data-tab="lut"].dot')) &&
        window.__gxrEdits().find((row) => row.stem === stem)?.lut === cubeId,
      { stem: source, cubeId: manifest.lut.cubeId },
      { timeout: 60_000 },
    );
  }
  out.steps.source = { edit: (await edits(page)).find((row) => row.stem === source), side: (await sides(page, [source]))[source] };

  // 复制（默认的两组）→ 先不套用 → 网格里选三张目标 → 套用调整。
  await page.locator('[data-act="copy"]').click();
  await page.waitForSelector('[data-state="copy-options"]');
  await page.locator('[data-state="copy-options"] .btn.primary').click();
  await page.waitForSelector('[data-state="copied-where"]');
  await page.locator('[data-state="copied-where"] .btn.ghost').click();
  await back(page);
  const line = await select(page, targets);
  out.steps.paste = { line, outcomes: await outcomes(page), sides: await sides(page, all), edits: await edits(page) };
  const undone = await undo(page);
  out.steps.undo = { line: undone, sides: await sides(page, all) };

  // 再套用一次，改其中一张，再撤销：那一张不动并说明。
  const again = await select(page, targets);
  await openPhoto(page, targets[0]);
  await setAdjust(page, 'shadow', 20);
  await back(page);
  const changed = await undo(page);
  out.steps.changed = { pasteLine: again, line: changed, sides: await sides(page, all) };
} catch (error) {
  out.error = String(error?.stack ?? error);
} finally {
  await browser.close();
}
writeFileSync(manifest.out, JSON.stringify(out));
console.log(JSON.stringify({ done: true, error: out.error ? out.error.split('\n')[0] : null }));
if (out.error) process.exit(1);
