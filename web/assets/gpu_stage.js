// 大图的 WebGPU 实时预览（功能开关 gpu_preview，见 docs/feature-flags.md）。
// 引擎在 /assets/gpu/engine.js（frontend/ 构建，已提交）。数据只上传一次，拖滑块只改参数，不再请求服务器出图。
// 显卡做不了、或这张照片不支持的情况一律退回原来的服务器出图（preview.js 的 runPreview），不显示不对的画面：
//   原图（按住空格）、只有 JPG 却选了配方（近似）、
//   RAW 写法特殊（压缩、线性化表、逐行黑电平等），以及浏览器没有 WebGPU。
// 边框和时间戳不在其中：显卡只画照片，边框和日期戳由浏览器画在照片外面和上面（stage_finish.js），加边框不会让显卡退场。
//
// 版本：相机数据（camdata-changed）或 LUT（luts-changed）一变，代数加一，照片信息、配方表、LUT 表的记忆全部作废；
// 每个异步加载完成时先核对代数，旧代的成功和失败都不再写回（不会把旧表装进引擎，也不会把新数据标成失败）。

import { api } from './api.js';
import { state, on } from './state.js';
import { gpuPreference } from './gpu_pref.js';
import { normalGeometry, expands, warpValid, warpCanvas, exportSize } from './gpu_warp.js';

let enginePromise = null;
let engine = null;
let warpApi = null; // 引擎模块：透视 / 畸变的画布和有效区（geometry.py 的浏览器版，见 frontend/src/warp.ts）
let disabled = false; // 浏览器没有 WebGPU 或设备丢失：本次会话都走服务器
let canvas = null; // 唯一的画布，跨照片复用（不重建 WebGPU 上下文）
let generation = 0; // 数据代数
const infos = new Map(); // 照片 id → Promise<info>
const pending = new Map(); // 资源键 → Promise
const failed = new Set(); // 加载失败的资源键：这张照片这一组参数退回服务器
const boxes = new Map(); // 裁切框请求键 → { box, valid }
const prefetched = new Set(); // 已经预取过的 照片:rev:代数
const PREFETCH = 1; // 左右各预取几张
let lastRerun = null; // 设备丢失、数据换代时让当前照片重新走一遍
let lastPrefetch = null;

function bumpGeneration() {
  generation += 1;
  infos.clear();
  bakeMeta.clear();
  lutMeta.clear();
  failed.clear();
  prefetched.clear();
  lastPrefetch = null;
  lastRerun?.();
}
on('camdata-changed', bumpGeneration);
on('luts-changed', bumpGeneration);
// 设置里换了本机的显卡预览选择：当前照片马上按新选择重画
on('gpu-pref-changed', bumpGeneration);

// 开关在 /api/settings 里（启动时读）；关着就不请求 /gpu，也不建 WebGPU 设备
// 服务器开关开着，并且这台电脑没有在「设置 → 预览」里关掉
function flagOn() {
  return !!(state.settings && state.settings.gpu_preview === true) && gpuPreference();
}

function loadEngine() {
  if (!enginePromise) {
    enginePromise = (async () => {
      if (!('gpu' in navigator) || !navigator.gpu) return null;
      const mod = await import('./gpu/engine.js');
      const e = await mod.Engine.create();
      if (e) {
        warpApi = mod;
        e.onLost = () => {
          disabled = true;
          engine = null;
          lastRerun?.();
        };
      }
      return e;
    })().catch(() => null).then((e) => {
      engine = e;
      if (!e) disabled = true;
      return e;
    });
  }
  return enginePromise;
}

function photoInfo(id) {
  const key = `${id}@${generation}`;
  if (!infos.has(key)) {
    infos.set(key, api(`/api/photos/${id}/gpu`).catch((e) => {
      infos.delete(key);
      throw e;
    }));
  }
  return infos.get(key);
}

async function binary(url) {
  const res = await fetch(url, { credentials: 'same-origin' });
  if (!res.ok) throw new Error(`http_${res.status}`);
  return { data: await res.arrayBuffer(), headers: res.headers };
}

// 同一个键同时只加载一次。fn(current)：current() 为假说明已经换代，结果不能写回。
function once(key, fn) {
  const gen = generation;
  const current = () => gen === generation && !!engine;
  const tagged = `${key}@${gen}`;
  if (!pending.has(tagged)) {
    pending.set(tagged, fn(current).catch((e) => {
      if (current()) failed.add(key);
      throw e;
    }).finally(() => pending.delete(tagged)));
  }
  return pending.get(tagged);
}

// ---- 这张照片、这一组参数该用哪条路 ------------------------------------------------

const DISPLAY_SPACES = ['srgb', 'rec709', 'gamma24'];

// 与 finish/serve.py color_image（base=auto）和 lut/photo.py 的分支一一对应。返回 null = 退回服务器。
export function decide(ed, info, stage, recipe, lutSpace) {
  if (!ed || !ed.v2 || !info || !info.enabled) return null;
  if (stage !== 'final' && stage !== 'uncropped') return null;
  const raw = info.raw;
  const jpg = info.jpg;
  const hasDng = info.raw_reason !== 'no_raw';
  if (ed.lut) {
    if (!lutSpace) return null;
    if (!DISPLAY_SPACES.includes(lutSpace)) {
      // Log 类：任何单元的 RAW 都行，要有场景色彩（lut/photo.py：没有 DNG 是 lut_needs_raw）
      return raw && info.scene ? { base: 'raw', recipe: undefined, lut: ed.lut } : null;
    }
    if (recipe) return raw ? { base: 'raw', recipe, lut: ed.lut } : null;
    if (jpg) return { base: 'jpg', lut: ed.lut };
    return raw ? { base: 'raw', recipe: null, lut: ed.lut } : null;
  }
  if (recipe) return raw ? { base: 'raw', recipe, lut: null } : null; // 只有 JPG + 配方 = 近似：服务器
  if (jpg) return { base: 'jpg', lut: null };
  if (hasDng && raw) return { base: 'raw', recipe: null, lut: null };
  return null;
}

// ---- 资源 ---------------------------------------------------------------------

function sourceKey(id, info, base) {
  return base === 'raw' ? `raw:${id}:${info.raw.url}` : `jpg:${id}:${info.jpg.url}`;
}

function ensureSource(id, info, base) {
  const key = sourceKey(id, info, base);
  if (engine.hasSource(key)) return null;
  return once(key, async (current) => {
    if (base === 'raw') {
      const { data } = await binary(info.raw.url);
      if (current()) await engine.addRaw(key, info.raw, data, info.scene);
    } else {
      // 浏览器按 EXIF 方向解码（不做色彩转换）：实测与服务器 PIL 的 load_jpeg 逐字节相同
      const res = await fetch(info.jpg.url, { credentials: 'same-origin' });
      if (!res.ok) throw new Error(`http_${res.status}`);
      const bitmap = await createImageBitmap(await res.blob(), { imageOrientation: 'from-image', colorSpaceConversion: 'none', premultiplyAlpha: 'none' });
      if (current()) engine.addJpg(key, bitmap);
      bitmap.close();
    }
  });
}

// 链接里带相机数据版本：换了相机数据就是另一份表
function bakeUrl(id, recipe) {
  const dv = encodeURIComponent(String(state.camdata?.dataVersion ?? ''));
  return `/api/photos/${id}/bake?dv=${dv}${recipe ? `&recipe=${encodeURIComponent(JSON.stringify(recipe))}` : ''}`;
}

const bakeMeta = new Map(); // 请求链接 → 描述（键、标签、是否已标定）

function ensureBake(id, recipe) {
  const url = bakeUrl(id, recipe);
  const meta = bakeMeta.get(url);
  if (meta && engine.hasBake(meta.key)) return null;
  return once(url, async (current) => {
    const { data, headers } = await binary(url);
    if (!current()) return;
    const m = JSON.parse(decodeURIComponent(headers.get('X-GXR-Bake') || '{}'));
    if (!engine.hasBake(m.key)) engine.addBake(m.key, m, data);
    bakeMeta.set(url, m);
  });
}

const lutMeta = new Map(); // cube id → 描述（键里带 LUT 的版本）

function ensureLut(cubeId) {
  const meta = lutMeta.get(cubeId);
  if (meta && engine.hasLut(meta.key)) return null;
  return once(`lut:${cubeId}`, async (current) => {
    const { data, headers } = await binary(`/api/luts/${cubeId}/table`);
    if (!current()) return;
    const m = JSON.parse(decodeURIComponent(headers.get('X-GXR-Lut') || '{}'));
    if (!engine.hasLut(m.key)) engine.addLut(m.key, m, data);
    lutMeta.set(cubeId, m);
  });
}

// 颜色层（半分辨率 RAW 或相机 JPG）的宽高，和这张照片全尺寸（已转正）的宽高
function layerSize(info, base) {
  if (base === 'raw') return { w: info.raw.layer[0], h: info.raw.layer[1], fw: info.raw.full[0], fh: info.raw.full[1] };
  return null;
}

function boxKey(id, w, h, geometry, crop) {
  return JSON.stringify([id, w, h, geometry, crop]);
}

function ensureBox(id, w, h, geometry, crop) {
  const key = boxKey(id, w, h, geometry, crop);
  if (boxes.has(key)) return null;
  return once(`box:${key}`, async (current) => {
    const r = await api(`/api/photos/${id}/gpu/box`, { method: 'POST', body: { width: w, height: h, geometry, crop } });
    if (!current()) return;
    boxes.set(key, r);
    if (boxes.size > 64) boxes.delete(boxes.keys().next().value);
  });
}

// ---- 对外 ---------------------------------------------------------------------

// 返回 'gpu'（显卡接手，画面稍后出来）、'server'（走原来的服务器出图）或 'wait'（还在判断，稍后会再调一次 rerun）。
// ctx：{ stage, recipe, adjust, curve, geometry, crop, geometryKey }，由 preview.js 按当前草稿算好。
export function route(view, ctx, rerun) {
  if (disabled || !flagOn()) return 'server';
  lastRerun = rerun;
  const id = view.photo.id;
  const gen = generation;
  const info = view.gpuInfo;
  if (!info || view.gpuInfoId !== `${id}@${gen}`) {
    photoInfo(id).then((i) => {
      if (gen !== generation) return; // 已换代：新的一轮会自己再取
      view.gpuInfo = i;
      view.gpuInfoId = `${id}@${gen}`;
      rerun();
    }, () => {
      if (gen !== generation) return;
      view.gpuInfo = { enabled: false };
      view.gpuInfoId = `${id}@${gen}`;
      rerun();
    });
    return 'wait';
  }
  if (!info.enabled) return 'server';
  if (!engine) {
    loadEngine().then(() => rerun());
    return 'wait';
  }
  const ed = view.ed;
  // LUT 表第一次要在服务器上算几秒：这期间照常用服务器出图，表到了再切到显卡
  if (ed.lut && !lutMeta.has(ed.lut)) {
    if (!failed.has(`lut:${ed.lut}`)) ensureLut(ed.lut)?.then(rerun, rerun);
    view.gpuActive = null;
    return 'server';
  }
  const lutSpace = ed.lut ? (lutMeta.get(ed.lut).table8 ? 'srgb' : 'log') : null;
  const plan = decide(ed, info, ctx.stage, ctx.recipe, lutSpace);
  if (!plan) return 'server';
  const loads = [];
  const sKey = sourceKey(id, info, plan.base);
  if (failed.has(sKey)) return 'server';
  const needBake = plan.base === 'raw' && plan.recipe !== undefined;
  const bUrl = needBake ? bakeUrl(id, plan.recipe) : null;
  if (bUrl && failed.has(bUrl)) return 'server';
  loads.push(ensureSource(id, info, plan.base));
  if (needBake) loads.push(ensureBake(id, plan.recipe));
  if (plan.lut) loads.push(ensureLut(plan.lut));
  const live = loads.filter(Boolean);
  if (live.length) {
    Promise.all(live).then(() => { if (gen === generation) rerun(); }, () => { if (gen === generation) rerun(); });
    // 同一张照片只是换了配方表或 LUT：先留着上一帧，数据到了再画；换了照片才清掉
    if (!(view.gpuActive && engine.currentKey() === sKey)) view.gpuActive = null;
    return 'wait';
  }
  // 源图已在显存：按颜色层尺寸要裁切框（与 Python 在同一尺寸上裁出来的位置相同）。全部到齐之前不切换画面。
  const layer = engine.sourceSize(sKey);
  const box = boxFor(view, id, layer, ctx, rerun);
  if (box === 'wait') return 'wait';
  if (box === 'server') return 'server';
  engine.show(sKey);
  const bake = bUrl ? bakeMeta.get(bUrl) : null;
  engine.setLook({
    recipe: bake ? bake.key : null,
    lut: plan.lut ? lutMeta.get(plan.lut).key : null,
    lutStrength: 1,
    adjust: ctx.adjust,
    curve: ctx.curve,
  });
  const g = normalGeometry(ctx.geometry);
  engine.setGeometry({ ...g, box: ctx.stage === 'final' ? box.box : null });
  if (ctx.stage === 'uncropped' && box.valid) {
    const [x, y, w, h] = box.valid;
    ed.valid = { x, y, w, h };
    ed.validKey = ctx.geometryKey;
  }
  // 小标签（与服务器出图的 X-GXR-Label 相同的取值）
  if (plan.base === 'jpg' && !plan.lut) {
    ed.label = 'original';
    ed.calibrated = null;
  } else {
    ed.label = 'prediction';
    ed.calibrated = plan.lut ? false : bake ? !!bake.calibrated : null;
  }
  view.gpuActive = ctx.stage;
  view.gpuFull = full(info, plan.base, layer, g);
  // 导出时（已裁切）照片的像素：边框和时间戳的版面按它算（stage_finish.js），浏览器自己画，不请服务器
  const grow = plan.base === 'raw' ? info.raw.full[0] / info.raw.layer[0] : 1;
  view.gpuExport = exportSize(engine.finalSize(), grow, g, view.gpuFull, expands(g) ? engine.canvasSize() : null);
  ed.error = null;
  ed.busy = false;
  prefetch(view);
  return 'gpu';
}

function boxFor(view, id, layer, ctx, rerun) {
  // 成片要裁切框；裁剪页（未裁切）也要有效区，给裁切框的约束用
  const g = normalGeometry(ctx.geometry);
  const crop = ctx.stage === 'final' ? ctx.crop || null : null;
  const identity = !g.straighten && !expands(g) && !(g.rotate90 % 4) && !g.flip;
  const plain = !crop || (crop.rect && crop.rect.join(',') === '0,0,1,1');
  if (identity && plain) return { box: null, valid: [0, 0, 1, 1] };
  // 透视 / 畸变，裁剪页：拖滑块每一步都是新的几何，有效区在浏览器里算，不等服务器
  if (expands(g) && ctx.stage !== 'final' && warpApi) return { box: null, valid: warpValid(warpApi, g, layer.w, layer.h) };
  const key = boxKey(id, layer.w, layer.h, g, crop);
  const hit = boxes.get(key);
  if (hit) return hit;
  if (failed.has(`box:${key}`)) return 'server';
  ensureBox(id, layer.w, layer.h, g, crop).then(rerun, rerun);
  return 'wait';
}

// 全尺寸（已转正、按 90° 旋转交换宽高）：裁剪页显示的像素数要和导出一致
function full(info, base, layer, g) {
  const swap = (g.rotate90 || 0) % 2 === 1;
  const size = base === 'raw' ? layerSize(info, base) : { fw: layer.w, fh: layer.h };
  if (expands(g) && warpApi) return warpCanvas(warpApi, g, size.fw, size.fh); // 透视 / 畸变扩大画布：按全尺寸算（和导出同一个 _expanded_bounds）
  return swap ? { w: size.fh, h: size.fw } : { w: size.fw, h: size.fh };
}

// 左右相邻的照片先把源图传上显卡，换照片时只需换绑定
// 每张照片在每一代只预取一次（不在每次滑块输入时重发请求）；相邻照片的 rev 变了才重取它的配方表
function prefetch(view) {
  const here = `${view.photo.id}@${generation}`;
  if (lastPrefetch === here) return;
  lastPrefetch = here;
  const list = state.photos || [];
  const at = list.findIndex((p) => p.id === view.photo.id);
  if (at < 0) return;
  for (let d = -PREFETCH; d <= PREFETCH; d++) {
    const p = list[at + d];
    if (!d || !p) continue;
    const tag = `${p.id}:${p.sidecar_rev || 0}@${generation}`;
    if (prefetched.has(tag)) continue;
    prefetched.add(tag);
    photoInfo(p.id).then((info) => {
      if (!engine || !info.enabled) return;
      // 编辑过（有配方或 LUT）的照片多半用 RAW；没编辑过、有相机 JPG 的用 JPG
      const base = info.raw && ((p.sidecar_rev || 0) > 0 || !info.jpg) ? 'raw' : info.jpg ? 'jpg' : null;
      if (base) ensureSource(p.id, info, base)?.catch(() => {});
      // 配方表：服务器第一次烘焙一个条目要 0.1–0.2 秒，先替下一张做掉
      if (base === 'raw') {
        api(`/api/photos/${p.id}/edit`).then((edit) => {
          const r = edit && edit.recipe;
          const body = r ? { kind: r.kind || 'builtin', ...(r.id ? { id: r.id } : {}), unit: r.unit, slot: r.slot, levels: r.levels || {} } : null;
          ensureBake(p.id, body)?.catch(() => {});
        }).catch(() => {});
      }
    }).catch(() => {});
  }
}

// 大图区里的画布：唯一一个，按成片的宽高比放进可用区域
// natural：100% 显示（一个画面像素对一个屏幕像素），放不下由画面区滚动
export function gpuCanvas(view, maxW, maxH, { natural = false } = {}) {
  if (!engine) return null;
  if (!canvas) {
    canvas = document.createElement('canvas');
    canvas.className = 'pv-gpu';
  }
  const size = engine.finalSize();
  if (!size) return null;
  const scale = natural ? 1 / (globalThis.devicePixelRatio || 1) : Math.min(maxW / size.w, maxH / size.h);
  canvas.style.width = `${Math.max(1, Math.floor(size.w * scale))}px`;
  canvas.style.height = `${Math.max(1, Math.floor(size.h * scale))}px`;
  canvas.setAttribute('aria-label', view.photo.name);
  engine.attach(canvas);
  engine.redraw();
  return canvas;
}

export function gpuEngineForTests() {
  return engine;
}

// 显卡画布现在的画面（成片，只有照片，不含边框）：重画一帧，在同一个任务里交给 use(canvas)——WebGPU 画布的内容只在提交的那个任务里有效。
// 边框卡片用它缩成小底图（finish_cards.js）。画布就是显示的大小，比把整张成片读回 CPU 快得多（大图要一秒多）。
// 显卡不在、或画布没挂在页面上时返回 null。
export function gpuSnapshot(use) {
  if (!engine || !canvas || !canvas.isConnected || !canvas.clientWidth) return null;
  if (!engine.frame()) return null;
  return use(canvas);
}

// 直方图要在引擎的成片纹理上数（histogram.js）；引擎还没建好、或这台电脑走服务器出图时是 null
export function gpuEngine() {
  return engine;
}
