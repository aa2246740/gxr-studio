// 卡片用真实导出版面，CSS 必须把整张画布放进固定高度的卡槽。CI 同样关闭 WebGPU、减少动态效果。
async function inPage() {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const raf = () => new Promise((resolve) => requestAnimationFrame(resolve));
  const until = async (fn) => {
    const deadline = performance.now() + 30000;
    while (performance.now() < deadline) {
      const result = await fn();
      if (result) return result;
      await wait(25);
    }
    throw new Error('边框卡片检查等待超时');
  };
  const { currentView } = await import('/assets/viewer.js');
  const { photoSize } = await import('/assets/output_photo_size.js');
  const { metrics, loadFont } = await import('/assets/finish_fonts.js');
  const { frameLayout } = await import('/assets/finish_layout.js');
  const { flushPhotos } = await import('/assets/saves.js');
  await Promise.all(['atkinson', 'inter_sb', 'inter', 'courier', 'patrick', 'dseg'].map(loadFont));
  const response = await fetch('/api/photos');
  if (!response.ok) throw new Error('无法读取夹具照片');
  const listed = await response.json();
  const photos = listed.photos || listed.items || listed;
  const find = (name) => photos.find((photo) => photo.name === name);
  const cases = [
    { kind: 'landscape', photo: find('R0000102'), preset: 'original', rect: [0, 0, 1, 1] },
    { kind: 'portrait', photo: find('R0000105'), preset: 'original', rect: [0, 0, 1, 1] },
    { kind: 'xpan', photo: find('R0000103'), preset: 'xpan-65-24', rect: [0, 0.25, 1, 0.49] },
  ];
  const styles = ['none', 'leica', 'gallery', 'hasselblad', 'instant', 'film'];
  const { selectFrame } = await import('/assets/finish.js');
  const card = (style) => document.querySelectorAll('.fcard')[styles.indexOf(style)];
  const canvases = () => [...document.querySelectorAll('.fcard canvas')];
  const box = (node) => {
    const rect = node.getBoundingClientRect();
    return { x: rect.x, y: rect.y, w: rect.width, h: rect.height };
  };
  const signature = (canvas) => {
    const bytes = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    let hash = 2166136261;
    for (const byte of bytes) hash = Math.imul(hash ^ byte, 16777619);
    return hash >>> 0;
  };
  const photoPixels = (canvas, layout, w, h) => {
    const scale = canvas.width / (layout ? layout.width : w);
    const [x, y] = layout ? layout.photo : [0, 0];
    const probe = document.createElement('canvas');
    probe.width = probe.height = 16;
    const ctx = probe.getContext('2d');
    ctx.drawImage(canvas, (x + w * 0.1) * scale, (y + h * 0.1) * scale, w * 0.7 * scale, h * 0.7 * scale, 0, 0, 16, 16);
    return ctx.getImageData(0, 0, 16, 16).data;
  };
  const quiet = () => until(() => window.__fetchLog.inflight === 0 && performance.now() - window.__fetchLog.lastAt > 800);
  const out = { samples: [], updates: [], captionUpdates: [], navigation: [], reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches, gpu: !!navigator.gpu };
  for (const item of cases) {
    if (!item.photo) throw new Error('缺少 ' + item.kind + ' 夹具');
    location.hash = '#/photos';
    await until(() => !currentView());
    // 用真实 API 设置裁剪，打开照片后仍通过用户界面改样式、比例、颜色和宽度。
    const crop = await fetch('/api/photos/' + item.photo.id + '/crop', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preset: item.preset, rect: item.rect }),
    });
    if (!crop.ok) throw new Error('无法设置裁剪 ' + crop.status + ' ' + await crop.text());
    const edit = await fetch('/api/photos/' + item.photo.id + '/edit', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ frame: { style: 'gallery', text: 'Local' },
        stamp: { enabled: true, content: 'text', text: 'Paris', font: 'typewriter', glow: false, placement: 'border' } }),
    });
    if (!edit.ok) throw new Error('无法设置边框和日期戳 ' + edit.status);
    location.hash = '#/photo/' + item.photo.id;
    await until(() => currentView()?.photo.id === item.photo.id && currentView()?.ed && !currentView().ed.loading);
    document.querySelector('#tab-frame').click();
    await until(() => canvases().length === 6);
    await photoSize(currentView()).promise;
    await quiet();
    out.navigation.push({ kind: item.kind, key: currentView().cardBase.key, id: item.photo.id, signature: signature(canvases()[0]) });
    card('gallery').click();
    await until(() => document.querySelector('.pv.framed'));
    for (const ratio of ['1:1', '4:5', '3:4', '2:3', '9:16', '16:9', '3:2', 'original']) {
      const renders = window.__fetchLog.renders;
      const start = performance.now();
      const select = document.querySelector('select[aria-label="成品比例"]');
      select.value = ratio;
      select.dispatchEvent(new Event('change', { bubbles: true }));
      await raf();
      const ms = performance.now() - start;
      const view = currentView();
      const [w, h] = photoSize(view).value.photo;
      const reference = photoPixels(canvases()[0], null, w, h);
      const sample = { kind: item.kind, ratio, photo: [w, h], meta: view.ed.meta.frame, cards: [], stages: [], ms };
      for (const style of styles) {
        const button = card(style);
        const canvas = button.querySelector('canvas');
        const holder = button.querySelector('.thumb');
        const layout = style === 'none' ? null : frameLayout(w, h, style === view.ed.frame.style ? view.ed.frame : selectFrame(view.ed.frame, style), view.ed.meta.frame, metrics);
        const pixels = photoPixels(canvas, layout, w, h);
        const photoError = pixels.reduce((sum, value, i) => sum + Math.abs(value - reference[i]), 0) / pixels.length;
        sample.cards.push({ style, bitmap: [canvas.width, canvas.height], content: box(canvas), box: box(holder),
          photoError, objectFit: getComputedStyle(canvas).objectFit,
          background: getComputedStyle(holder).backgroundColor, outline: getComputedStyle(canvas).boxShadow });
      }
      // 每种样式选中时，卡片和舞台都显示同一张成片。
      for (const style of styles.slice(1)) {
        card(style).click();
        await raf();
        const selected = document.querySelector('.fcard[aria-pressed="true"] canvas');
        sample.stages.push({ style, card: box(selected), stage: box(document.querySelector('.pv.framed')) });
      }
      sample.spec = { ...view.ed.frame };
      sample.clickRenders = window.__fetchLog.renders - renders;
      out.samples.push(sample);
    }
    card('gallery').click();
    await quiet();
    for (const [label, expected] of [['黑', [14, 14, 16, 255]], ['米白', [244, 239, 230, 255]], ['白', [255, 255, 255, 255]]]) {
      const before = canvases().map(signature);
      const renders = window.__fetchLog.renders;
      const start = performance.now();
      document.querySelector('.sw[aria-label="' + label + '"]').click();
      await raf();
      const ms = performance.now() - start;
      const after = canvases().map(signature);
      out.updates.push({ kind: item.kind, action: label, ms, changed: styles.filter((_, i) => before[i] !== after[i]),
        corner: [...card('gallery').querySelector('canvas').getContext('2d').getImageData(0, 0, 1, 1).data], expected,
        clickRenders: window.__fetchLog.renders - renders });
    }
    for (const label of ['细', '中', '粗']) {
      const renders = window.__fetchLog.renders;
      const start = performance.now();
      [...document.querySelectorAll('.seg button')].find((node) => node.textContent === label).click();
      await raf();
      const view = currentView();
      const [w, h] = photoSize(view).value.photo;
      const expected = frameLayout(w, h, view.ed.frame, view.ed.meta.frame, metrics);
      const canvas = card('gallery').querySelector('canvas');
      const shown = box(canvas);
      out.updates.push({ kind: item.kind, action: label, ms: performance.now() - start,
        aspect: shown.w / shown.h, expectedAspect: expected.width / expected.height,
        clickRenders: window.__fetchLog.renders - renders });
    }
    card('leica').click();
    await quiet();
    const before = canvases().map(signature);
    const renders = window.__fetchLog.renders;
    const input = document.querySelector('input[aria-label="文字"]');
    const caption = 'Harbour walk';
    input.focus();
    input.value = '  ' + caption + '  ';
    const drawn = new Map();
    const prototype = CanvasRenderingContext2D.prototype;
    const fillText = prototype.fillText;
    prototype.fillText = function(text, ...args) {
      if (!drawn.has(this.canvas)) drawn.set(this.canvas, []);
      drawn.get(this.canvas).push(text);
      return fillText.call(this, text, ...args);
    };
    try {
      input.dispatchEvent(new Event('input', { bubbles: true }));
      await raf();
    } finally {
      prototype.fillText = fillText;
    }
    const after = canvases().map(signature);
    const typed = { changed: styles.filter((_, i) => before[i] !== after[i]),
      painted: styles.filter((_, i) => (drawn.get(canvases()[i]) || []).join('').replace(/\s/g, '').includes('Harbourwalk')),
      focusKept: document.activeElement === input };
    // 等停笔自动保存，再换样式；面板没重搭过，按钮也必须读取最新文字。
    await until(() => currentView().ed.frame.text === caption);
    await flushPhotos([item.photo.id]);
    const readFrame = async () => (await (await fetch('/api/photos/' + item.photo.id + '/edit')).json()).frame;
    const savedBefore = await readFrame();
    card('hasselblad').click();
    await raf();
    const shown = document.querySelector('input[aria-label="文字"]').value;
    await flushPhotos([item.photo.id]);
    out.captionUpdates.push({ kind: item.kind, ...typed, shown, savedBefore, savedAfter: await readFrame(),
      clickRenders: window.__fetchLog.renders - renders });
  }
  return out;
}

export async function walkFrameCards({ evalIn }) {
  return evalIn(`(${inPage.toString()})()`, 120000);
}
