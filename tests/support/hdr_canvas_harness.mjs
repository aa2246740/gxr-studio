// HDR 预览画布对拍的页面脚本。载入提交的 web/assets/gpu/engine.js，把一张 RGBA 图挂到真的 <canvas> 上，
// 先按 SDR 画一帧，再开 HDR 预览画一帧。每次都在 engine.frame() 的同一个任务里把画布的当前纹理拷出来
// （configure 时多加 COPY_SRC），发回测试进程。这里不解码：合成器怎么解码，由测试按 getConfiguration() 去做。
const post = (path, body) => fetch(path, { method: 'POST', body });
const progress = (phase, id = null, info = null) => post('/progress', JSON.stringify({ phase, id, info }));

async function bin(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} ${res.status}`);
  return res.arrayBuffer();
}

function half(h) {
  const exp = (h >> 10) & 0x1f;
  const frac = h & 0x3ff;
  const sign = h & 0x8000 ? -1 : 1;
  if (exp === 0) return sign * 2 ** -14 * (frac / 1024);
  if (exp === 31) return frac ? Number.NaN : sign * Infinity;
  return sign * 2 ** (exp - 15) * (1 + frac / 1024);
}

/** 让画布纹理可以拷出来，并记下引擎这一帧拿到的那张。 */
function instrument(canvas) {
  const ctx = canvas.getContext('webgpu');
  const configure = ctx.configure.bind(ctx);
  const current = ctx.getCurrentTexture.bind(ctx);
  const state = { tex: null };
  ctx.configure = (cfg) => configure({ ...cfg, usage: GPUTextureUsage.RENDER_ATTACHMENT | GPUTextureUsage.COPY_SRC });
  ctx.getCurrentTexture = () => (state.tex = current());
  return { ctx, state };
}

async function capture(engine, ctx, state) {
  state.tex = null;
  if (!engine.frame()) throw new Error('engine.frame() drew nothing');
  const tex = state.tex;
  if (!tex) throw new Error('engine.frame() did not take the canvas texture');
  const d = engine.device;
  const wide = tex.format === 'rgba16float';
  const bpr = Math.ceil((tex.width * (wide ? 8 : 4)) / 256) * 256;
  const buf = d.createBuffer({ size: bpr * tex.height, usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ });
  const enc = d.createCommandEncoder();
  enc.copyTextureToBuffer({ texture: tex }, { buffer: buf, bytesPerRow: bpr }, [tex.width, tex.height]);
  d.queue.submit([enc.finish()]);
  const got = ctx.getConfiguration?.() ?? null;
  const config = got && { format: got.format, colorSpace: got.colorSpace ?? null, toneMapping: got.toneMapping?.mode ?? null };
  await buf.mapAsync(GPUMapMode.READ);
  const mapped = buf.getMappedRange();
  const w = tex.width;
  const h = tex.height;
  let data;
  if (wide) {
    const raw = new Uint16Array(mapped);
    data = new Float32Array(w * h * 4);
    for (let y = 0; y < h; y++) for (let i = 0; i < w * 4; i++) data[y * w * 4 + i] = half(raw[y * (bpr / 2) + i]);
  } else {
    const raw = new Uint8Array(mapped);
    const bgra = tex.format.startsWith('bgra');
    data = new Uint8Array(w * h * 4);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = y * bpr + x * 4;
        const o = (y * w + x) * 4;
        data[o] = raw[i + (bgra ? 2 : 0)];
        data[o + 1] = raw[i + 1];
        data[o + 2] = raw[i + (bgra ? 0 : 2)];
        data[o + 3] = raw[i + 3];
      }
    }
  }
  buf.unmap();
  buf.destroy();
  return { w, h, format: tex.format, config, data };
}

const sendBytes = (path, a) => post(path, new Uint8Array(a.buffer, a.byteOffset, a.byteLength));

try {
  await progress('import');
  const { Engine } = await import('/engine.js');
  const cases = await (await fetch('/cases.json')).json();
  await progress('create');
  const engine = await Engine.create();
  if (!engine) {
    await post('/done', JSON.stringify({ error: 'no WebGPU adapter' }));
  } else {
    const report = { info: engine.info, dpr: window.devicePixelRatio, cases: {} };
    for (const c of cases) {
      await progress('source', c.id, engine.info);
      const rgba = new Uint8ClampedArray(await bin(c.image.url));
      const bitmap = await createImageBitmap(new ImageData(rgba, c.image.width, c.image.height), { colorSpaceConversion: 'none', premultiplyAlpha: 'none' });
      engine.addJpg(c.id, bitmap);
      bitmap.close();
      const canvas = document.createElement('canvas');
      canvas.style.cssText = `display:block;width:${c.canvas.width}px;height:${c.canvas.height}px`;
      document.body.append(canvas);
      const { ctx, state } = instrument(canvas);
      engine.show(c.id);
      engine.setClipOverlay(c.clip);
      engine.attach(canvas);
      await progress('sdr', c.id, engine.info);
      const sdr = await capture(engine, ctx, state);
      const codes = new Uint8Array(await bin(c.gain.url));
      engine.setOutput({ hdr: true, gain: { width: c.gain.width, height: c.gain.height, codes }, meta: c.meta, headroom: c.headroom });
      await progress('hdr', c.id, engine.info);
      const hdr = await capture(engine, ctx, state);
      engine.setOutput({ hdr: false });
      engine.detach();
      canvas.remove();
      report.cases[c.id] = { sdr: { format: sdr.format, config: sdr.config }, hdr: { format: hdr.format, config: hdr.config } };
      await sendBytes(`/result/${c.id}-sdr?w=${sdr.w}&h=${sdr.h}`, sdr.data);
      await sendBytes(`/result/${c.id}-hdr?w=${hdr.w}&h=${hdr.h}&fmt=f32`, hdr.data);
    }
    await post('/done', JSON.stringify(report));
  }
} catch (err) {
  await post('/done', JSON.stringify({ error: String((err && err.stack) || err) }));
}
