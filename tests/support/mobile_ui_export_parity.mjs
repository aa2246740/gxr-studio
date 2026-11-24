// UI-M3 第 9 项：在界面里给六张照片做编辑、全部导出（原始尺寸 / 高），再把 A 和 C 用小 / 低导出一次。
// 假的共享面板收下的每个文件写进 manifest.outDir（测试的临时目录），Python 那边和桌面 export_finish 逐字节比。

import { writeFileSync } from 'node:fs';

export const cellOf = (stem) => `.cell[data-id="${stem.toLowerCase()}"]`;

export async function pickAll(page, manifest, files) {
  if (manifest.pack) {
    await page.locator('input.calibpick').setInputFiles(manifest.pack);
    // 起始页没有校准卡了：等导入的包真的用上（不是内置的默认校准）。
    await page.waitForFunction(() => window.__gxrBook?.pack?.kind === 'user' && !window.__gxrBook.packBusy, null, { timeout: 60_000 });
  }
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(files);
  await page.waitForFunction(
    (n) => {
      const sub = document.querySelector('.nav small');
      return sub && !sub.textContent.includes('正在读取') && document.querySelectorAll('.cell').length >= n;
    },
    manifest.count,
    { timeout: 120_000 },
  );
}

async function openPhoto(page, stem) {
  await page.locator(cellOf(stem)).evaluate((node) => node.click());
  await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, stem, { timeout: 120_000 });
}

async function backToGrid(page) {
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
}

async function waitCards(page) {
  await page.waitForFunction(
    () => {
      const strip = document.querySelector('.editor .strip[data-recipes]');
      return strip?.dataset.state === 'calibrated' && strip.querySelectorAll('.rc[data-card^="builtin:"]').length === 8;
    },
    null,
    { timeout: 180_000 },
  );
}

/** 调整页的一个滑块：点底栏「调整」、点参数片、设值、发 input（和用户拖动一样只走 input）。返回滑块上实际的值。 */
async function setKnob(page, key, value) {
  if (!(await page.locator('.editor .panel.adjust:not([hidden])').count())) {
    await page.locator('.editor .toolbar [data-tool="adjust"]').click();
    await page.waitForSelector('.editor .panel.adjust:not([hidden])');
  }
  await page.locator(`.editor .panel.adjust .chips .chip[data-key="${key}"]`).click();
  return page.locator('.editor .panel.adjust input[type="range"]').evaluate((input, v) => {
    input.value = String(v);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    return Number(input.value);
  }, value);
}

async function pickCard(page, card) {
  const node = page.locator(`.editor .strip[data-recipes] .rc[data-card="${card}"]`);
  await node.scrollIntoViewIfNeeded();
  await node.click();
  await page.waitForFunction((id) => document.querySelector('.editor .strip[data-recipes] .rc.on')?.dataset.card === id, card, { timeout: 60_000 });
}

/** 一张照片在界面里做的编辑。返回实际设上的值（Python 那边按同样的值让桌面写 sidecar）。 */
export async function edit(page, stem, plan) {
  await openPhoto(page, stem);
  const done = { stem, knobs: {} };
  if (plan.card || plan.look) await waitCards(page);
  if (plan.card) {
    await pickCard(page, plan.card);
    done.card = plan.card;
  }
  if (plan.look) {
    const look = page.locator('.editor .strip[data-recipes] .rc[data-card^="look:"]:not(.lockd)').first();
    const id = await look.getAttribute('data-card');
    await pickCard(page, id);
    await page.locator(`.editor .strip[data-recipes] .rc[data-card="${id}"]`).click();
    await page.waitForSelector('.editor .recipe-fine:not([hidden])');
    const row = page.locator(`.editor .knobs[data-card="${id}"] .krow:not([data-knob="amount"])`).first();
    const knob = await row.getAttribute('data-knob');
    await page.locator(`.editor .recipe-fine .knob-chips .chip[data-knob-chip="${knob}"]`).click();
    await row.locator('input').evaluate((input) => {
      input.value = String(Math.min(Number(input.max), Number(input.value) + 2));
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
    done.look = { id: id.slice('look:'.length), knob, level: Number(await row.locator('.val').innerText()) };
  }
  for (const [key, value] of Object.entries(plan.knobs || {})) done.knobs[key] = await setKnob(page, key, value);
  await backToGrid(page);
  return done;
}

/** 选择模式里点这几张，点「导出 N 张」，在面板里选尺寸和品质，点「存到相册」。返回面板上的字。 */
export async function exportFromGrid(page, stems, size = '原始尺寸', quality = '高', beforeGo = null, opts = {}) {
  await page.locator('.nav .nb.bold').click();
  for (const stem of stems) await page.locator(cellOf(stem)).evaluate((node) => node.click());
  await page.locator('.dock [data-act="export"]').click();
  await page.waitForSelector('.ms.export');
  const sheet = page.locator('.ms.export');
  await sheet.locator('.frow').nth(0).locator('.seg button', { hasText: size }).click();
  await sheet.locator('.frow').nth(1).locator('.seg button', { hasText: quality }).click();
  const sw = sheet.locator('input.switch');
  const hdrDisabled = await sw.isDisabled();
  if (typeof opts.hdr === 'boolean' && !hdrDisabled) {
    const checked = await sw.isChecked();
    if (checked !== opts.hdr) await sw.click();
  }
  const text = await sheet.innerText();
  if (beforeGo) await beforeGo();
  await sheet.locator('.go').click();
  return { text, hdrDisabled };
}

export async function waitPhase(page, phases, timeout = 600_000) {
  await page.waitForFunction((want) => want.includes(document.querySelector('.pdock')?.dataset.phase), phases, { timeout });
  return page.locator('.pdock').innerText();
}

/** 点就绪条的「存进相册」（几批就点几次），收下假共享面板拿到的文件；最后一批分享完就绪条自动消失。 */
export async function shareAll(page) {
  const labels = [];
  for (let n = 0; n < 10; n += 1) {
    const before = await page.evaluate(() => window.__shareLog.calls.length);
    const btn = page.locator('.pdock [data-act="share"]');
    if (!(await btn.count())) break;
    labels.push(await btn.innerText());
    await btn.click();
    await page.waitForFunction((k) => window.__shareLog.calls.length > k, before, { timeout: 30_000 });
    await page.waitForTimeout(100);
    if (!(await page.locator('.pdock [data-act="share"]').count())) break;
  }
  const saved = (await page.locator('.pdock').count()) ? await page.locator('.pdock').innerText() : '';
  return { labels, saved };
}

/** 假共享面板收到的文件：名字、类型，字节写进 `dir/<前缀><名字>`。 */
export async function sharedFiles(page, dir, prefix, from = 0) {
  const meta = await page.evaluate((start) => window.__shareLog.calls.slice(start).map((c) => ({ active: c.active, files: c.files.map((f) => ({ name: f.name, type: f.type, size: f.size })) })), from);
  let call = from;
  for (const row of meta) {
    for (let i = 0; i < row.files.length; i += 1) {
      const b64 = await page.evaluate(
        async ({ c, k }) => {
          const bytes = new Uint8Array(await window.__shareLog.calls[c].files[k].arrayBuffer());
          let text = '';
          for (let j = 0; j < bytes.length; j += 0x8000) text += String.fromCharCode(...bytes.subarray(j, j + 0x8000));
          return btoa(text);
        },
        { c: call, k: i },
      );
      writeFileSync(`${dir}/${prefix}${row.files[i].name}`, Buffer.from(b64, 'base64'));
    }
    call += 1;
  }
  return meta;
}

/** 这一批每张的 side（交给 export_prepare 的那份）、shot 的宽高和结果 JSON，按文件名主干。 */
export async function batchFacts(page) {
  return page.evaluate(() => {
    const batch = window.__devExport.flow.current();
    const out = {};
    for (const item of batch.items) {
      const shot = batch.shots.get(item.id) || {};
      out[item.stem] = { side: item.req.side, width: shot.width ?? null, height: shot.height ?? null, result: batch.results.get(item.id) || null, binding: item.req.binding };
    }
    return { facts: out, mem: JSON.parse(document.querySelector('.app').dataset.exportMem || '{}'), stagedHigh: batch.stagedHigh, times: batch.times };
  });
}

export async function parity({ newPage, manifest, out }) {
  const page = await newPage('record');
  await page.evaluate(({ binding }) => {
    window.__devExport.binding = binding;
    window.__devExport.chunkFiles = 4;
  }, manifest);
  await pickAll(page, manifest, manifest.files);
  const edits = [];
  for (const [stem, plan] of Object.entries(manifest.plans)) edits.push(await edit(page, stem, plan));
  out.steps.edits = edits;

  // 六张，原始尺寸 / 高（SDR 对拍：显式关闭默认开启的 HDR）。分批上限设成 4（只在开发服务器里）：两批。
  const order = manifest.order;
  const sheet = await exportFromGrid(page, order, '原始尺寸', '高', null, { hdr: false });
  await waitPhase(page, ['running', 'ready']);
  const ready = await waitPhase(page, ['ready']);
  const facts = await batchFacts(page);
  const shared = await shareAll(page);
  const files = await sharedFiles(page, manifest.outDir, 'full-');
  const canShare = await page.evaluate(() => window.__shareLog.canShare);
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });
  out.steps.full = { sheet, ready, facts, shared, files, canShare };

  // A 和 C 再导一次：小 / 低。先关掉上一批还闲着的 Worker（只在开发服务器里），小尺寸的内存单独量。
  await page.evaluate(() => window.__devExport.core.stop());
  const calls = await page.evaluate(() => window.__shareLog.calls.length);
  const small = await exportFromGrid(page, manifest.smallOrder, '小', '低', null, { hdr: false });
  await waitPhase(page, ['ready']);
  const smallFacts = await batchFacts(page);
  const smallShared = await shareAll(page);
  const smallFiles = await sharedFiles(page, manifest.outDir, 'small-', calls);
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });
  out.steps.small = { sheet: small, facts: smallFacts, shared: smallShared, files: smallFiles };
  await page.close();
}
