// 真浏览器驱动脚本共用：显卡预览没有接手时，把判断「为什么」要用到的东西交出去。
// 内容和 viewer_browser.mjs 里「collect WebGPU diagnostics」那一步相同（开关、本机选择、适配器、这张照片的 /gpu、引擎能不能建、
// 页面自己那台引擎有没有），再加 Chrome 日志里的两样：和显卡有关的最后 30 行，以及「画布的共享图像建不起来」这一种错误
// （Linux 无头 Chrome 的软件显卡：设备建得起来，但 WebGPU 画布显示不出来，页面的引擎随即失效，退回服务器出图）。
// Python 那边的 cannot_present()（tests/test_viewer_browser.py）据此决定 Linux 上跳过、macOS 强制跑。
import { readFileSync } from 'node:fs';

// 画布上不了屏时 Chrome 日志里的错：共享图像建不起来（Unable to create shared image），或者取不到（ProduceMemory … non-existent mailbox，
// 软件显卡上画布会短暂出现、引擎还活着、画面却一直出不来时是这一种）。两个驱动脚本都用它。
export const SHARED_IMAGE_ERROR = /Unable to create shared image|SharedImageBackingFactory|SharedImageManager::ProduceMemory|non-existent mailbox/;

// Chrome 的日志文件：尾部、整份、和显卡有关的行。整份日志只用来查有没有某一种错误（只看尾部会漏掉早先的报错）。
export function chromeLogReader(path) {
  const read = () => {
    try {
      return readFileSync(path, 'utf8');
    } catch {
      return '';
    }
  };
  return {
    tail: (n = 3000) => read().slice(-n),
    full: read,
    gpuLines: () => read().split('\n').filter((l) => /gpu|webgpu|dawn|swiftshader|vulkan|watchdog|lost|context|shared ?image|mailbox/i.test(l)).slice(-30).map((l) => l.slice(0, 300)),
    sharedImageError: () => SHARED_IMAGE_ERROR.test(read()),
  };
}

// 在页面里跑的诊断（返回 Promise 的表达式）。photoId：要看的这张照片。
export const diagnosticsExpression = (photoId) => `(async () => {
  const d = { navigatorGpu: !!navigator.gpu, pref: localStorage.getItem('gxr-studio.gpu-preview') };
  try { d.flag = (await (await fetch('/api/settings')).json()).gpu_preview; } catch (e) { d.settingsError = String(e); }
  try { const g = await (await fetch('/api/photos/${photoId}/gpu')).json(); d.photo = { enabled: g.enabled, keys: Object.keys(g).slice(0, 24) }; } catch (e) { d.photoError = String(e); }
  try {
    const a = navigator.gpu ? await navigator.gpu.requestAdapter({ powerPreference: 'high-performance' }) : null;
    d.adapter = a ? { vendor: a.info?.vendor, architecture: a.info?.architecture, description: a.info?.description, fallback: a.info?.isFallbackAdapter } : null;
  } catch (e) { d.adapterError = String(e); }
  try { const m = await import('/assets/gpu/engine.js'); d.engine = (await m.Engine.create()) ? 'created' : 'null'; } catch (e) { d.engineError = String(e).slice(0, 300); }
  // 页面自己的那台引擎（和页面同一个模块实例）：没建起来，还是建了又丢了（设备丢失后整个会话都走服务器）
  try {
    const st = await import('/assets/gpu_stage.js');
    const e = st.gpuEngineForTests();
    d.pageEngine = e ? { lost: !!e.lost, currentKey: typeof e.currentKey === 'function' ? e.currentKey() : null } : null;
  } catch (e) { d.pageEngineError = String(e).slice(0, 300); }
  d.stage = { classes: document.querySelector('.stage-area, .stage')?.className || null, img: !!document.querySelector('.pv-img'), canvas: !!document.querySelector('canvas') };
  return d;
})()`;
