// 曲线导出：在界面里改曲线，再按 UI-M3 的办法整张导出。stdout 不打照片。

import { batchFacts, exportFromGrid, pickAll, shareAll, sharedFiles, waitPhase } from './mobile_ui_export_parity.mjs';

async function backToGrid(page) {
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
}

async function pointer(page, type, x, y) {
  await page.evaluate(
    ({ type, x, y }) => {
      const svg = document.querySelector('.editor svg.curve');
      const box = svg.getBoundingClientRect();
      if (!(box.width > 0) || !(box.height > 0)) throw new Error('curve surface has no box');
      svg.dispatchEvent(
        new PointerEvent(type, {
          bubbles: true,
          cancelable: true,
          pointerId: 1,
          button: 0,
          buttons: type === 'pointerup' || type === 'pointercancel' ? 0 : 1,
          clientX: box.left + (x / 255) * box.width,
          clientY: box.top + ((255 - y) / 255) * box.height,
        }),
      );
    },
    { type, x, y },
  );
}

async function tap(page, x, y) {
  await pointer(page, 'pointerdown', x, y);
  await pointer(page, 'pointerup', x, y);
}

async function drag(page, x0, y0, x1, y1) {
  await pointer(page, 'pointerdown', x0, y0);
  await pointer(page, 'pointermove', x1, y1);
  await pointer(page, 'pointerup', x1, y1);
}

async function readCurve(page, stem) {
  return page.evaluate((name) => {
    const row = window.__gxrEdits().find((item) => item.stem === name);
    return row ? row.curve : undefined;
  }, stem);
}

async function editCurve(page, stem, kind) {
  await page.locator(`.cell[data-id="${stem.toLowerCase()}"]`).evaluate((node) => node.click());
  await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, stem, { timeout: 120_000 });
  await page.waitForFunction((want) => document.querySelector('.app')?.dataset.settled === want, stem, { timeout: 180_000 });
  if (kind === 'none') {
    await backToGrid(page);
    return readCurve(page, stem);
  }
  await page.getByRole('tab', { name: '曲线', exact: true }).click();
  await page.waitForSelector('.editor .panel.curve-panel svg.curve');
  await page.locator('.editor svg.curve').scrollIntoViewIfNeeded();
  const channel = async (key) => {
    await page.locator(`.editor .curve-btn[data-channel="${key}"]`).click();
  };
  if (kind === 's-curve') {
    await tap(page, 64, 32);
    await tap(page, 192, 220);
  } else if (kind === 'r-only') {
    await channel('r');
    await tap(page, 128, 40);
  } else if (kind === 'g-b') {
    await channel('g');
    await tap(page, 96, 180);
    await channel('b');
    await tap(page, 160, 50);
  } else if (kind === 'endpoint') {
    await drag(page, 250, 250, 250, -10);
  } else if (kind === 'many') {
    for (const x of [36, 72, 108, 144, 180, 216]) await tap(page, x, Math.min(255, x + 40));
  } else if (kind === 'identity') {
    await tap(page, 64, 32);
    await page.getByRole('button', { name: '重置全部曲线', exact: true }).click();
  } else {
    throw new Error('unknown curve edit');
  }
  const curve = await readCurve(page, stem);
  await backToGrid(page);
  return curve;
}

export async function parity({ newPage, manifest, out }) {
  const page = await newPage('record');
  await page.evaluate(({ binding }) => {
    window.__devExport.binding = binding;
    window.__devExport.chunkFiles = 8;
  }, manifest);
  await pickAll(page, manifest, manifest.files);
  const edited = [];
  for (const job of manifest.jobs) {
    const curve = await editCurve(page, job.stem, job.kind);
    if (job.kind === 'none' || job.kind === 'identity') {
      if (curve) throw new Error('identity curve was stored');
    } else if (!curve) {
      throw new Error('curve edit did not reach the photo');
    }
    edited.push({ kind: job.kind, curve });
  }
  out.steps.edited = edited;
  const order = manifest.jobs.map((job) => job.stem);
  const sheet = await exportFromGrid(page, order);
  await waitPhase(page, ['running', 'ready']);
  const ready = await waitPhase(page, ['ready']);
  const facts = await batchFacts(page);
  const shared = await shareAll(page);
  const files = await sharedFiles(page, manifest.outDir, 'curve-');
  out.steps.full = { sheet, ready, facts, shared, files };
  await page.close();
}
