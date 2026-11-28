// 同名照片（机主 2026-11-28）：两张不同的同名照片都留下、标「(2)」、说一句；原来那张的调整和已经在显卡里的源不动；
// 第 2 份打开传进显卡的是自己的 DNG；同一份重新拷过（修改时间变了）认得出、不多留；倒着、不带文件夹重新选回来，调整还在原来那份上；
// 两份导出的文件名不撞；没有文件夹时同名的 RAW 和 JPG 按拍摄时间配。
// 用真实的 card250 照片（第 1、3 对，第 3 对改成第 1 对的名字）和 g250 相机配方文件（有它，RAW+JPG 打开时才走 RAW）。
// 哪个文件在哪一份：显卡上传（addRaw）的字节数，和 __gxrEdits 里每张的拍摄时间（只在页面里比，不写出来）。
// 隐私：stdout 只有一行 JSON；报告里只有计数、布尔和固定文案，没有文件名、拍摄时间或 EXIF。

import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { exportFromGrid, waitPhase } from './mobile_ui_export_parity.mjs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const out = { pageErrors: [], steps: {}, flowErrors: {}, error: null, ms: {} };
const S = manifest.stem;
const KEPT_ONE = '有 1 张同名照片另外留下了';
const GPU_ARGS = ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'];

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const again = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!again) return;
  }
  throw new Error('page kept reloading');
}

async function openPage(browser, label) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
  const page = await ctx.newPage();
  page.on('pageerror', (err) => out.pageErrors.push(`${label}: ${err.message.slice(0, 200)}`));
  const downloads = [];
  page.on('download', (dl) => downloads.push(dl));
  await page.goto(url);
  await settle(page);
  await page.evaluate(() => {
    localStorage.clear();
    // 每次传进显卡的整份 DNG 有多大（传完 bytes 会被转走，先记下）。
    window.__uploads = [];
    const engine = window.__gxrEngine;
    if (!engine) return;
    const original = engine.addRaw.bind(engine);
    engine.addRaw = async (key, layout, dng, scene) => {
      const row = { size: dng.byteLength, done: false };
      window.__uploads.push(row);
      await original(key, layout, dng, scene);
      row.done = true;
    };
  });
  return { ctx, page, downloads };
}

const picker = (page) => page.locator('input.filepick:not(.calibpick)');

async function waitGrid(page, n) {
  await page.waitForFunction(
    (want) => {
      const sub = document.querySelector('.nav small');
      return sub && !sub.textContent.includes('正在读取') && document.querySelectorAll('.cell').length === want && !document.querySelector('.cell.skel');
    },
    n,
    { timeout: 60_000 },
  ).catch(async () => {
    const cells = await page.evaluate(() => document.querySelectorAll('.cell').length);
    throw new Error(`grid has ${cells} photos, want ${n}`);
  });
}

/** 选一个文件夹：给选择框临时加上 webkitdirectory，Chromium 给每个文件填 webkitRelativePath（和「选文件夹」一样）。 */
async function pickFolder(page, dir) {
  await picker(page).evaluate((node) => node.setAttribute('webkitdirectory', ''));
  await picker(page).setInputFiles(dir);
  await picker(page).evaluate((node) => node.removeAttribute('webkitdirectory'));
}

const ids = (page) => page.locator('.cell').evaluateAll((nodes) => nodes.map((n) => n.dataset.id || ''));
const toast = (page) => page.evaluate(() => document.querySelector('.ftoast')?.textContent ?? null);
const uploads = (page) => page.evaluate(() => window.__uploads.map((row) => ({ ...row })));

async function waitToast(page, text) {
  await page.waitForFunction((t) => document.querySelector('.ftoast')?.textContent === t, text, { timeout: 10_000 }).catch(() => {});
  return toast(page);
}

async function cellFacts(page, id) {
  return page.locator(`.cell[data-id="${id}"]`).evaluate((cell, s) => {
    const aria = cell.getAttribute('aria-label') ?? '';
    return {
      copyBadge: cell.querySelector('.bl .cpy')?.textContent ?? null,
      ariaCopy: aria.endsWith(`${s} (2)`),
      pencil: Boolean(cell.querySelector('.edt')),
      rawJpg: aria.includes('RAW+JPG'),
      rawOnly: aria.includes('RAW') && !aria.includes('RAW+JPG'),
    };
  }, S);
}

/**
 * 每张照片的编辑和拍摄时间，按网格的顺序（网格和 __gxrEdits 用同一份顺序），配上格子的 id。
 * `mark` 给了就在页面里记下第一张的时间；`against` 给了就只回答「是不是那个时间」，时间本身不出页面。
 */
async function rows(page, mark = null, against = null) {
  return page.evaluate(([m, a]) => {
    const cells = [...document.querySelectorAll('.cell')].map((n) => n.dataset.id || '');
    const edits = window.__gxrEdits?.() ?? [];
    if (m) window[m] = edits[0]?.ms;
    return cells.map((id, i) => ({ id, edited: (edits[i]?.knobs?.exposure_ev ?? 0) !== 0, isMarked: a ? edits[i]?.ms === window[a] : null }));
  }, [mark, against]);
}

async function openCell(page, id) {
  await page.locator(`.cell[data-id="${id}"]`).click();
  await page.waitForSelector('[data-screen="editor"]', { timeout: 60_000 });
}

/** 等这张传进显卡、画出来（RAW 那条路）。 */
async function waitRaw(page, count) {
  await page.waitForFunction((n) => window.__uploads.length >= n && window.__uploads.every((row) => row.done), count, { timeout: 180_000 });
  await page.waitForFunction(() => document.querySelector('.editor .lbl')?.textContent, null, { timeout: 60_000 });
  await page.waitForTimeout(500);
}

const navFacts = (page) => page.evaluate(() => ({
  pos: document.querySelector('.editor .nav small')?.textContent ?? '',
  id: document.querySelector('.editor .nav')?.dataset.photoId ?? '',
}));

async function back(page) {
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
}

async function exposure(page) {
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  return (await page.locator('.editor .band-adjust-hd .val').innerText()).trim();
}

async function dragExposure(page) {
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  const slider = page.locator('input[data-key="exposure_ev"]');
  const box = await slider.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.75, box.y + box.height / 2, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(300);
  return (await page.locator('.editor .band-adjust-hd .val').innerText()).trim();
}

async function infoName(page) {
  await page.locator('.editor .nav [data-act="editor-more"]').click();
  await page.locator('.ms[data-state="editor-more"] [data-act="more-photo-info"]').click();
  await page.waitForSelector('.ms[data-state="photo-info"]', { timeout: 10_000 });
  const value = await page.locator('.ms[data-state="photo-info"] .lrow small').first().innerText();
  await page.locator('.ms[data-state="photo-info"] .hd .nb').click();
  return value.trim();
}

/** 等收下 `n` 个下载，返回建议的文件名。 */
async function downloaded(downloads, from, n) {
  const deadline = Date.now() + 60_000;
  while (downloads.length < from + n && Date.now() < deadline) await new Promise((r) => setTimeout(r, 100));
  return downloads.slice(from).map((dl) => dl.suggestedFilename());
}

/** 先选 A 那一对、打开、调好；再选文件夹 B（同名、另一张）：两张都留下，A 不动。再倒着、不带文件夹、换了修改时间重新选回来。最后导出。 */
async function keepFlow(browser) {
  const t0 = Date.now();
  const { ctx, page, downloads } = await openPage(browser, 'keep');
  const step = {};
  out.steps.keep = step;
  try {
    step.gpu = await page.evaluate(() => document.querySelector('.app')?.dataset.gpu === '1' && Boolean(window.__gxrEngine));
    await picker(page).setInputFiles([...manifest.first, manifest.dat, manifest.bin]);
    await waitGrid(page, 1);
    await page.waitForFunction(() => window.__gxrBook?.dat && !window.__gxrBook.datBusy, null, { timeout: 60_000 });
    const [first] = await ids(page);
    step.firstPlain = first === S.toLowerCase();
    await rows(page, '__tA');
    await openCell(page, first);
    await waitRaw(page, 1);
    const up1 = await uploads(page);
    step.firstUpload = up1.length === 1 && up1[0].size === manifest.aSize;
    step.firstPosPlain = !(await navFacts(page)).pos.includes(S);
    step.edited = await dragExposure(page);
    await back(page);

    // 文件夹 B：同名的另一张。
    await pickFolder(page, manifest.folderB);
    await waitGrid(page, 2);
    step.toast = await waitToast(page, KEPT_ONE);
    const both = await ids(page);
    const second = both.find((id) => id !== first) ?? '';
    step.ids = { count: both.length, firstKept: both.includes(first), secondFromFile: second.startsWith(`${first}~`) };
    step.firstCell = await cellFacts(page, first);
    step.secondCell = await cellFacts(page, second);
    const r1 = await rows(page, null, '__tA');
    step.rows = { firstIsA: r1.find((r) => r.id === first)?.isMarked === true, secondIsA: r1.find((r) => r.id === second)?.isMarked === true };

    // A：还是原来那份，调整在，显卡里的源还在（不再上传）。
    await openCell(page, first);
    await page.waitForTimeout(2500);
    step.firstReopen = { uploads: (await uploads(page)).length, posPlain: !(await navFacts(page)).pos.includes(S), exposure: await exposure(page) };
    await back(page);

    // B：传进显卡的是 B 的 DNG；顶栏、照片信息写「名字 (2)」。
    const before = (await uploads(page)).length;
    await openCell(page, second);
    await waitRaw(page, before + 1);
    const up2 = (await uploads(page)).slice(before);
    const nav = await navFacts(page);
    step.secondOpen = {
      uploadB: up2.length === 1 && up2[0].size === manifest.bSize,
      posNamed: nav.pos.startsWith(`${S} (2) · `),
      navId: nav.id === second,
      exposure: await exposure(page),
    };
    step.secondInfo = (await infoName(page)).endsWith(' (2)');
    await back(page);

    // 倒着、不带文件夹、重新拷过（修改时间变了）选回来：认出各是哪一份，不多留，调整还在 A 上。
    await picker(page).setInputFiles(manifest.again);
    await page.waitForTimeout(300);
    await waitGrid(page, 2);
    await page.waitForTimeout(1000);
    const after = await ids(page);
    const r2 = await rows(page, null, '__tA');
    const rowOf = (id) => r2.find((r) => r.id === id) ?? {};
    step.again = {
      sameIds: after.length === 2 && after.includes(first) && after.includes(second),
      toast: await toast(page),
      firstIsA: rowOf(first).isMarked === true,
      secondIsA: rowOf(second).isMarked === true,
      firstEdited: rowOf(first).edited === true,
      secondEdited: rowOf(second).edited === true,
      firstPencil: (await cellFacts(page, first)).pencil,
      secondPencil: (await cellFacts(page, second)).pencil,
    };

    // 两份一起导出（电脑上打成 ZIP，兜底链接表列出每个文件）；再单张导出第 2 份。
    const d0 = downloads.length;
    await exportFromGrid(page, [first, second], '小', '低', null, { hdr: false });
    await waitPhase(page, ['ready'], 300_000);
    await page.locator('.pdock [data-act="share"]').click();
    const zip = await downloaded(downloads, d0, 1);
    await page.waitForSelector('.links[data-state="download-backup"]', { timeout: 10_000 });
    const names = await page.locator('.links[data-state="download-backup"] a[download]').evaluateAll((as) => as.map((a) => a.download));
    step.batch = {
      zip: zip.length === 1 && /^GXR-export-.*\.zip$/.test(zip[0] ?? ''),
      count: names.length,
      unique: new Set(names).size === names.length,
      firstName: names.includes(`${S}_gxr.jpg`),
      secondName: names.includes(`${S} (2)_gxr.jpg`),
    };
    await page.locator('[data-act="close-download-backup"]').click();
    await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 }).catch(() => {});
    await openCell(page, second);
    await page.locator('.editor .toolbar [data-act="export"]').waitFor({ state: 'visible', timeout: 120_000 });
    const d1 = downloads.length;
    await page.locator('.editor .toolbar [data-act="export"]').click();
    await waitPhase(page, ['ready'], 300_000);
    await page.locator('.pdock [data-act="share"]').click();
    const single = await downloaded(downloads, d1, 1);
    step.single = { count: single.length, secondName: single[0] === `${S} (2)_gxr.jpg` };
  } catch (err) {
    out.flowErrors.keep = String(err?.message ?? err).slice(0, 400).replaceAll(S, '<stem>');
  } finally {
    await ctx.close();
    out.ms.keep = Date.now() - t0;
  }
}

/** 没有文件夹、一次选进同名的两个 RAW 和一个 JPG：JPG 配上拍摄时间一样的那个 RAW，另一个 RAW 单独一张。 */
async function timeFlow(browser) {
  const t0 = Date.now();
  const { ctx, page } = await openPage(browser, 'time');
  const step = {};
  out.steps.time = step;
  try {
    await picker(page).setInputFiles(manifest.trio);
    await waitGrid(page, 2);
    step.toast = await waitToast(page, KEPT_ONE);
    const all = await ids(page);
    const facts = await Promise.all(all.map((id) => cellFacts(page, id)));
    step.kinds = { pair: facts.filter((f) => f.rawJpg).length, rawOnly: facts.filter((f) => f.rawOnly).length };
    // 配对的那张的时间来自 JPG，单独那张来自它自己的 RAW。配错了（另一个 RAW 配上这个 JPG）两张的时间就一样。
    step.timesDiffer = await page.evaluate(() => {
      const ms = (window.__gxrEdits?.() ?? []).map((row) => row.ms);
      return ms.length === 2 && ms[0] !== ms[1];
    });
  } catch (err) {
    out.flowErrors.time = String(err?.message ?? err).slice(0, 400).replaceAll(S, '<stem>');
  } finally {
    await ctx.close();
    out.ms.time = Date.now() - t0;
  }
}

const browser = await launchPinnedChromium({ args: GPU_ARGS });
try {
  await keepFlow(browser);
  await timeFlow(browser);
} catch (err) {
  out.error = String(err?.message ?? err).slice(0, 400).replaceAll(S, '<stem>');
} finally {
  await browser.close();
  out.pageErrors = out.pageErrors.map((e) => e.replaceAll(S, '<stem>'));
  writeFileSync(manifest.reportPath, JSON.stringify(out));
  process.stdout.write(`${JSON.stringify({ done: true, error: out.error })}\n`);
}
