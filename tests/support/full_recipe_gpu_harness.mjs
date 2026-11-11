const post = (path, body) => fetch(path, { method: 'POST', body });
try {
  const { Engine } = await import('/engine.js');
  const cases = await (await fetch('/cases.json')).json();
  const engine = await Engine.create();
  if (!engine) throw new Error('no WebGPU adapter');
  for (const item of cases) {
    const source = await (await fetch(item.raw.url)).arrayBuffer();
    const baked = await (await fetch(item.bake.url)).arrayBuffer();
    await engine.addRaw(item.source, item.raw.layout, source, null);
    engine.addBake(item.bake.key, item.bake.meta, baked);
    engine.show(item.source);
    engine.setLook(item.look);
    engine.setGeometry(item.geometry);
    const result = await engine.readback();
    await post(`/result/${item.id}?w=${result.w}&h=${result.h}`, result.data);
  }
  const summary = { info: engine.info, cases: cases.length };
  await post('/done', JSON.stringify(summary));
  globalThis.__fullRecipeResult = summary;
} catch (error) {
  const summary = { error: String(error.stack || error) };
  await post('/done', JSON.stringify(summary));
  globalThis.__fullRecipeResult = summary;
}
