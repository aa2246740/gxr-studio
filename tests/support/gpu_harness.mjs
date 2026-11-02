// 显卡一致性测试的页面脚本：载入提交的 web/assets/gpu/engine.js，按 /cases.json 逐个设置数据和参数，
// 读回成片纹理（1:1）发回测试进程。不访问工作室接口，所有数据由 tests/gpu_support.py 提供。
import { Engine } from '/engine.js';

const post = (path, body) => fetch(path, { method: 'POST', body });

async function bin(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} ${res.status}`);
  return res.arrayBuffer();
}

try {
  const cases = await (await fetch('/cases.json')).json();
  const engine = await Engine.create();
  if (!engine) {
    await post('/done', JSON.stringify({ error: 'no WebGPU adapter' }));
  } else {
    for (const c of cases) {
      if (!engine.hasSource(c.source)) {
        if (c.raw) await engine.addRaw(c.source, c.raw.layout, await bin(c.raw.url), c.raw.scene || null);
        else {
          const blob = await (await fetch(c.jpg)).blob();
          const bitmap = await createImageBitmap(blob, { imageOrientation: 'from-image', colorSpaceConversion: 'none', premultiplyAlpha: 'none' });
          engine.addJpg(c.source, bitmap);
          bitmap.close();
        }
      }
      if (c.bake && !engine.hasBake(c.bake.key)) engine.addBake(c.bake.key, c.bake.meta, await bin(c.bake.url));
      if (c.lut && !engine.hasLut(c.lut.key)) engine.addLut(c.lut.key, c.lut.meta, await bin(c.lut.url));
      engine.show(c.source);
      engine.setLook(c.look);
      engine.setGeometry(c.geometry);
      const rb = await engine.readback();
      await post(`/result/${c.id}?w=${rb.w}&h=${rb.h}`, rb.data);
    }
    await post('/done', JSON.stringify({ info: engine.info }));
  }
} catch (err) {
  await post('/done', JSON.stringify({ error: String((err && err.stack) || err) }));
}
