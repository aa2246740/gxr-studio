// Browser driver for halfframe-p0:
// - mode="detect": verifies mobile isDiptychBlob(file) in Chromium against given JPEGs.
// - mode="e2e": imports card250 pair 1 + pair 2, inspects grid badges / editor / recipe note / export.
// Photo filenames and EXIF are never printed to stdout.

import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { cellOf, exportFromGrid, pickAll, shareAll, sharedFiles, waitPhase } from './mobile_ui_export_parity.mjs';
import { phoneTouch } from './phone_touch.mjs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});
const out = { pageErrors: [] };
const line = (err) => (err instanceof Error ? err.message : String(err)).split('\n')[0].slice(0, 300);

function fakeShare() {
  const scope = window;
  scope.__shareLog = { canShare: 0, calls: [] };
  Object.defineProperty(navigator, 'canShare', {
    configurable: true,
    value: (data) => {
      scope.__shareLog.canShare += 1;
      return Array.isArray(data?.files) && data.files.length > 0 && data.files.every((f) => f instanceof File);
    },
  });
  Object.defineProperty(navigator, 'share', {
    configurable: true,
    value: (data) => {
      const active = navigator.userActivation ? navigator.userActivation.isActive : null;
      scope.__shareLog.calls.push({ active, files: data.files });
      return Promise.resolve();
    },
  });
}

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.addInitScript(phoneTouch);
  page.on('pageerror', (error) => out.pageErrors.push(error.message));

  if (manifest.mode === 'detect') {
    await page.goto(url);
    await page.evaluate(() => {
      const inp = document.createElement('input');
      inp.id = '__hf_probe';
      inp.type = 'file';
      inp.multiple = true;
      document.body.appendChild(inp);
    });
    const verdicts = [];
    const batchSize = 12;
    for (let i = 0; i < manifest.files.length; i += batchSize) {
      const batch = manifest.files.slice(i, i + batchSize);
      await page.locator('#__hf_probe').setInputFiles(batch);
      const part = await page.evaluate(async () => {
        const { isDiptychBlob } = await import('/src/lib/halfframe.ts');
        const files = Array.from(document.querySelector('#__hf_probe').files || []);
        return Promise.all(files.map((f) => isDiptychBlob(f)));
      });
      verdicts.push(...part);
    }
    out.verdicts = verdicts;
  } else if (manifest.mode === 'e2e') {
    await page.addInitScript(fakeShare);
    await page.goto(url);
    await settle(page);
    await pickAll(page, manifest, manifest.files);
    if (manifest.hasDat) {
      await page.waitForFunction(
        () => Boolean(window.__gxrBook?.dat) && !window.__gxrBook.datBusy,
        null,
        { timeout: 120_000 },
      );
    }

    out.gridBadges = {};
    for (const stem of manifest.stems) {
      out.gridBadges[stem] = await page.locator(`${cellOf(stem)} .bl`).innerText();
    }

    const s1 = manifest.stems[0];
    const s2 = manifest.stems[1];
    if (out.gridBadges[s2] !== 'JPG') {
      out.earlyExit = `grid badge mismatch for ${s2}: ${out.gridBadges[s2]}`;
    } else {
      // Photo 1 (single half-frame frame, has RAW)
      await page.locator(cellOf(s1)).evaluate((node) => node.click());
      await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, s1, { timeout: 120_000 });
      await page.waitForFunction((want) => document.querySelector('.app')?.dataset.settled === want, s1, { timeout: 120_000 });
      await page.waitForFunction(() => document.querySelector('.app')?.dataset.frame === '1', null, { timeout: 120_000 });
      const p1Layer = await page.evaluate(() => window.__gxrEngine?.layerSize?.() ?? null);
      await page.locator('.editor .toolbar [data-tool="recipe"]').click();
      await page.waitForSelector('.panel.recipe');
      let p1RecipeText = await page.locator('.editor .band').innerText();
      const p1StatusBtn = page.locator('.editor .panel.recipe [data-act="recipe-status"]');
      if (await p1StatusBtn.isVisible().catch(() => false)) {
        await p1StatusBtn.click();
        await page.waitForSelector('.recipe-info-sheet', { timeout: 10_000 });
        p1RecipeText += `\n${await page.locator('.recipe-info-sheet').innerText()}`;
        await page.locator('.recipe-info-sheet [data-act="close"]').click();
        await page.waitForSelector('.recipe-info-sheet', { state: 'detached', timeout: 10_000 });
      }
      await page.getByRole('button', { name: '照片', exact: true }).click();
      await page.waitForSelector('[data-screen="grid"]');

      // Photo 2 (diptych JPG, treated as JPG-only)
      await page.locator(cellOf(s2)).evaluate((node) => node.click());
      await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, s2, { timeout: 120_000 });
      await page.waitForFunction((want) => document.querySelector('.app')?.dataset.settled === want, s2, { timeout: 120_000 });
      await page.waitForFunction(() => document.querySelector('.app')?.dataset.frame === '1', null, { timeout: 120_000 });
      const p2Layer = await page.evaluate(() => window.__gxrEngine?.layerSize?.() ?? null);
      await page.locator('.stage .pv').screenshot({ path: manifest.stagePng });
      await page.locator('.editor .toolbar [data-tool="recipe"]').click();
      await page.waitForSelector('.panel.recipe');
      await page.locator('.editor .panel.recipe [data-act="recipe-status"]').click();
      await page.waitForSelector('.recipe-info-sheet');
      const p2RecipeText = await page.locator('.recipe-info-sheet').innerText();
      await page.locator('.recipe-info-sheet [data-act="close"]').click();
      await page.getByRole('button', { name: '照片', exact: true }).click();
      await page.waitForSelector('[data-screen="grid"]');

      await exportFromGrid(page, manifest.stems, '原始尺寸', '高');
      await waitPhase(page, ['running', 'ready']);
      const ready = await waitPhase(page, ['ready']);
      const shared = await shareAll(page);
      const files = await sharedFiles(page, manifest.outDir, 'hf-');

      out.photo1 = { layer: p1Layer, recipeHasDiptychNote: p1RecipeText.includes('这张是半格双联') };
      out.photo2 = { layer: p2Layer, recipeText: p2RecipeText };
      out.export = { ready, shared, files };
    }
  } else if (manifest.mode === 'desktop-note') {
    await page.goto(`${manifest.baseUrl}/?token=${encodeURIComponent(manifest.token)}`);
    await page.waitForSelector('.top, .grid, .empty', { timeout: 30_000 });
    await page.goto(`${manifest.baseUrl}/#/photo/${manifest.photo2Id}`);
    await page.waitForSelector('.notice.grey', { timeout: 30_000 });
    const note2 = await page.locator('.notice.grey').innerText();
    await page.goto(`${manifest.baseUrl}/#/photo/${manifest.photo1Id}`);
    await page.waitForSelector('.pane-head', { timeout: 30_000 });
    const hasDiptychNote1 = await page.evaluate(() => document.body.innerText.includes('这张是半格双联'));
    out.desktopNote2 = note2;
    out.desktopHasNote1 = hasDiptychNote1;
  }

  await page.close();
} catch (err) {
  out.error = line(err);
} finally {
  writeFileSync(manifest.out, JSON.stringify(out));
  await browser.close();
}
console.log('done');
