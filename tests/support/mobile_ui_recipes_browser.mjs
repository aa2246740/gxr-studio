// UI-M2 在无头 Chrome 里走一遍：导入校准数据、选照片（先不带相机配方文件，06d）、补选 GXRCE.DAT、
// 配方卡画出来、点一张换配方、换片再回来选择还在、长按看原图、删掉校准数据（06c）、只有 JPG（06e）。
// 输出一行 JSON，里面有 UI 交给显卡的烘焙表的 sha256，Python 那边和桌面的逐字节比。

import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const out = { pageErrors: [], steps: [] };
const step = (name, data = {}) => out.steps.push({ name, ...data });

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 30_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

/** UI 交给显卡的那张表：sha256、字节数和 meta。 */
async function bakeOf(page, key) {
  return page.evaluate(async (want) => {
    const made = window.__gxrBakes?.get(want);
    if (!made) return null;
    const digest = await crypto.subtle.digest('SHA-256', made.data);
    const sha256 = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
    return { sha256, bytes: made.data.byteLength, meta: made.meta };
  }, key);
}

/** 舞台的像素摘要（显卡读回，缩小后求和），用来确认照片真的换了。 */
async function stageSum(page) {
  return page.evaluate(async () => {
    const engine = window.__gxrEngine;
    for (let i = 0; i < 30 && engine.pending; i++) await new Promise((done) => requestAnimationFrame(done));
    const shot = await engine.readback();
    let sum = 0;
    for (let i = 0; i < shot.data.length; i += 4 * 97) sum += shot.data[i] * 3 + shot.data[i + 1] * 5 + shot.data[i + 2] * 7;
    return sum;
  });
}

/** 只在手动截图时：manifest.shots 给了文件夹就把这一屏存下来（不进仓库）。 */
async function snap(page, name) {
  if (manifest.shots) await page.screenshot({ path: `${manifest.shots}/chrome-${name}.png` });
}

const recipeKey = (page) => page.locator('.app').getAttribute('data-recipe-key');
const label = (page) => page.locator('.editor .lbl').innerText();

async function openFirst(page) {
  await page.locator('.cell').first().evaluate((node) => node.click());
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.frame === '1' && document.querySelector('.editor .lbl')?.textContent, null, { timeout: 120_000 });
}

async function backToGrid(page) {
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
}

async function waitCards(page) {
  await page.waitForFunction(
    () => {
      const strip = document.querySelector('.editor .strip[data-recipes]');
      if (strip?.dataset.state !== 'calibrated') return false;
      const cards = [...strip.querySelectorAll('.rc[data-card^="builtin:"] .th')];
      return cards.length === 8 && cards.every((node) => node.dataset.source === '电脑预测');
    },
    null,
    { timeout: 180_000 },
  );
}

try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  await page.goto(url);
  await settle(page);

  // 01：导入校准数据。先给一个压缩过的 zip，看到一行原因；再给对的。
  await page.locator('input.calibpick').setInputFiles(manifest.deflated);
  await page.waitForSelector('.calib [data-state="calib-error"]', { timeout: 60_000 });
  const refused = await page.locator('.calib [data-state="calib-error"]').innerText();
  await page.locator('input.calibpick').setInputFiles(manifest.pack);
  await page.waitForSelector('.calib[data-calib="ready"]', { timeout: 60_000 });
  await snap(page, '01-calibrated');
  step('calibration', { refused, card: await page.locator('.calib').innerText() });

  // 02 → 03，没有相机配方文件（06d）。
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.pairs);
  await page.waitForFunction(() => {
    const sub = document.querySelector('.nav small');
    return sub && !sub.textContent.includes('正在读取') && document.querySelector('.cell canvas');
  }, null, { timeout: 90_000 });
  await openFirst(page);
  const noDat = {
    state: await page.locator('.editor .strip[data-recipes]').getAttribute('data-state'),
    locked: await page.locator('.editor .strip[data-recipes] .rc.lockd').count(),
    names: await page.locator('.editor .strip[data-recipes] .rc .nm').allInnerTexts(),
    label: await label(page),
  };
  await page.locator('.editor .strip[data-recipes] .rc').first().click();
  await page.waitForSelector('.ask-sheet[data-state="no-recipe-file"]');
  noDat.sheet = await page.locator('.ask-sheet').innerText();
  await snap(page, '06d');
  await page.getByRole('button', { name: '先不用，只看中性转换' }).click();
  noDat.closed = (await page.locator('.ask-sheet').count()) === 0;
  step('no-recipe-file', noDat);

  // 补选 GXRCE.DAT（只选这一个文件，留在编辑器里），卡片按校准模型画出来。
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.dat);
  await waitCards(page);
  const stem = await page.locator('.editor').getAttribute('data-photo');
  const before = await stageSum(page);
  const defaultKey = await recipeKey(page);
  step('cards', {
    stem,
    names: await page.locator('.editor .strip[data-recipes] .rc .nm').allInnerTexts(),
    subs: await page.locator('.editor .strip[data-recipes] .rc .sub').allInnerTexts(),
    label: await label(page),
    defaultKey,
    defaultBake: await bakeOf(page, defaultKey),
  });

  // 点「GR 正片」：照片换成这张表。
  await page.locator('.editor .strip[data-recipes] .rc[data-card="builtin:1"]').click();
  await page.waitForFunction((prev) => {
    const key = document.querySelector('.app')?.dataset.recipeKey;
    return key && key !== prev;
  }, defaultKey, { timeout: 60_000 });
  const chosenKey = await recipeKey(page);
  await page.locator('.editor .tabs button', { hasText: '配方' }).click();
  await page.waitForTimeout(300);
  await snap(page, '03a');
  step('chosen', {
    key: chosenKey,
    bake: await bakeOf(page, chosenKey),
    label: await label(page),
    changed: (await stageSum(page)) !== before,
    panel: await page.locator('.editor .panel.recipe').innerText(),
    on: await page.locator('.editor .strip[data-recipes] .rc.on').getAttribute('data-card'),
  });

  // 相机里的用户配方（有能用的就点一张，走旋钮那条路）。
  const user = page.locator('.editor .strip[data-recipes] .rc[data-card^="camera_user:"]:not(.lockd)').first();
  if (await user.count()) {
    const id = await user.getAttribute('data-card');
    await user.click();
    await page.waitForFunction((prev) => document.querySelector('.app')?.dataset.recipeKey !== prev, chosenKey, { timeout: 60_000 });
    const key = await recipeKey(page);
    step('user', { id, key, bake: await bakeOf(page, key) });
    await page.locator('.editor .strip[data-recipes] .rc[data-card="builtin:1"]').click();
    await page.waitForFunction((want) => document.querySelector('.app')?.dataset.recipeKey === want, chosenKey, { timeout: 60_000 });
  }

  // 长按看原图，松手回来。
  const box = await page.locator('.stage').boundingBox();
  const sx = box.x + box.width / 2;
  const sy = box.y + box.height / 2;
  await page.mouse.move(sx, sy);
  await page.mouse.down();
  await page.waitForTimeout(600);
  const held = await label(page);
  await page.mouse.up();
  step('hold', { held, after: await label(page) });

  // 换到下一张再回来：每张照片记着自己的选择。
  await page.mouse.move(sx, sy);
  await page.mouse.down();
  await page.mouse.move(sx - 140, sy, { steps: 8 });
  await page.mouse.up();
  await page.waitForFunction((prev) => document.querySelector('.editor')?.dataset.photo !== prev, stem, { timeout: 60_000 });
  await waitCards(page);
  const second = { stem: await page.locator('.editor').getAttribute('data-photo'), on: await page.locator('.editor .strip[data-recipes] .rc.on').count(), key: await recipeKey(page) };
  await page.mouse.move(sx, sy);
  await page.mouse.down();
  await page.mouse.move(sx + 140, sy, { steps: 8 });
  await page.mouse.up();
  await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, stem, { timeout: 60_000 });
  await page.waitForFunction((want) => document.querySelector('.app')?.dataset.recipeKey === want, chosenKey, { timeout: 60_000 });
  step('swipe', { second, backOn: await page.locator('.editor .strip[data-recipes] .rc.on').getAttribute('data-card'), backKey: await recipeKey(page) });

  // 06c：删掉校准数据，配方还能选，但卡片和照片都是中性转换。
  await backToGrid(page);
  await page.getByRole('button', { name: '更多' }).click();
  await page.locator('.lrow', { hasText: '校准数据' }).click();
  await page.waitForSelector('.calib-sheet .calib[data-calib="ready"]');
  await page.locator('.calib-sheet .calib button', { hasText: '删除' }).click();
  await page.waitForSelector('.calib-sheet .calib[data-calib="none"]');
  await page.locator('.calib-sheet button', { hasText: '完成' }).click();
  await openFirst(page);
  await page.waitForFunction(() => document.querySelector('.editor .strip[data-recipes]')?.dataset.state === 'uncalibrated', null, { timeout: 60_000 });
  await page.locator('.editor .tabs button', { hasText: '配方' }).click();
  await snap(page, '06c');
  step('no-calibration', {
    label: await label(page),
    subs: await page.locator('.editor .strip[data-recipes] .rc .sub').allInnerTexts(),
    on: await page.locator('.editor .strip[data-recipes] .rc.on').getAttribute('data-card'),
    banner: await page.locator('.editor [data-state="no-calibration"]').innerText(),
    tag: await page.locator('.editor .recipe-head .tag[data-state]').innerText(),
    key: await recipeKey(page),
  });

  // 06e：只有 JPG。
  await backToGrid(page);
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.jpgOnly);
  const jpgId = manifest.jpgOnlyStem.toLowerCase();
  await page.waitForSelector(`.cell[data-id="${jpgId}"] canvas`, { timeout: 60_000 });
  await page.locator(`.cell[data-id="${jpgId}"]`).click();
  await page.waitForFunction(() => document.querySelector('.editor .strip[data-recipes]')?.dataset.state === 'jpg-only', null, { timeout: 60_000 });
  await page.locator('.editor .tabs button', { hasText: '配方' }).click();
  await snap(page, '06e');
  step('jpg-only', {
    label: await label(page),
    disabled: await page.locator('.editor .strip[data-recipes] .rc:disabled').count(),
    cards: await page.locator('.editor .strip[data-recipes] .rc').count(),
    panel: await page.locator('.editor .panel.recipe').innerText(),
  });
} finally {
  await browser.close();
}

process.stdout.write(`${JSON.stringify(out)}\n`);
