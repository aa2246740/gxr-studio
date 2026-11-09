// 第一帧就检查白色握柄的两条边，而非只检查仍能拖动的透明命中区。
function installProbe() {
  window.__cropPointerEvents = 0;
  for (const type of ['pointerdown', 'pointermove']) document.addEventListener(type, () => { window.__cropPointerEvents += 1; });
  window.__readCropHandles = () => {
    const box = document.querySelector('.crop-box');
    if (!box || !box.getBoundingClientRect().width) return null;
    const pv = box.parentElement.getBoundingClientRect();
    const rect = box.getBoundingClientRect();
    const grips = [...box.querySelectorAll('[data-h]')].map((grip) => {
      const b = grip.getBoundingClientRect();
      const s = getComputedStyle(grip);
      const left = grip.dataset.h.endsWith('l');
      const top = grip.dataset.h.startsWith('t');
      const vertical = parseFloat(left ? s.borderLeftWidth : s.borderRightWidth);
      const horizontal = parseFloat(top ? s.borderTopWidth : s.borderBottomWidth);
      const points = [
        [left ? b.left + vertical / 2 : b.right - vertical / 2, b.top + b.height / 2],
        [b.left + b.width / 2, top ? b.top + horizontal / 2 : b.bottom - horizontal / 2],
      ];
      return {
        corner: grip.dataset.h,
        visible: grip.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }),
        borders: [vertical, horizontal],
        // .pv 会裁掉外侧的边；命中透明区域不能证明白边可见。
        paintedInsidePhoto: points.map(([x, y]) => x >= pv.left && x < pv.right && y >= pv.top && y < pv.bottom),
        unobscured: points.map(([x, y]) => document.elementFromPoint(x, y) === grip),
      };
    });
    return {
      grips,
      box: [rect.x, rect.y, rect.width, rect.height],
      photo: [pv.x, pv.y, pv.width, pv.height],
      gpu: !!document.querySelector('.stage-area.gpu'),
      reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
      navigatorGpu: !!navigator.gpu,
      pointerEvents: window.__cropPointerEvents,
    };
  };
  window.__firstCropFrame = (action) => new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { observer.disconnect(); reject(new Error('裁切框没有出现')); }, 30000);
    const frame = () => {
      const result = window.__readCropHandles();
      if (!result) return;
      clearTimeout(timeout);
      observer.disconnect();
      resolve(result);
    };
    const observer = new MutationObserver(() => requestAnimationFrame(frame));
    observer.observe(document.querySelector('.stage-area'), { childList: true, subtree: true, attributes: true });
    action();
    requestAnimationFrame(frame);
  });
}

export async function walkCropHandles({ call, evalIn, origin, photoId, wantGpu, step }) {
  await call('Page.navigate', { url: `${origin}/?open=1#/photo/${photoId}` });
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  async function ready(id) {
    const started = Date.now();
    while (Date.now() - started < 60000) {
      if (await evalIn(`(async () => {
        const v = (await import('/assets/viewer.js')).currentView();
        const media = document.querySelector('.pv-gpu, .pv-img');
        return v?.photo.id === ${JSON.stringify(id)} && !!v.ed && !!media?.getBoundingClientRect().width
          && (!${wantGpu} || !!document.querySelector('.stage-area.gpu'));
      })()`)) return;
      await wait(50);
    }
    throw new Error(`照片预览没有就绪（gpu=${wantGpu}）`);
  }
  await ready(photoId);
  await evalIn(`(${installProbe.toString()})()`);
  const results = [];
  async function open(label) {
    step(label);
    results.push({ label, ...await evalIn("window.__firstCropFrame(() => document.querySelector('#tab-crop').click())") });
  }
  await open('first-open');
  await evalIn("document.querySelector('#tab-adjust').click()");
  await ready(photoId);
  await open('return-from-adjust');
  // 真正换照片，再打开裁切；不复用上一张照片的框。
  const other = await evalIn(`(async () => {
    const photos = await (await fetch('/api/photos')).json();
    return photos.find((p) => p.has_jpg && p.id !== ${JSON.stringify(photoId)}).id;
  })()`);
  await evalIn(`location.hash = '#/photo/${other}'`);
  await ready(other);
  await open('switch-photo');
  // 存一个触及左右边缘的框，再重载，覆盖「已有裁切」的首次打开。
  await evalIn(`(async () => {
    const { currentView } = await import('/assets/viewer.js');
    const { applyPreset } = await import('/assets/crop.js');
    await applyPreset(currentView(), { id: '16:9', label: '16:9' });
  })()`);
  await call('Page.reload');
  await ready(other);
  await evalIn(`(${installProbe.toString()})()`);
  await open('saved-crop');
  // 没有裁切时四条边都贴住照片，正是原来的白边被裁掉的情况。
  await evalIn(`(async () => {
    const v = (await import('/assets/viewer.js')).currentView();
    (await import('/assets/crop.js')).resetCropLocal(v);
    await (await import('/assets/preview.js')).resetCropRemote(v);
    document.querySelector('#tab-adjust').click();
  })()`);
  await ready(other);
  await open('no-crop');
  const before = results.at(-1).box;
  await call('Emulation.setDeviceMetricsOverride', { width: 640, height: 540, deviceScaleFactor: 1, mobile: false });
  step('resize-stage');
  const resized = await evalIn(`new Promise((resolve, reject) => {
    const deadline = performance.now() + 10000;
    const read = () => {
      const r = window.__readCropHandles();
      if (r && Math.abs(r.box[2] - ${before[2]}) > 1) resolve(r);
      else if (performance.now() > deadline) reject(new Error('舞台没有随窗口缩放'));
      else requestAnimationFrame(read);
    };
    requestAnimationFrame(read);
  })`);
  results.push({ label: 'resize-stage', ...resized });
  return results;
}
