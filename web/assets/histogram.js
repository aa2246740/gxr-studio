// 修图右侧面板顶部的直方图：红、绿、蓝和亮度，左右两个三角提示暗部死黑 / 高光死白，点三角或按 J 在照片上标出位置。
// 数的是导出时的样子（配方、LUT、调整、曲线、几何、裁剪之后；不含边框和时间戳），规则写在 histogram_math.js 和 docs/histogram.md。
//
// 更新：引擎每画完一帧就叫醒它；另外每 50 毫秒看一眼（服务器图换了、裁剪框动了）。有变化就数一次，两次开始之间至少隔 50 毫秒，最多每秒 20 次。
//   显卡路径：引擎在成片纹理上数，异步读回；上一次还没读完就跳过这一轮，绝不排队，不拖慢预览。
//   服务器图：解码服务器出的那张图，降采样后在浏览器里数。
//   裁剪页（画面是整张未裁切的照片）：只数裁剪框里面的，也就是将要导出的部分；拖框时直方图跟着变。
import { h, icon, modalOpen } from './dom.js';
import { gpuEngine } from './gpu_stage.js';
import { percentText } from './histogram_math.js';
import { drawHistogram } from './histogram_draw.js';
import { bitmapHistogram, clearOverlay, decodeImage, gpuHistogram, paintOverlay } from './histogram_sources.js';

const KEY = 'gxr-studio.histogram-collapsed';
const TICK_MS = 50; // 定时器的间隔
const MIN_GAP_MS = 45; // 两次开始数之间至少隔多久（定时器和引擎叫醒都受它限制）
const RETRY_MS = 2000; // 出错后隔多久再试

// 溢出提示盖不盖：本次打开期间一直记着，换照片不丢（Lightroom 也这样）
const clip = { shadow: false, highlight: false };
let latest = null; // 最近一个挂在大图上的，给测试看状态

// J 自己监听，不走全局快捷键：拖完滑块焦点还在滑块上，那时全局快捷键不工作，而"拖一下、按 J 看看"正是最常见的用法。
// 滑块、开关、按钮不吃字母键，只有真的在打字的地方才不响应。
const TEXT_INPUT = /^(text|search|email|url|tel|password|number|date|datetime-local|month|time|week)$/;
function typingIn(t) {
  if (!t) return false;
  if (t.isContentEditable || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT') return true;
  return t.tagName === 'INPUT' && TEXT_INPUT.test(t.type || 'text');
}
// 地址栏要去的照片 id（换照片时，新的一张加载完之前它和屏幕上的不是同一张）
function targetPhoto() {
  const m = /^#\/photo\/([^/?]+)/.exec(location.hash);
  return m ? decodeURIComponent(m[1]) : null;
}

function pref() {
  try { return localStorage.getItem(KEY) === '1'; } catch { return false; }
}
function setPref(on) {
  try { localStorage.setItem(KEY, on ? '1' : '0'); } catch { /* 隐私模式写不进去就算了 */ }
}

const SIDES = {
  shadow: { name: '暗部', colour: '蓝色', bad: '已经死黑，没有层次了', calm: '没有明显死黑', legend: '死黑', none: '没有死黑的点' },
  highlight: { name: '高光', colour: '红色', bad: '已经死白（过曝），没有层次了', calm: '没有明显死白', legend: '死白（过曝）', none: '没有死白的点' },
};

// 三角的提示：说清现在多少、点一下会怎样、快捷键
export function clipTitle(side, hist, pressed) {
  const s = SIDES[side];
  if (!hist) return `${s.name}提示：照片还没有读出来。`;
  const share = side === 'shadow' ? hist.shadowShare : hist.highlightShare;
  const lit = side === 'shadow' ? hist.shadowWarn : hist.highlightWarn;
  const act = pressed ? '再点一下关闭（J）' : `点一下，用${s.colour}标出${lit ? '它们' : '最接近的点'}（J）`;
  return lit
    ? `${s.name}：约 ${percentText(share)} 的点${s.bad}。${act}。`
    : `${s.name}：${s.calm}（约 ${percentText(share)}，达到 0.5% 才亮起）。${act}。`;
}

function clipButton(side) {
  const b = h('button', { type: 'button', class: 'hist-clip', 'data-side': side, 'data-lit': 'false', 'aria-pressed': 'false' }, h('span', { class: 'hist-tri', 'aria-hidden': 'true' }));
  return b;
}

export function createHistogram() {
  let collapsed = pref();
  const shadowBtn = clipButton('shadow');
  const highlightBtn = clipButton('highlight');
  const buttons = { shadow: shadowBtn, highlight: highlightBtn };
  const toggleBtn = h('button', { type: 'button', class: 'hist-toggle', 'aria-controls': 'hist-body' }, h('span', {}, '直方图'), icon('chevron-down'));
  const canvas = h('canvas', {
    class: 'hist-plot', role: 'img', 'aria-label': '直方图',
    title: '红、绿、蓝三种颜色（重叠处混色）和亮度（白线）的分布：左边是暗部，右边是高光。',
  });
  const body = h('div', { class: 'hist-body', id: 'hist-body' }, h('div', {}, canvas));
  const el = h('section', { class: 'hist', 'aria-label': '直方图' }, h('div', { class: 'hist-head' }, shadowBtn, toggleBtn, highlightBtn), body);

  const st = {
    view: null, timer: 0, busy: false, startedAt: 0, hook: null, lastKey: null, retryAt: 0, hist: null, whole: true, region: null, updates: 0, lastMs: 0, applied: '',
    bitmap: null, bitmapKey: null, overlayCanvas: null, legend: null, legendKey: '', observer: null, titles: {},
  };
  const handle = { el, attach, toggleClip, state: () => snapshot() };

  function snapshot() {
    const hist = st.hist;
    return {
      source: hist ? hist.source : null,
      mean: hist ? hist.mean : null,
      samples: hist ? hist.samples : 0,
      crushed: hist ? hist.crushed : 0,
      blown: hist ? hist.blown : 0,
      shadowShare: hist ? hist.shadowShare : 0,
      highlightShare: hist ? hist.highlightShare : 0,
      shadowWarn: !!(hist && hist.shadowWarn),
      highlightWarn: !!(hist && hist.highlightWarn),
      updates: st.updates,
      lastMs: st.lastMs,
      clip: { ...clip },
      collapsed,
      region: st.region,
      attached: !!st.view,
    };
  }

  // ---- 外观 ----

  function paintCollapsed() {
    el.dataset.collapsed = String(collapsed);
    toggleBtn.setAttribute('aria-expanded', String(!collapsed));
    toggleBtn.title = collapsed ? '展开直方图' : '收起直方图（三角提示仍然保留）';
    if (collapsed) body.setAttribute('inert', '');
    else body.removeAttribute('inert');
  }

  function setCollapsed(on) {
    collapsed = on;
    setPref(on);
    paintCollapsed();
    if (!on) drawNow();
  }

  function drawNow() {
    if (!collapsed) drawHistogram(canvas, st.hist);
  }

  function paintLegend() {
    const legend = st.legend;
    if (!legend) return;
    const sides = Object.keys(SIDES).filter((s) => clip[s]);
    legend.hidden = sides.length === 0;
    if (!sides.length) return;
    const hist = st.hist;
    const items = sides.map((s) => [s, st.whole && hist && !(s === 'shadow' ? hist.crushed : hist.blown) ? SIDES[s].none : SIDES[s].legend]);
    const key = JSON.stringify(items);
    if (key === st.legendKey) return; // 内容没变就不动 DOM（每次更新都会走到这里）
    st.legendKey = key;
    legend.replaceChildren(...items.map(([s, text]) => h('span', { class: 'clip-key' }, h('i', { class: `sw ${s}`, 'aria-hidden': 'true' }), text)));
  }

  function paintClip() {
    const hist = st.hist;
    for (const side of Object.keys(SIDES)) {
      const b = buttons[side];
      const lit = !!(hist && (side === 'shadow' ? hist.shadowWarn : hist.highlightWarn));
      b.dataset.lit = String(lit);
      b.setAttribute('aria-pressed', String(clip[side]));
      const title = clipTitle(side, hist, clip[side]);
      if (st.titles[side] !== title) {
        st.titles[side] = title;
        b.title = title;
        b.setAttribute('aria-label', title);
      }
    }
    paintLegend();
  }

  // ---- 溢出提示 ----

  const gpuShown = () => !!(st.view && st.view.stageArea && st.view.stageArea.classList.contains('gpu'));

  // 服务器图：叠在照片上的 canvas。画面重建（换图、进出裁剪）时重新放回去；左右对比时放在"原图"那半边的下面
  function mountOverlay() {
    const view = st.view;
    const pv = view && view.stageArea ? view.stageArea.querySelector('.pv') : null;
    const want = (clip.shadow || clip.highlight) && !gpuShown() && pv && pv.querySelector('.pv-img') && !view.showOriginal;
    if (!want) {
      st.overlayCanvas?.remove();
      return null;
    }
    st.overlayCanvas ??= h('canvas', { class: 'clip-overlay', 'aria-hidden': 'true' });
    const img = pv.querySelector('.pv-img');
    if (st.overlayCanvas.previousElementSibling !== img) img.after(st.overlayCanvas); // 紧跟在照片后面：裁剪框、左右对比的原图都在它上面
    return st.overlayCanvas;
  }

  function refreshOverlay() {
    const cv = mountOverlay();
    if (!cv) return;
    if (st.bitmap) paintOverlay(cv, st.bitmap, clip);
    else clearOverlay(cv);
  }

  function applyClip() {
    syncEngine();
    refreshOverlay();
    paintClip();
  }

  function syncEngine() {
    const eng = gpuEngine();
    const want = `${clip.shadow}${clip.highlight}`;
    if (eng && st.applied !== want) {
      eng.setClipOverlay({ shadow: clip.shadow, highlight: clip.highlight });
      st.applied = want;
    }
  }

  // J（和 Lightroom 一样）：任何一边开着就全关，否则两边都开
  function toggleClip() {
    if (!st.view) return false;
    const on = !(clip.shadow || clip.highlight);
    clip.shadow = on;
    clip.highlight = on;
    applyClip();
    return true;
  }

  for (const side of Object.keys(SIDES)) {
    buttons[side].addEventListener('click', () => {
      clip[side] = !clip[side];
      applyClip();
    });
  }
  toggleBtn.addEventListener('click', () => setCollapsed(!collapsed));
  paintCollapsed();
  paintClip();

  // ---- 更新 ----

  function regionOf(view) {
    const r = view.mode === 'uncropped' && view.crop && view.crop.rect;
    return r ? { x: r.x, y: r.y, w: r.w, h: r.h } : null;
  }

  // 这一轮要数什么：{ key, run } 或 null（没有新东西 / 还没准备好）。key 不变就不数。
  function plan(view) {
    const region = regionOf(view);
    const rk = region ? [region.x, region.y, region.w, region.h].map((v) => v.toFixed(4)).join(',') : '-';
    if (gpuShown()) {
      const eng = gpuEngine();
      if (!eng || eng.pending) return null; // 还有一帧没画出来：成片纹理是旧的，等它画完再数
      return { key: `gpu|${eng.contentVersion}|${view.mode}|${rk}`, region, run: () => gpuHistogram(eng, region) };
    }
    const ed = view.ed;
    const mode = view.mode;
    if (!ed || view.showOriginal || (mode !== 'final' && mode !== 'uncropped')) return null;
    const url = ed.urls && ed.urls[mode];
    if (!url) return null;
    // 数屏幕上这一张。边框和时间戳是浏览器另外画的，不在这张图里（也不在显卡的成片纹理里），所以直方图不会数到它们。
    // <img> 还没换成最新的（解码中）就等下一轮
    const img = view.stageArea.querySelector('.pv-img');
    if (!(img && img.src === url && img.complete && img.naturalWidth > 0)) return null;
    return { key: `srv|${url}|${rk}`, region, run: () => serverRun(view, { img, bitmapKey: url, region }) };
  }

  async function serverRun(view, p) {
    if (st.bitmapKey !== p.bitmapKey) {
      const bitmap = await decodeImage(p.img);
      if (st.view !== view) {
        bitmap.close();
        return null;
      }
      st.bitmap?.close();
      st.bitmap = bitmap;
      st.bitmapKey = p.bitmapKey;
    }
    const hist = bitmapHistogram(st.bitmap, p.region);
    refreshOverlay();
    return hist;
  }

  function accept(hist, region) {
    st.hist = hist;
    st.region = region;
    st.whole = !region;
    st.updates += 1;
    el.dataset.source = hist.source;
    el.dataset.mean = hist.mean.toFixed(2);
    el.dataset.updates = String(st.updates);
    paintClip();
    drawNow();
  }

  // 看一眼，有新东西就数一次。上一次没结束、离上一次开始不到 50 毫秒、页面在后台、出错后的冷却期里，都直接跳过
  function work(view) {
    const now = performance.now();
    if (st.busy || document.hidden || now < st.retryAt || now - st.startedAt < MIN_GAP_MS) return;
    syncEngine();
    if (!gpuShown()) mountOverlay();
    else st.overlayCanvas?.remove();
    const job = plan(view);
    if (!job || job.key === st.lastKey) return;
    st.busy = true;
    const t0 = (st.startedAt = performance.now());
    job.run().then((hist) => {
      if (st.view !== view || !hist) return;
      st.lastKey = job.key;
      st.lastMs = performance.now() - t0; // 从开始数到读回来用了多久
      accept(hist, job.region);
    }, () => {
      st.retryAt = performance.now() + RETRY_MS;
    }).finally(() => {
      st.busy = false;
    });
  }

  function tick() {
    const view = st.view;
    if (!view) return;
    if (view.disposed || !el.isConnected) {
      stop();
      return;
    }
    st.timer = setTimeout(tick, TICK_MS);
    hookFrames();
    work(view);
  }

  // 引擎每画完一帧（成片纹理刚提交）就来数一次，不等定时器：拖滑块时定时器赶上的多半是"下一帧参数已经改了、还没画"的空档
  function hookFrames() {
    const eng = gpuEngine();
    if (!eng || (st.hook && st.hook.eng === eng)) return;
    const prev = eng.onFrame;
    const fn = (info) => {
      if (prev) prev(info);
      if (st.view && !st.view.disposed) work(st.view);
    };
    eng.onFrame = fn;
    st.hook = { eng, prev, fn };
  }

  function onKey(e) {
    if ((e.key !== 'j' && e.key !== 'J') || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey || e.repeat) return;
    if (!st.view || typingIn(e.target) || modalOpen() || targetPhoto() !== st.view.photo.id) return;
    e.preventDefault();
    toggleClip();
  }

  function attach(view) {
    if (st.view) stop();
    st.view = view;
    st.lastKey = null;
    st.retryAt = 0;
    st.applied = '';
    latest = handle;
    const shell = view.stageArea.parentElement;
    st.legend = h('div', { class: 'clip-legend', hidden: true });
    st.legendKey = '';
    if (shell) shell.append(st.legend);
    st.observer = new MutationObserver(() => mountOverlay());
    st.observer.observe(view.stageArea, { childList: true });
    document.addEventListener('keydown', onKey);
    paintClip();
    tick();
  }

  function stop() {
    clearTimeout(st.timer);
    document.removeEventListener('keydown', onKey);
    if (st.hook && st.hook.eng.onFrame === st.hook.fn) st.hook.eng.onFrame = st.hook.prev;
    st.hook = null;
    st.observer?.disconnect();
    st.overlayCanvas?.remove();
    st.legend?.remove();
    st.bitmap?.close();
    Object.assign(st, { view: null, observer: null, legend: null, bitmap: null, bitmapKey: null, busy: false });
    if (latest === handle) latest = null;
  }

  return handle;
}

// 测试和排查用：当前直方图的状态（来源、平均亮度、溢出占比、更新次数、三角和提示的开关）
export function histogramState() {
  return latest ? latest.state() : null;
}
