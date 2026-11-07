// 显卡直方图测试的页面脚本：载入提交的 web/assets/gpu/engine.js，把无损 PNG 当"相机 JPG"传上显卡（颜色层原样通过），
// 读回成片纹理（1:1）发回测试进程当 numpy 的参考；再让显卡数直方图、读回带溢出提示的显示结果，一并放进总结。
// 不访问工作室接口，数据由 tests/gpu_support.py 提供。
import { Engine } from '/engine.js';

const post = (path, body) => fetch(path, { method: 'POST', body });
const IDENTITY = { recipe: null, lut: null, lutStrength: 1, adjust: {}, curve: null };
const FLAT = { straighten: 0, rotate90: 0, flip: false, box: null };

try {
  const cases = await (await fetch('/cases.json')).json();
  const engine = await Engine.create();
  if (!engine) {
    await post('/done', JSON.stringify({ error: 'no WebGPU adapter' }));
  } else {
    const hist = {};
    for (const c of cases) {
      const bitmap = await createImageBitmap(await (await fetch(c.image)).blob(), { imageOrientation: 'from-image', colorSpaceConversion: 'none', premultiplyAlpha: 'none' });
      engine.addJpg(c.id, bitmap);
      bitmap.close();
      engine.show(c.id);
      engine.setLook(IDENTITY);
      engine.setGeometry(FLAT);
      const rb = await engine.readback();
      await post(`/result/${c.id}?w=${rb.w}&h=${rb.h}`, rb.data);
      const h = await engine.histogram(c.region || null, c.maxSamples || undefined);
      // 同时发两次：第二次必须被跳过（返回 null），不排队
      const [a, b] = await Promise.all([engine.histogram(c.region || null, c.maxSamples || undefined), engine.histogram(c.region || null)]);
      hist[c.id] = {
        counts: Array.from(h.counts), samples: h.samples, stride: h.stride, rect: h.rect,
        again: a ? Array.from(a.counts) : null, skipped: b === null,
      };
      if (c.present) {
        engine.setClipOverlay(c.overlay);
        const pr = await engine.readPresent(c.present[0], c.present[1]);
        await post(`/result/${c.id}.present?w=${pr.w}&h=${pr.h}`, pr.data);
      }
    }
    await post('/done', JSON.stringify({ info: engine.info, hist }));
  }
} catch (err) {
  await post('/done', JSON.stringify({ error: String((err && err.stack) || err) }));
}
