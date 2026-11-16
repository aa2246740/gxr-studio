// UI-M3-GROUP 在无头 Chrome 里走一遍：没复制时的那句、选配方动两个调整、复制调整、套用到这一组、打开一张目标看配方和调整、
// 分组间隔 3 → 1 分钟后组标题和「套用到这一组」的张数、选照片…（2 张）、撤销、套用之后又改过的那句。
// 只输出一行 JSON：界面上的字、组标题的张数，以及 dev 钩子读出的编辑状态（配方 id、调整、rev），不含照片数据。

import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const out = { pageErrors: [], steps: {} };
const step = (name, data) => {
  out.steps[name] = data;
};

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 30_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

const edits = (page) => page.evaluate(() => window.__gxrEdits());
const editOf = async (page, stem) => (await edits(page)).find((row) => row.stem === stem);
const text = (page, sel) => page.locator(sel).first().innerText();

async function grid(page) {
  await page.waitForSelector('[data-screen="grid"] .gscroll');
}

async function openPhoto(page, stem) {
  await page.locator(`.cell[data-id="${stem.toLowerCase()}"]`).evaluate((node) => node.click());
  await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want && document.querySelector('.app')?.dataset.frame === '1', stem, { timeout: 120_000 });
}

async function back(page) {
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await grid(page);
}

/** 调整页的一根滑块：设值，发 input 和 change（松手），和手指拖完一样。 */
async function setKnob(page, key, value) {
  const tab = page.locator('.tabs button', { hasText: '调整' });
  await tab.click();
  await page.locator(`.chips .chip[data-key="${key}"]`).click();
  await page.locator('.panel.adjust input[type="range"]').evaluate((input, v) => {
    input.value = String(v);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }, value);
}

async function knobShown(page, key) {
  await page.locator('.tabs button', { hasText: '调整' }).click();
  await page.locator(`.chips .chip[data-key="${key}"]`).click();
  return page.locator('.panel.adjust .val').innerText();
}

/** 带着编辑的照片右下角的铅笔（04e），按格子的照片 id。 */
async function pencils(page) {
  return page.locator('.cell').evaluateAll((nodes) => nodes.filter((node) => node.querySelector('.edt')).map((node) => node.dataset.id).sort());
}

async function heads(page) {
  return page.locator('.gh[data-row]').evaluateAll((nodes) =>
    nodes.map((node) => ({ row: node.dataset.row, range: node.querySelector('.rng')?.textContent, cnt: node.querySelector('.cnt')?.textContent, go: node.querySelector('.go')?.textContent })),
  );
}

async function copyDefaults(page) {
  await page.locator('[data-act="copy"]').click();
  await page.waitForSelector('[data-state="copy-options"]');
  const sheet = await text(page, '[data-state="copy-options"]');
  const button = await text(page, '[data-state="copy-options"] .btn.primary');
  await page.locator('[data-state="copy-options"] .btn.primary').click();
  await page.waitForSelector('[data-state="copied-where"]');
  return { sheet, button, where: await text(page, '[data-state="copied-where"]') };
}

async function confirmText(page) {
  await page.waitForSelector('[data-state="confirm"]');
  return { title: await text(page, '[data-state="confirm"] h2'), body: await text(page, '[data-state="confirm"]') };
}

async function appliedBar(page) {
  await page.waitForSelector('.pastebar[data-state="applied"]', { timeout: 30_000 });
  return { line: await text(page, '.pastebar[data-state="applied"] .tx b'), sub: await text(page, '.pastebar[data-state="applied"] .tx small') };
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
  await settle(page);
  await page.locator('input.filepick:not(.calibpick)').setInputFiles([...manifest.files, manifest.dat]);
  await page.waitForFunction(
    (n) => {
      const sub = document.querySelector('.nav small');
      return sub && !sub.textContent.includes('正在读取') && document.querySelectorAll('.cell canvas').length >= n && window.__gxrBook?.dat;
    },
    manifest.photos,
    { timeout: 120_000 },
  );
  await grid(page);
  const start = await edits(page);
  step('loaded', { photos: start.length, revs: start.map((row) => row.rev), heads: await heads(page) });

  // 没复制就点「套用到这一组」：一句怎么复制。
  await page.locator('.gh .go').first().click();
  await page.waitForSelector('.pastebar[data-state="info"]');
  step('hint', { line: await text(page, '.pastebar[data-state="info"]'), idle: await page.locator('.gh .go.idle').count() });

  // 来源：选配方、动曝光和对比度。
  const src = manifest.source;
  await openPhoto(page, src);
  await page.waitForFunction(() => {
    const strip = document.querySelector('.editor .strip[data-recipes]');
    return strip && strip.dataset.state !== 'no-recipe-file' && strip.querySelector(`.rc[data-card="${'builtin:2'}"]:not(:disabled)`);
  }, null, { timeout: 60_000 });
  const before = await editOf(page, src);
  await page.locator('.editor .strip[data-recipes] .rc[data-card="builtin:2"]').click();
  await setKnob(page, 'exposure_ev', 0.7);
  await setKnob(page, 'contrast', 12);
  const source = await editOf(page, src);
  step('source', { beforeRev: before.rev, after: source, on: await page.locator('.editor .strip[data-recipes] .rc.on').getAttribute('data-card') });

  // 04a → 04b → 04c → 网格上的结果。
  const copied = await copyDefaults(page);
  await page.locator('[data-state="copied-where"] [data-act="apply-group"]').click();
  const confirm = await confirmText(page);
  await page.locator('[data-state="confirm"] [data-act="confirm"]').click();
  await grid(page);
  const applied = await appliedBar(page);
  step('group', { ...copied, confirm, applied, edits: await edits(page), pencils: await pencils(page) });

  // 打开一张目标：同一个配方、同样的调整。回到网格，结果栏还在。
  const target = manifest.sameGroupTarget;
  await openPhoto(page, target);
  const opened = {
    on: await page.locator('.editor .strip[data-recipes] .rc.on').getAttribute('data-card'),
    exposure: await knobShown(page, 'exposure_ev'),
    contrast: await knobShown(page, 'contrast'),
  };
  await back(page);
  opened.barAfterBack = await page.locator('.pastebar[data-state="applied"]').count();
  step('target', opened);

  // 分组间隔改成 1 分钟：组标题的张数跟着变，「套用到这一组」给的是新的张数。
  await page.locator('.nav .nb.plain').click();
  await page.locator('.ms .seg button', { hasText: '1 分钟' }).click();
  await page.waitForFunction((n) => document.querySelectorAll('.gh[data-row]').length === n, manifest.expect['1'].length, { timeout: 10_000 });
  const gap1 = await heads(page);
  await page.locator(`.gh[data-row="group:${manifest.sourceGroupFirst1}"] .go`).click();
  const gap1Confirm = await confirmText(page);
  await page.locator('[data-state="confirm"] .btn', { hasText: '取消' }).click();
  step('gap', { gap1, confirm: gap1Confirm });

  // 选照片…：在 04b 点「选照片…」，网格进挑选模式，选 2 张，套用。
  const [p1, p2] = manifest.picks;
  const pickedBefore = [await editOf(page, p1), await editOf(page, p2)];
  await openPhoto(page, src);
  const again = await copyDefaults(page);
  await page.locator('[data-state="copied-where"] .btn', { hasText: '选照片…' }).click();
  await grid(page);
  const pickTitle = await text(page, '.nav .t b');
  for (const stem of [p1, p2]) await page.locator(`.cell[data-id="${stem.toLowerCase()}"]`).evaluate((node) => node.click());
  const pickBar = { text: await text(page, '.dock.pastebar'), sub: await text(page, '.nav .t small') };
  await page.locator('[data-act="paste-picked"]').click();
  const pickConfirm = await confirmText(page);
  await page.locator('[data-state="confirm"] [data-act="confirm"]').click();
  const pickApplied = await appliedBar(page);
  const pickedAfter = [await editOf(page, p1), await editOf(page, p2)];
  const undone = await undo(page);
  const pickedUndone = [await editOf(page, p1), await editOf(page, p2)];
  step('pick', { button: again.button, pickTitle, pickBar, confirm: pickConfirm, applied: pickApplied, before: pickedBefore, after: pickedAfter, undone, restored: pickedUndone });

  // 改一张，再套用，再改它，撤销：那一张不动，并说明。
  await openPhoto(page, p1);
  await setKnob(page, 'shadow', 20);
  await back(page);
  await page.locator('.nav .nb.bold').click();
  for (const stem of [p1, p2]) await page.locator(`.cell[data-id="${stem.toLowerCase()}"]`).evaluate((node) => node.click());
  await page.locator('[data-act="paste-selection"]').click();
  const selConfirm = await confirmText(page);
  await page.locator('[data-state="confirm"] [data-act="confirm"]').click();
  const selApplied = await appliedBar(page);
  await openPhoto(page, p1);
  await setKnob(page, 'shadow', -15);
  await back(page);
  const changedBefore = await editOf(page, p1);
  const changedLine = await undo(page);
  step('changed', {
    confirm: selConfirm,
    applied: selApplied,
    line: changedLine,
    p1: await editOf(page, p1),
    p1Before: changedBefore,
    p2: await editOf(page, p2),
  });

  // 清除剪贴板：底栏收起，组标题回到灰的。
  await page.waitForSelector('.pastebar[data-state="copied"]', { timeout: 10_000 });
  const copiedBar = await text(page, '.pastebar[data-state="copied"]');
  await page.locator('[data-act="clear-clip"]').click();
  step('cleared', { copiedBar, bars: await page.locator('.pastebar').count(), idle: await page.locator('.gh .go.idle').count() });

  // Sol r1 P2：只套用配方（目标的调整本来全是 0），再点「全部重置」、点两下已经是 0 的滑块、松开没动的滑块：都不是修改，
  // rev 不变，撤销照样还原。
  const target2 = manifest.picks[1];
  const zeroBefore = await editOf(page, target2);
  await openPhoto(page, src);
  await page.locator('[data-act="copy"]').click();
  await page.waitForSelector('[data-state="copy-options"]');
  await page.locator('[data-state="copy-options"] .optrow[data-group="adjust"]').click();
  const recipeOnly = await text(page, '[data-state="copy-options"] .btn.primary');
  await page.locator('[data-state="copy-options"] .btn.primary').click();
  await page.waitForSelector('[data-state="copied-where"]');
  await page.locator('[data-state="copied-where"] .btn.ghost').click();
  await back(page);
  await page.locator('.nav .nb.bold').click();
  await page.locator(`.cell[data-id="${target2.toLowerCase()}"]`).evaluate((node) => node.click());
  await page.locator('[data-act="paste-selection"]').click();
  await confirmText(page);
  await page.locator('[data-state="confirm"] [data-act="confirm"]').click();
  const noopApplied = await appliedBar(page);
  const pasted = await editOf(page, target2);
  await openPhoto(page, target2);
  await page.locator('.tabs button', { hasText: '调整' }).click();
  await page.locator('.hint-row .fbtn', { hasText: '全部重置' }).click();
  await page.locator('.panel.adjust input[type="range"]').evaluate((input) => {
    input.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await back(page);
  const afterNoop = await editOf(page, target2);
  const pencilsBeforeUndo = await pencils(page);
  const noopLine = await undo(page);
  step('noop', { recipeOnly, before: zeroBefore, applied: noopApplied, pasted, afterNoop, line: noopLine, restored: await editOf(page, target2), pencilsBeforeUndo, pencilsAfterUndo: await pencils(page) });
} catch (error) {
  out.error = String(error?.stack ?? error);
} finally {
  await browser.close();
}
console.log(JSON.stringify(out));
if (out.error) process.exit(1);
