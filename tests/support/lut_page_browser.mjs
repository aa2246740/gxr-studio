// 真实页面、真实 LUT 导入；只有拟合与配方保存用可控的接口夹具。不保存合成照片截图。
import assert from 'node:assert/strict';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtime, port, token, files] = process.argv.slice(2);
const { launchPinnedChromium } = await import(pathToFileURL(runtime).href);
const browser = await launchPinnedChromium();
const out = { errors: [], viewports: [] };
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  page.on('pageerror', (error) => out.errors.push(error.message));
  const calibratedUnits = new Set(['jlaunch']);
  await page.route('**/api/lut/quality', async (route) => {
    await route.fulfill({ json: { sources: ['jlaunch', 'qlaunch', 'l08firm'].map((unit, index) => ({
      id: String(index + 1).repeat(64), unit, coverage: { train: 64, photo_iso200: 32 }, missing: ['photo_other_iso'],
    })) } });
  });
  await page.route('**/api/lut/spaces', async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.units = body.units.map((u) => ({ ...u, calibrated: calibratedUnits.has(u.id),
      fit_supported: calibratedUnits.has(u.id), calibration: { state: calibratedUnits.has(u.id) ? 'ready' : 'missing',
        isos: u.id === 'jlaunch' ? [200, 1600] : [200], exposure_note: u.id === 'jlaunch' ? null : '曝光沿用 50mm' } }));
    await route.fulfill({ response, json: body });
  });
  let done = false;
  let saved = null;
  let photos = [];
  let photoRequests = 0;
  let releasePhotos;
  const photosReady = new Promise((resolve) => { releasePhotos = resolve; });
  let photosBlocked = true;
  const candidate = {
    unit: 'jlaunch', slot: 8, recipe: { kind: 'local', unit: 'jlaunch', slot: 8, id: 'a'.repeat(64) },
    similarity: 88, fit: { holdout: { median: 1.2 } }, record_sha256: 'b'.repeat(64),
    quality: { state: 'needs_evidence', failures: [], missing: ['photo_other_iso'] },
  };
  await page.route('**/api/photos?*', async (route) => {
    photoRequests++;
    const response = await route.fetch();
    // 和真实 API 一样：unit 是焦距文字；MOUNT 50mm 也不能被当作 A12 50mm。
    photos = (await response.json()).map((p, i) => ({ ...p,
      unit: i < 2 ? '50mm' : '28mm', lens: i === 0 ? 'GXR MOUNT 50mm' : i === 1 ? 'A12 50mm' : 'A12 28mm',
    }));
    await photosReady;
    await route.fulfill({ json: photos });
  });
  await page.route('**/api/lut/fit', async (route) => {
    out.request = route.request().postDataJSON();
    await route.fulfill({ json: { job_id: 'abc123abc123', unit: out.request.unit, photos: [] } });
  });
  await page.route('**/api/lut/fit/abc123abc123', async (route) => {
    await route.fulfill({ json: {
      job_id: 'abc123abc123', unit: 'jlaunch', state: done ? 'done' : 'running',
      progress: { stage: 'exact', completed_starts: 2 },
      result: { candidates: [candidate], formula: '电脑预测' },
    } });
  });
  await page.route('**/api/my-recipes', async (route) => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      saved = { id: candidate.recipe.id, name: body.name, recipe: candidate.recipe, revision: 1,
        quality: candidate.quality, source: { title: 'Warm Street' }, fit: { similarity: 88 } };
      out.saved = saved;
      await route.fulfill({ json: saved });
    } else await route.fulfill({ json: { recipes: saved ? [saved] : [] } });
  });

  const unit = () => page.getByLabel('配方适用的镜头组件', { exact: true });
  const space = () => page.getByLabel('输入色彩空间', { exact: true });
  const choice = async () => ({ space: await space().inputValue(), source: await page.locator('[data-role="lut-space"] .tag').innerText() });
  const visibleInViewport = async (locator, state) => {
    for (const [width, height] of [[1280, 800], [1440, 900]]) {
      await page.setViewportSize({ width, height });
      const rect = await locator.boundingBox();
      assert(rect, `${state} 的元素不可见`);
      const visible = await locator.evaluate((el) => {
        const r = el.getBoundingClientRect();
        const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return el === top || el.contains(top);
      });
      out.viewports.push({ state, width, height, bottom: rect.y + rect.height, visible });
      assert(rect.y >= 0 && rect.y + rect.height <= height && visible, `${state} 不在首屏：${JSON.stringify(rect)}`);
      assert.equal(await page.locator('.lut-scroll').evaluate((el) => el.scrollTop), 0);
    }
  };
  const importLut = async (filename) => {
    const uploaded = page.waitForResponse((response) => response.url().endsWith('/api/lut/upload'));
    const importedLibrary = photosBlocked ? page.waitForResponse((response) => response.url().endsWith('/api/luts')) : null;
    await page.getByLabel('导入 LUT', { exact: true }).setInputFiles(path.join(files, filename));
    await (await uploaded).finished();
    if (photosBlocked) {
      await (await importedLibrary).finished();
      // 导入已返回，等一帧让页面进入照片读取，再放行初始化请求。
      await page.evaluate(() => new Promise(requestAnimationFrame));
      photosBlocked = false;
      releasePhotos();
    }
    await space().waitFor();
  };

  const firstPhotoRequest = page.waitForRequest((request) => new URL(request.url()).pathname === '/api/photos');
  await page.goto(`http://127.0.0.1:${port}/?token=${token}#/recipes`);
  await firstPhotoRequest;
  assert.doesNotMatch(await page.locator('.lut-page').innerText(), /^null$/m);
  await importLut('Warm_Street.cube');
  out.default = { ...await choice(), unit: await unit().inputValue() };
  assert.equal(out.default.unit, 'qlaunch', '没有手选时取照片最多的组件');
  assert(await page.getByRole('button', { name: '为A12 28mm生成完整配方', exact: true }).isDisabled());
  assert(await page.getByText('A12 28mm 的校准数据尚未导入或已失效', { exact: false }).isVisible());
  await page.getByLabel('预览照片', { exact: true }).selectOption(photos[0].id);
  assert.equal(await unit().inputValue(), 'l08firm', '预览换组件时默认值也随之变化');
  assert(await page.getByRole('button', { name: '为MOUNT生成完整配方', exact: true }).isDisabled());
  await unit().selectOption('jlaunch');
  assert(await page.getByText('预览照片来自MOUNT。配方只会出现在A12 50mm拍的照片上。', { exact: true }).isVisible());
  assert.equal(await page.locator('.lut-more textarea').first().isVisible(), false);
  const groups = await space().locator('optgroup').evaluateAll((els) => els.map((el) => el.label));
  assert.deepEqual(groups, ['普通照片与视频显示', '相机 Log 与高级空间']);
  await visibleInViewport(page.getByRole('button', { name: '为A12 50mm生成完整配方', exact: true }), 'generate');
  await page.getByRole('button', { name: '为A12 50mm生成完整配方', exact: true }).click();
  await page.getByText('正在为A12 50mm生成配方…', { exact: false }).waitFor();
  assert(await unit().isDisabled());
  assert(await space().isDisabled());
  await page.getByText('正在精确收敛并重算整数记录…', { exact: true }).waitFor();
  assert.equal(out.request.quality_source, '1'.repeat(64));
  done = true;
  await page.getByText('为A12 50mm做的配方', { exact: true }).waitFor();
  assert(await page.getByText('电脑预测', { exact: true }).count() > 0);
  out.name = await page.getByLabel('名称', { exact: true }).inputValue();
  assert(await page.getByText('质量证据未齐，可保存为本机草稿', { exact: true }).isVisible());
  await visibleInViewport(page.getByRole('button', { name: '保存本机草稿', exact: true }), 'save');
  await page.getByRole('button', { name: '保存本机草稿', exact: true }).click();
  await page.getByText('已保存「Warm Street」，这是为A12 50mm做的配方。', { exact: false }).waitFor();
  assert(await page.getByRole('button', { name: '配方已保存', exact: true }).isDisabled());
  await page.getByText('更多信息（可选）', { exact: true }).click();
  assert(await page.getByRole('button', { name: '保存 LUT 信息', exact: true }).isVisible());

  await space().selectOption('rec709');
  await page.getByText('已手选', { exact: true }).waitFor();
  await page.getByRole('button', { name: '重新选择', exact: true }).click();
  await importLut('Warm_Street.cube');
  out.manual = await choice();
  await page.getByRole('button', { name: '重新选择', exact: true }).click();
  await importLut('Declared.cube');
  out.declared = await choice();
  await page.getByRole('button', { name: '重新选择', exact: true }).click();
  await importLut('Guessed_sRGB.cube');
  out.guessed = await choice();

  // #78 负责路由识别；这里直接调用本页入口，验证本页自己读取 unit 查询参数。
  await page.evaluate(async () => {
    history.replaceState(null, '', '#/recipes?unit=l08firm');
    const { renderLutPage } = await import('/assets/lut.js');
    renderLutPage(document.getElementById('view'));
  });
  out.queryUnit = await unit().inputValue();
  await page.evaluate(async () => {
    history.replaceState(null, '', '#/recipes?unit=toString');
    const { renderLutPage } = await import('/assets/lut.js');
    renderLutPage(document.getElementById('view'));
  });
  out.invalidQueryUnit = await unit().inputValue();
  out.calibratedUnits = [];
  for (const [id, label] of [['qlaunch', 'A12 28mm'], ['l08firm', 'MOUNT']]) {
    calibratedUnits.add(id);
    await page.evaluate(async (unitId) => {
      history.replaceState(null, '', `#/recipes?unit=${unitId}`);
      const { renderLutPage } = await import('/assets/lut.js');
      renderLutPage(document.getElementById('view'));
    }, id);
    const text = `为${label}生成完整配方`;
    await page.waitForFunction((name) => [...document.querySelectorAll('button')].some((b) => b.textContent === name && !b.disabled), text);
    assert(await page.getByText('为所选单元生成配方，结果是电脑预测。曝光沿用 50mm', { exact: true }).isVisible());
    out.calibratedUnits.push(id);
  }
  out.logLutUnits = await page.evaluate(async () => {
    const { api } = await import('/assets/api.js');
    const { loadLutLibrary, photoLutField } = await import('/assets/lut_library.js');
    const list = await loadLutLibrary();
    const cube = list.luts[0].cube_id;
    await api(`/api/luts/${cube}`, { method: 'PATCH', body: { input_space: 'vlog', output_space: 'srgb' } });
    await loadLutLibrary();
    const callbacks = { beginChange() {}, persist: async () => {}, endChange() {}, schedulePreview() {} };
    return [['A12 50mm', true], ['A12 28mm', true], ['GXR MOUNT 35mm', true], ['A12 28mm', false]].map(([lens, has_raw]) => {
      const field = photoLutField({ photo: { lens, has_raw }, ed: { lut: null } }, callbacks);
      return field.querySelector(`option[value="${cube}"]`).disabled;
    });
  });
  assert.deepEqual(out.logLutUnits, [false, false, false, true]);
  await page.setViewportSize({ width: 375, height: 812 });
  out.narrowOverflow = await page.locator('.lut-page').evaluate((el) => {
    const rect = el.getBoundingClientRect();
    return el.scrollWidth > el.clientWidth || rect.left < 0 || rect.right > innerWidth;
  });
  out.narrowOutliers = await page.evaluate(() => [...document.querySelectorAll('body *')]
    .filter((el) => el.getBoundingClientRect().right > innerWidth)
    .slice(0, 12).map((el) => ({ tag: el.tagName, id: el.id, class: el.className })));
  out.photoRequests = photoRequests;
  assert.equal(out.errors.length, 0, JSON.stringify(out.errors));
  console.log(JSON.stringify(out));
} finally {
  await browser.close();
}
