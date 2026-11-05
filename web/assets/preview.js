// 修图的编辑状态与预览（每张照片一份，挂在 view 上：view.ed、view.crop）。
// 读：GET /edit、GET /crop。写：PUT /edit、PUT /crop，全部排在 view.saveQ 里一个一个来，共用一个 rev。
// 预览：POST /render，请求体带草稿（还没保存的滑块值也能看到效果）。
// 两套后端：ed.v2 为真（探测到 /api/frames）时带 stage/size 和几何、边框、时间戳；否则走旧接口（只认配方、调整、曲线、拉直）。

import { api, apiBlob } from './api.js';
import { state, on } from './state.js';
import { saveRecord, writeSave, clearFailed, flushPhotos, readFresh } from './saves.js';
export { flushPhotos, saveStatus, retrySaves, SaveError } from './saves.js';
import { toast, toastError } from './dom.js';
import { applyCamdataStatus } from './camdata.js';
import { isPrediction, lookNoteText, originalNoteText } from './look_notes.js';
import { loadFrames, loadDefaults, loadPhotoCaps, loadMeta, fallbackCaps } from './caps.js';
import { resolveFinish, DEFAULT_GEOMETRY, frameBody } from './finish.js';
import { legacyValid, fromArray, toArray } from './geom.js';
import { route as gpuRoute } from './gpu_stage.js';

export const CURVE_MAX = 16;
const PREVIEW_EDGE = 800; // 旧接口：拖动时的小图长边
const DEBOUNCE_MS = 120;

// 8 个底子的名字（与 UX.md 3.6 一致；28mm / MOUNT 的底子名字沿用同一套）
// 配方位置的默认名（/api/recipes 没给名字时用）。5–8 号在相机里是用户可以改的位置，不写别家的名字（U7）。
export const BASE_NAMES = ['GR 正片', 'GR 负片', 'GR 高对比黑白', '原厂标准', '空位 5', '空位 6', '空位 7', '空位 8'];
// 配方名：/api/recipes 给了名字就用它，没给按位置号取默认名
export function slotName(unitId, slot) {
  const unit = state.catalog && state.catalog.units ? state.catalog.units.find((u) => u.id === unitId) : null;
  const base = unit && unit.recipes ? unit.recipes.find((b) => b.slot === slot) : null;
  const name = (base && base.name) || BASE_NAMES[slot - 1] || `空位 ${slot}`;
  return `${String(slot).padStart(2, '0')} ${name}`;
}

export const KNOB_NAMES = {
  amount: '强度',
  contrast: '对比度',
  highlight: '高光',
  shadow: '阴影',
  fade: '褪色',
  saturation: '饱和度',
  warmth: '色温',
  tint: '色调',
  split: '分离色调',
  filter: '黑白滤镜',
};

// 照片的镜头 → 单元（与后端 unit_of_lens 一致；认不出就返回 null，由调用方退回 50mm）
export function unitForPhoto(photo) {
  const text = `${photo.lens || ''} ${photo.unit || ''}`.toLowerCase();
  if (text.includes('mount')) return 'l08firm';
  if (text.includes('50mm')) return 'jlaunch';
  if (text.includes('28mm')) return 'qlaunch';
  return null;
}

let catalogPromise = null;
let catalogVersion = 0;
on('camdata-changed', () => {
  state.catalog = null;
  catalogPromise = null;
  catalogVersion++;
});
export function loadCatalog({ refresh = false } = {}) {
  if (refresh) {
    state.catalog = null;
    catalogPromise = null;
    catalogVersion++;
  }
  if (state.catalog) return Promise.resolve(state.catalog);
  if (!catalogPromise) {
    const version = catalogVersion;
    catalogPromise = api('/api/recipes').then(
      (r) => {
        if (version !== catalogVersion) return loadCatalog();
        state.catalog = r;
        return r;
      },
      (e) => {
        if (version !== catalogVersion) return loadCatalog();
        catalogPromise = null;
        throw e;
      },
    ).finally(() => { if (version === catalogVersion) catalogPromise = null; });
  }
  return catalogPromise;
}

// ---- 保存：裁切和编辑共用一个 rev（保存队列在 saves.js，按照片 id 存，不挂在 ed 上） ----------

export function syncRev(view, rev) {
  const rec = saveRecord(view.photo.id);
  rec.rev = rev;
  // 记下拍摄日期：「这一天的全部」导出前据此找出要等的照片（F02）
  if (view.photo.date) rec.date = view.photo.date;
  if (view.ed) view.ed.rev = rev;
  if (view.crop) view.crop.rev = rev;
}

// 导出前调用：等这张照片还没写完的保存；有没写进去的就拒绝（R05）
export function flushEdits(view) {
  return flushPhotos([view.photo.id]);
}

// 视图还在显示这张照片吗（关掉之后只更新保存记录，不碰界面）
function live(view) {
  return !view.disposed;
}

// 放弃没写进去的修改：按服务端现在的内容重读
export async function discardFailed(view) {
  clearFailed(view.photo.id);
  await reloadEdit(view);
  await loadCrop(view).catch(() => {});
  view.redrawSide?.();
  view.onChange?.();
}

// ---- 读取 --------------------------------------------------------------------

async function fetchEdit(view) {
  return api(`/api/photos/${view.photo.id}/edit`);
}

function capsFrom(photo, probe) {
  if (probe && probe.state === 'ok' && probe.data) return probe.data;
  return fallbackCaps(photo);
}

function applyEdit(ed, edit) {
  ed.rev = edit.rev;
  ed.memory = edit.memory || {};
  ed.lut = edit.lut || null;
  ed.recipe = edit.recipe ? { ...edit.recipe, levels: { ...(edit.recipe.levels || {}) } } : null;
  const a = edit.adjust || {};
  ed.adj = {
    exposure_ev: a.exposure_ev ?? 0,
    warm: a.wb?.warm ?? 0,
    tint: a.wb?.tint ?? 0,
    contrast: a.contrast ?? 0,
    highlight: a.highlight ?? 0,
    shadow: a.shadow ?? 0,
    black_point: a.black_point ?? 0,
    saturation: a.saturation ?? 0,
    vibrance: a.vibrance ?? 0,
    vignette: a.vignette ?? 0,
  };
  ed.curve = edit.curve?.master ? edit.curve.master.map((p) => [...p]) : null;
  ed.geometry = ed.v2 ? { ...DEFAULT_GEOMETRY, ...(edit.geometry || {}) } : null;
  const fin = resolveFinish(edit, ed.defaults);
  ed.frame = fin.frame;
  ed.frameFrom = fin.frameFrom;
  ed.stamp = fin.stamp;
  ed.stampFrom = fin.stampFrom;
  ed.render = edit.render || null;
  ed.warning = edit.warning || null;
}

export async function ensureEdit(view) {
  if (!view.edPromise) {
    view.edPromise = (async () => {
      const id = view.photo.id;
      // 这张照片还有没写完的保存（刚切走又切回来）：等它们写完再读，读的过程中又有写入就重读（F04）
      const [edit, , frames, defaults, caps, meta] = await readFresh(id, () => Promise.all([
        fetchEdit(view), loadCatalog(), loadFrames(), loadDefaults(), loadPhotoCaps(id), loadMeta(id),
      ]));
      if (view.disposed) throw new Error('这张照片已经关闭。');
      const ed = {
        v2: frames.state === 'ok',
        frames: frames.state === 'ok' ? frames.data : null,
        defaults: defaults.state === 'ok' ? defaults.data : null,
        caps: capsFrom(view.photo, caps),
        meta: meta.state === 'ok' ? meta.data : null,
        seq: 0,
        timer: null,
        ctl: null,
        urls: { final: null, uncropped: null },
        valid: null,
        label: null,
        error: null,
        busy: false,
      };
      applyEdit(ed, edit);
      view.ed = ed;
      syncRev(view, ed.rev);
      return ed;
    })().catch((e) => {
      view.edPromise = null;
      throw e;
    });
  }
  return view.edPromise;
}

// 从接口重新读一次（导入相机数据后、重试保存成功后、放弃修改后用）。
// 读到的必须比本地新：读的过程中有新的写入就重读（readFresh）；视图已经关了或换了编辑对象就什么都不改，
// 免得旧的内容和旧的 rev 盖掉刚写进去的（F04）。
export async function reloadEdit(view, { refreshCatalog = true } = {}) {
  const ed = view.ed;
  if (!ed) return;
  let edit;
  let caps;
  try {
    [edit, , caps] = await readFresh(view.photo.id, () => Promise.all([
      fetchEdit(view), loadCatalog({ refresh: refreshCatalog }), loadPhotoCaps(view.photo.id, { refresh: refreshCatalog }),
    ]));
  } catch (e) {
    if (live(view) && view.ed === ed) toastError(e, '无法重新读取这张照片的编辑记录。');
    return;
  }
  if (!live(view) || view.ed !== ed) return;
  applyEdit(ed, edit);
  ed.caps = capsFrom(view.photo, caps);
  syncRev(view, ed.rev);
  view.redrawSide?.();
  schedulePreview(view, { full: true });
}

export async function loadCrop(view) {
  const c = await readFresh(view.photo.id, () => api(`/api/photos/${view.photo.id}/crop`));
  if (live(view)) setCrop(view, c);
}

// 接口的 rect 是数组，这里转成 {x, y, w, h}（写回时再转回数组，见 cropDraft / saveCrop 的调用方）
export function setCrop(view, c) {
  view.crop = { ...c, rect: fromArray(c.rect) };
  // 记下服务端现在的框：本地拖过、还没写回的框和它不一样，松手时要保存（见 correct.js 的 afterGeometry）
  view.crop.savedKey = JSON.stringify(view.crop.rect);
  syncRev(view, c.rev);
}

// ---- 当前底子与参数 -----------------------------------------------------------

export function selection(view) {
  const ed = view.ed;
  if (ed.recipe) return ed.recipe;
  return { unit: unitForPhoto(view.photo) || 'jlaunch', slot: 4, levels: {} };
}

export function recipeInfo(view, sel = selection(view)) {
  const unit = state.catalog?.units.find((u) => u.id === sel.unit);
  if (sel.kind === 'look') return unit?.looks?.find((r) => r.id === sel.id) || null;
  if (sel.kind === 'camera_user') return unit?.users?.find((r) => r.id === sel.id) || null;
  return unit?.recipes.find((r) => r.slot === sel.slot) || null;
}

export function recipeKey(sel) {
  return sel.kind && sel.kind !== 'builtin' ? `${sel.unit}/${sel.kind}/${sel.id}` : `${sel.unit}/${sel.slot}`;
}

// 旋钮的当前档位：没设过、或不在可选档位里，就取默认
export function effective(control, levels) {
  const v = levels?.[control.name];
  return control.levels.includes(v) ? v : control.default;
}

// 只留下这个底子真正有的旋钮，并且档位都是允许的
export function cleanLevels(info, levels) {
  const out = {};
  if (!info) return out;
  for (const c of info.controls) {
    const v = levels?.[c.name];
    if (v !== undefined && c.levels.includes(v)) out[c.name] = v;
  }
  return out;
}

export function ensureRecipe(view) {
  const ed = view.ed;
  if (!ed.recipe) {
    const sel = selection(view);
    ed.recipe = { ...sel, levels: {} };
  }
  return ed.recipe;
}

export function recipeBody(view, { memory = false } = {}) {
  const ed = view.ed;
  if (!ed.recipe) return null;
  const body = {
    kind: ed.recipe.kind || 'builtin',
    ...(ed.recipe.id ? { id: ed.recipe.id } : {}),
    unit: ed.recipe.unit,
    slot: ed.recipe.slot,
    levels: cleanLevels(recipeInfo(view, ed.recipe), ed.recipe.levels),
  };
  if (memory && Object.keys(ed.memory).length) body.memory = ed.memory;
  return body;
}

export function adjustBody(ed) {
  const a = ed.adj;
  const body = { exposure_ev: a.exposure_ev, wb: { warm: a.warm, tint: a.tint }, contrast: a.contrast, highlight: a.highlight, shadow: a.shadow };
  if (ed.v2) Object.assign(body, { black_point: a.black_point, saturation: a.saturation, vibrance: a.vibrance, vignette: a.vignette });
  return body;
}

export function curveBody(ed) {
  return ed.curve ? { master: ed.curve } : null;
}

// 旧版的「能不能调」：只有 v2 才认黑点、饱和度、自然饱和度、暗角
export function geometryOf(view) {
  if (view.ed.v2) return view.ed.geometry;
  return { ...DEFAULT_GEOMETRY, straighten: view.crop ? view.crop.angle_deg || 0 : 0 };
}

export function styleOptions(ed, styleId) {
  const list = ed.frames && ed.frames.styles;
  const arr = Array.isArray(list) ? list : Object.values(list || {});
  const s = arr.find((x) => x && x.id === styleId);
  return s && Array.isArray(s.options) ? s.options : [];
}

// ---- 保存 --------------------------------------------------------------------

// 写 /edit。changes 只带要改的键（后端只认带上来的键）。
// 照片 id 和内容在调用时绑定（R03/R04）；兑现为 true / false。失败时界面保留你的修改，标成「未保存」，可以重试或放弃。
export function persist(view, changes) {
  return writeSave(saveRecord(view.photo.id), 'edit', changes, {
    onOk: (r) => {
      if (!live(view)) return;
      syncRev(view, r.rev);
      if (view.ed) view.ed.render = r.render || view.ed.render;
      if (r.warning) toast(r.warning, 'info');
      view.onChange?.();
    },
    onFail: () => {
      if (live(view)) view.onChange?.();
    },
  });
}

// 写 /crop：preset、rect、orientation（v2）或 flip（旧版的横竖），以及旧版的 angle_deg
export function saveCrop(view, body) {
  return writeSave(saveRecord(view.photo.id), 'crop', body, {
    onOk: (c) => {
      if (!live(view)) return;
      setCrop(view, c);
      if (c.warning) toast(c.warning, 'info');
      view.redrawSide?.();
      view.onChange?.();
    },
    // 拉直超出旧版后端的范围等错误：把后端的中文原因显示出来；界面先保留你的框，标成未保存
    onFail: (e) => {
      if (!live(view)) return;
      view.lastCropError = e.message;
      view.redrawSide?.();
      view.onChange?.();
    },
  });
}

// 裁剪还原：用 PUT 写回「原始、整张、0°」，不用 DELETE /crop（DELETE 会把照片的宽高从编辑记录里清掉，之后读裁剪会失败）。
// 角度要明写 0：PUT /crop 不带 angle_deg 时保持原来的角度，不会归零。调用方先把几何（拉直）清掉，两边一致。
export function resetCropRemote(view) {
  const body = { preset: 'original', rect: [0, 0, 1, 1], angle_deg: 0, ...(view.ed && view.ed.v2 ? { orientation: 'landscape' } : { flip: false }) };
  return saveCrop(view, body);
}

// ---- 撤销：每次改动前记下整套状态（最多 100 步），撤销时把旧状态写回去 ------------

function snapshot(view) {
  const ed = view.ed;
  const c = view.crop;
  return JSON.stringify({
    recipe: ed.recipe,
    lut: ed.lut,
    adj: ed.adj,
    curve: ed.curve,
    geometry: ed.v2 ? ed.geometry : null,
    frame: ed.frame,
    stamp: ed.stamp,
    frameFrom: ed.frameFrom,
    stampFrom: ed.stampFrom,
    crop: c ? { preset: c.preset, rect: c.rect, angle_deg: c.angle_deg, orientation: c.orientation } : null,
  });
}

export function beginChange(view) {
  if (view.ed && !view.pending) view.pending = snapshot(view);
}

export function endChange(view) {
  const before = view.pending;
  view.pending = null;
  if (!before || !view.ed || before === snapshot(view)) return;
  view.undo = view.undo || [];
  view.undo.push(before);
  if (view.undo.length > 100) view.undo.shift();
  // 新的改动之后，原来可以重做的就作废了
  view.redo = [];
  syncUndo(view);
}

export function undoAvailable(view) {
  return !!(view.undo && view.undo.length);
}

// 撤销按钮：没有可以撤销的内容时置灰（COPY 3.10）
export function syncUndo(view) {
  if (!view.undoBtn) return;
  if (undoAvailable(view)) view.undoBtn.removeAttribute('aria-disabled');
  else view.undoBtn.setAttribute('aria-disabled', 'true');
}

export function undoChange(view) {
  const s = view.undo && view.undo.pop();
  if (!s || !view.ed) return false;
  view.redo = view.redo || [];
  view.redo.push(snapshot(view));
  syncUndo(view);
  return restoreSnapshot(view, s);
}

// 重做（⇧⌘Z / Ctrl+Y）：把撤销掉的那一步放回来
export function redoChange(view) {
  const s = view.redo && view.redo.pop();
  if (!s || !view.ed) return false;
  view.undo = view.undo || [];
  view.undo.push(snapshot(view));
  syncUndo(view);
  return restoreSnapshot(view, s);
}

function restoreSnapshot(view, s) {
  const o = JSON.parse(s);
  const ed = view.ed;
  // 装饰回到撤销前的状态：原来继承默认的，写 null（后端按当前默认值算）；明确设置的，写原来的内容（R2-09）。
  // 只有真的变了才写回去，不然撤销会把 frame 键写进编辑记录。
  const frameOwn = o.frameFrom === 'photo';
  const stampOwn = o.stampFrom === 'photo';
  const finishChanged = ed.frameFrom !== o.frameFrom || ed.stampFrom !== o.stampFrom
    || (frameOwn && JSON.stringify(ed.frame) !== JSON.stringify(o.frame))
    || (stampOwn && JSON.stringify(ed.stamp) !== JSON.stringify(o.stamp));
  ed.recipe = o.recipe;
  ed.lut = o.lut || null;
  ed.adj = o.adj;
  ed.curve = o.curve;
  if (ed.v2 && o.geometry) ed.geometry = o.geometry;
  const fin = resolveFinish({ frame: frameOwn ? o.frame : null, stamp: stampOwn ? o.stamp : null }, ed.defaults);
  ed.frame = fin.frame;
  ed.frameFrom = fin.frameFrom;
  ed.stamp = fin.stamp;
  ed.stampFrom = fin.stampFrom;
  const changes = { recipe: recipeBody(view, { memory: true }), lut: ed.lut, adjust: adjustBody(ed), curve: curveBody(ed) };
  if (ed.v2) {
    Object.assign(changes, { geometry: ed.geometry });
    if (finishChanged) Object.assign(changes, {
      frame: frameOwn ? frameBody(ed.frame, styleOptions(ed, ed.frame.style)) : null,
      stamp: stampOwn ? ed.stamp : null,
    });
  }
  persist(view, changes);
  if (o.crop && view.crop) {
    const cur = view.crop;
    const same = JSON.stringify(cur.rect) === JSON.stringify(o.crop.rect) && cur.preset === o.crop.preset;
    if (!same) saveCrop(view, { preset: o.crop.preset, rect: toArray(o.crop.rect), angle_deg: geometryOf(view).straighten || 0, ...(ed.v2 ? { orientation: o.crop.orientation } : {}) });
  }
  schedulePreview(view, { full: true });
  view.redrawSide?.();
  view.onChange?.();
  return true;
}

// ---- 预览（渲染） -------------------------------------------------------------

function cropDraft(view) {
  const c = view.crop;
  if (!c) return undefined;
  return { preset: c.preset, rect: toArray(c.rect), orientation: c.orientation };
}

// 渲染用的配方：只有 RAW 的完整配方照常用；只有 JPG 的「近似」只用底子、不带旋钮（DIAG 原因 3）；不能用的照片不带配方。
// 其它请求（调整、曲线、裁切、纠正、边框、时间戳）和配方无关，照常发。
export function recipeForRender(view) {
  const ed = view.ed;
  const caps = ed.caps && ed.caps.recipe;
  if (caps === 'none') return null;
  const body = recipeBody(view);
  if (body && caps === 'approx') body.levels = {};
  return body;
}

function draftBody(view, stage) {
  const ed = view.ed;
  const body = { recipe: recipeForRender(view), lut: ed.lut, adjust: adjustBody(ed), curve: curveBody(ed) };
  if (!ed.v2) return body;
  Object.assign(body, {
    geometry: ed.geometry,
    frame: frameBody(ed.frame, styleOptions(ed, ed.frame.style)),
    stamp: ed.stamp,
  });
  if (stage === 'final' && view.crop) body.crop = cropDraft(view);
  return body;
}

function renderQuery(ed, stage, full) {
  if (ed.v2) return `?stage=${stage}&size=${full ? 'full' : 'preview'}`;
  const parts = [`crop=${stage === 'final' ? 'true' : 'false'}`];
  if (!full) parts.push(`max_edge=${PREVIEW_EDGE}`);
  return `?${parts.join('&')}`;
}

function setUrl(ed, stage, url) {
  if (ed.urls[stage]) URL.revokeObjectURL(ed.urls[stage]);
  ed.urls[stage] = url;
}

export function decodeLabel(raw) {
  if (!raw) return null;
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

// 拖动时防抖地请求小图，松手后 full 请求清晰版。旧图保留，直到新图到了。
// 显卡预览（gpu_stage.js）接手时不发请求：参数直接交给引擎，下一帧就画出来。
export function schedulePreview(view, { full = false, stage = null } = {}) {
  const ed = view.ed;
  if (!ed) return;
  if (stage) view.stage = stage;
  clearTimeout(ed.timer);
  const way = gpuRoute(view, gpuContext(view), () => {
    if (view.ed === ed) schedulePreview(view, { full });
  });
  if (way !== 'server') {
    ed.seq += 1; // 还在路上的服务器预览作废
    ed.ctl?.abort();
    if (way === 'gpu') {
      ed.busy = false;
      view.onChange?.();
    }
    return;
  }
  view.gpuActive = null;
  ed.timer = setTimeout(() => runPreview(view, full), full ? 0 : DEBOUNCE_MS);
}

// 显卡预览要的当前草稿（和 draftBody 发给服务器的是同一份）
function gpuContext(view) {
  const ed = view.ed;
  const stage = view.stage === 'uncropped' ? 'uncropped' : 'final';
  return {
    stage,
    recipe: recipeForRender(view),
    adjust: adjustBody(ed),
    curve: ed.curve,
    geometry: ed.v2 ? ed.geometry : null,
    crop: stage === 'final' ? cropDraft(view) || null : null,
    geometryKey: geometryKey(ed),
  };
}

async function runPreview(view, full) {
  const ed = view.ed;
  const stage = view.stage === 'uncropped' ? 'uncropped' : 'final';
  if (!ed) return;
  ed.seq += 1;
  const seq = ed.seq;
  ed.ctl?.abort();
  const ctl = new AbortController();
  ed.ctl = ctl;
  ed.busy = true;
  view.onChange?.();
  try {
    const { blob, headers } = await apiBlob(`/api/photos/${view.photo.id}/render${renderQuery(ed, stage, full)}`, {
      body: draftBody(view, stage),
      signal: ctl.signal,
    });
    if (seq !== ed.seq || view.ed !== ed) return;
    setUrl(ed, stage, URL.createObjectURL(blob));
    if (stage === 'uncropped') {
      // 有效画面只对应发出这次请求时的几何参数：之后几何变了，它就不能再用（见 validRect）
      ed.valid = parseRect(headers.get('X-GXR-Valid-Rect'));
      ed.validKey = geometryKey(ed);
    }
    if (stage === 'final') {
      ed.label = decodeLabel(headers.get('X-GXR-Label'));
      const cal = headers.get('X-GXR-Calibrated');
      ed.calibrated = cal === null ? null : cal === '1';
    }
    ed.error = null;
    if (ed.render) ed.render = { ...ed.render, available: true, message: null, reason: null };
  } catch (e) {
    if (e.name === 'AbortError' || seq !== ed.seq) return;
    ed.error = e;
    if (e.data?.status) applyCamdataStatus(e.data.status);
  } finally {
    if (seq === ed.seq) ed.busy = false;
    if (view.ed === ed) view.onChange?.();
  }
}

// 左右对比（Y）的左半边：同样的裁剪、拉直、边框，但没有配方、LUT、调整和曲线。
// 只在几何或裁剪变了时重新请求一次（按指纹缓存），拖滑块不会重复请求。
export function beforeUrl(view, onReady) {
  const ed = view.ed;
  if (!ed) return null;
  const body = draftBody(view, 'final');
  const zero = { ...ed, adj: Object.fromEntries(Object.keys(ed.adj).map((k) => [k, 0])) };
  Object.assign(body, { recipe: null, lut: null, adjust: adjustBody(zero), curve: null });
  const key = JSON.stringify([body.geometry || null, body.crop || null, body.frame || null, body.stamp || null]);
  const hit = ed.before;
  if (hit && hit.key === key) return hit.url;
  if (hit && hit.loading === key) return hit.url || null;
  // 每次请求带一个代号：几何参数连着改时会有几次请求同时在路上，只认最后一次的结果；
  // 晚到的旧结果直接丢掉，不装进缓存、也不重画（F08）。换掉的地址按缓存里当时真正的那个释放。
  const shown = hit ? hit.url : null;
  const ticket = (ed.beforeSeq = (ed.beforeSeq || 0) + 1);
  ed.before = { key: hit ? hit.key : null, url: shown, loading: key };
  apiBlob(`/api/photos/${view.photo.id}/render${renderQuery(ed, 'final', false)}`, { body }).then(({ blob }) => {
    if (view.ed !== ed || ed.beforeSeq !== ticket) return;
    const old = ed.before && ed.before.url;
    if (old) URL.revokeObjectURL(old);
    ed.before = { key, url: URL.createObjectURL(blob), loading: null };
    onReady?.();
  }, () => {
    if (view.ed === ed && ed.beforeSeq === ticket) ed.before = { key: null, url: ed.before && ed.before.url, loading: null };
  });
  return shown;
}

// 几何参数的指纹：有效画面要和它对得上才能用
function geometryKey(ed) {
  return JSON.stringify(ed.v2 ? ed.geometry : null);
}

function parseRect(raw) {
  if (!raw) return null;
  const n = raw.split(',').map(Number);
  if (n.length !== 4 || n.some((v) => !Number.isFinite(v))) return null;
  return { x: n[0], y: n[1], w: n[2], h: n[3] };
}

// 有效画面：后端给了就用后端的（v2），没有就按拉直估计（旧版）。canvas 是未裁切画面的像素宽高。
export function validRect(view, canvas) {
  const ed = view.ed;
  if (ed && ed.v2 && ed.valid && ed.validKey === geometryKey(ed)) return ed.valid;
  const g = geometryOf(view);
  if (!canvas || !canvas.w) return { x: 0, y: 0, w: 1, h: 1 };
  return legacyValid(g.straighten || 0, canvas);
}

// 画面上的小标签（COPY 3.9）：返回数组，没有就是 []。
// 电脑预测后面跟「未校准」（跨单元或跨 ISO，或认不出的单元），提示气泡写「使用 A12 50mm 的颜色模型」。
// 近似只在 JPG 上出现，标签是「近似」。
export const UNCALIBRATED_TITLE = '使用 A12 50mm 的颜色模型';

// 后端的 X-GXR-Label 是机器值：prediction（电脑预测）、approx（近似）、original（原图，不加小标签）。
// 没有校准（X-GXR-Calibrated=0）的电脑预测再加「未校准」。界面上不出现英文机器值（R2-05）。
export const LABEL_WORDS = { prediction: '电脑预测', approx: '近似', 'camera-jpg': '相机 JPG', neutral: '中性转换', embedded: '内嵌预览' };

export function stageLabels(view) {
  const ed = view.ed;
  if (!ed || view.showOriginal) return [];
  if (ed.caps.recipe === 'approx' && ed.recipe) return [{ text: '近似', kind: 'warn' }];
  const label = ed.label;
  if (!label || label === 'original') return [];
  if (label === 'prediction' || label.includes('电脑预测')) {
    const out = [{ text: '电脑预测', kind: 'ok' }];
    if (ed.calibrated === false || label.includes('未校准')) out.push({ text: '未校准', kind: 'warn', title: UNCALIBRATED_TITLE });
    return out;
  }
  if (label === 'approx') return [{ text: '近似', kind: 'warn' }];
  // 旧版后端直接给中文的时候照样显示
  if (/[^\x00-\x7f]/.test(label)) return [{ text: label.replace(/\s*未校准$/, ''), kind: 'grey' }];
  const word = LABEL_WORDS[label];
  return word ? [{ text: word, kind: 'grey' }] : [];
}

// 渲染失败或照片不能出图时，要告诉用户的原因（中文 message）
export function renderProblem(view) {
  const ed = view.ed;
  if (!ed) return null;
  if (ed.error) return ed.error.message;
  const r = ed.render;
  if (r && r.available === false && r.message) return r.message;
  return null;
}

// 原图（按住空格）要显示的那张：照片本身
// 按住空格看原图（PRD 第 11 节第 3 条）：有相机 JPG 给相机 JPG，只有 RAW 给 04 标准；永远不带编辑和默认装饰。
export function originalUrl(view) {
  return `/api/photos/${view.photo.id}/render?stage=original&size=preview`;
}

// 后端还没有原图接口（404）时退回原来的预览图，见 stage.js 的 error 处理
export function originalFallbackUrl(view) {
  return view.photo.preview_url;
}

export function originalNote(photo) {
  return originalNoteText(photo && photo.preview_look);
}

export function defaultNote(photo) {
  const look = photo && photo.preview_look;
  return { text: lookNoteText(look), prediction: isPrediction(look) };
}

// 关掉大图：只停预览、放掉图片。已经排队的保存不受影响（它们绑定的是保存记录，不是 ed）。
export function disposeEdit(view) {
  view.disposed = true;
  const ed = view.ed;
  if (!ed) return;
  clearTimeout(ed.timer);
  ed.ctl?.abort();
  for (const k of Object.keys(ed.urls)) {
    if (ed.urls[k]) URL.revokeObjectURL(ed.urls[k]);
    ed.urls[k] = null;
  }
  if (ed.before && ed.before.url) URL.revokeObjectURL(ed.before.url);
  ed.before = null;
  view.ed = null;
  view.edPromise = null;
}

// 给外部用：导入相机数据之后，这张照片的画面和能力都要重读
export async function refreshCaps(view) {
  const caps = await loadPhotoCaps(view.photo.id, { refresh: true });
  if (view.ed) view.ed.caps = capsFrom(view.photo, caps);
}

export { loadMeta, loadFrames };
