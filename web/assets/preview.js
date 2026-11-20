// 修图的编辑状态与预览（每张照片一份，挂在 view 上：view.ed、view.crop）。
// 读：GET /edit、GET /crop。写：PUT /edit、PUT /crop，全部排在 view.saveQ 里一个一个来，共用一个 rev。
// 预览：POST /render，请求体带草稿（还没保存的滑块值也能看到效果）。
// 两套后端：ed.v2 为真（探测到 /api/frames）时带 stage/size 和几何、边框、时间戳；否则走旧接口（只认配方、调整、曲线、拉直）。

import { api, apiBlob } from './api.js';
import { saveRecord, writeSave, clearFailed, flushPhotos, readFresh } from './saves.js';
export { flushPhotos, saveStatus, retrySaves, SaveError } from './saves.js';
import { toast, toastError } from './dom.js';
import { applyCamdataStatus } from './camdata.js';
import { loadFrames, loadDefaults, loadPhotoCaps, loadMeta, fallbackCaps } from './caps.js';
import { resolveFinish, DEFAULT_GEOMETRY, frameBody, NO_FRAME, NO_STAMP } from './finish.js';
import { legacyValid, fromArray, toArray } from './geom.js';
import { route as gpuRoute } from './gpu_stage.js';
import { curveBody, normalizeCurves } from './curves.js';
import { rememberRecipe } from './my_recipes.js';
export { CURVE_MAX, curveBody } from './curves.js';
import { cropShape, HISTORY_GROUPS, ownedGroups, restoreCropLocal, snapshot, staleCropForRedo, syncUndo, withOwned } from './undo_entries.js';
export { syncUndo }; // 撤销 / 重做按钮的置灰在 undo_entries.js
import { isPairSecond, isSinglePairView, isUntouchedPairRecipe } from './halfframe.js';
import { loadCatalog, recipeBody } from './preview_recipe.js';
export {
  BASE_NAMES, slotName, recipeLabel, KNOB_NAMES, unitForPhoto, loadCatalog,
  selection, recipeInfo, recipeKey, effective, cleanLevels, ensureRecipe, recipeBody,
  LABEL_WORDS, stageLabels, originalNote, defaultNote,
} from './preview_recipe.js';

const PREVIEW_EDGE = 800; // 旧接口：拖动时的小图长边
const DEBOUNCE_MS = 120;

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


async function fetchEdit(view) {
  return api(`/api/photos/${view.photo.id}/edit`);
}

function capsFrom(photo, probe) {
  if (probe && probe.state === 'ok' && probe.data) return probe.data;
  return fallbackCaps(photo);
}

function applyEdit(ed, edit) {
  rememberRecipe(edit.local_recipe);
  ed.rev = edit.rev;
  ed.memory = edit.memory || {};
  ed.lut = edit.lut || null;
  ed.recipe = edit.recipe ? { ...edit.recipe, levels: { ...(edit.recipe.levels || {}) } } : null;
  ed.recipeFromCamera = !!edit.recipe_from_camera;
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
  ed.curve = normalizeCurves(edit.curve);
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

// 编辑记录的顶层字段 → ed 上对应的属性（重试只采用补写进去、界面之后又没改过的字段）。
// 撤销快照（undo_entries.js 的 snapshot）里的键和 ed 上的属性同名
const EDIT_FIELD_PROPS = {
  recipe: ['recipe', 'memory'],
  lut: ['lut'],
  adjust: ['adj'],
  curve: ['curve'],
  geometry: ['geometry'],
  frame: ['frame', 'frameFrom'],
  stamp: ['stamp', 'stampFrom'],
};

// 重试保存写成功一类：把写入接口的回应（服务器上现在的内容）装进正在显示的视图（F03 / N02）。
// 只装 fields 里的字段：界面之后又改过的字段保留界面上的值（V01）。
// 同步完成，不再发请求，所以不会读失败、也不会晚到盖掉之后的草稿。
// 撤销记录不用改：每一步只记它自己改了哪些字段，撤销只还原那些字段（Y01）。
// 正在进行的一步（pending）是「这一步开始前」的样子，补写进来的不是这一步做的，所以也装进去。
export function applyRetried(view, { kind, response, fields = [], rev = null } = {}) {
  if (!live(view) || !view.ed || !response || !fields.length) return false;
  const before = view.pending ? JSON.parse(view.pending) : null;
  if (kind === 'crop') {
    setCrop(view, response);
    if (before) before.crop = cropShape(view.crop);
  } else {
    // 换算要用到这张照片的 v2 与默认边框，和 view.ed 保持一致
    const fresh = { v2: view.ed.v2, defaults: view.ed.defaults };
    applyEdit(fresh, response);
    for (const f of fields) {
      for (const prop of EDIT_FIELD_PROPS[f] || []) {
        view.ed[prop] = fresh[prop];
        if (before && prop in before) before[prop] = view.ed.v2 || prop !== 'geometry' ? fresh[prop] : null;
      }
    }
    view.ed.render = fresh.render;
  }
  if (before) view.pending = JSON.stringify(before);
  if (rev !== null && rev !== undefined) syncRev(view, rev);
  schedulePreview(view, { full: true });
  return true;
}

export async function loadCrop(view) {
  const pre = view.crop ? JSON.stringify(view.crop.rect) : null; // 开始读的时候界面上的框
  const c = await readFresh(view.photo.id, () => api(`/api/photos/${view.photo.id}/crop`), (stale) => staleCropForRedo(view, stale, pre));
  const unsaved = saveRecord(view.photo.id).failed; // 界面上有一个还没保存的框：不能拿服务器的旧框盖掉它
  if (live(view)) { if (unsaved && unsaved.crop) view.boxStale = view.boxStale && !unsaved.cropShape; else setCrop(view, c); } // 被拒的框留着；它之后的拖动照常严格校验（不再带 fit）
}

// 接口的 rect 是数组，这里转成 {x, y, w, h}（写回时再转回数组，见 cropDraft / saveCrop 的调用方）
export function setCrop(view, c) {
  view.crop = { ...c, rect: fromArray(c.rect) };
  view.boxStale = false;
  // 记下服务端现在的框：本地拖过、还没写回的框和它不一样，松手时要保存（见 correct.js 的 afterGeometry）
  view.crop.savedKey = JSON.stringify(view.crop.rect);
  syncRev(view, c.rev);
}


export function adjustBody(ed) {
  const a = ed.adj;
  const body = { exposure_ev: a.exposure_ev, wb: { warm: a.warm, tint: a.tint }, contrast: a.contrast, highlight: a.highlight, shadow: a.shadow };
  if (ed.v2) Object.assign(body, { black_point: a.black_point, saturation: a.saturation, vibrance: a.vibrance, vignette: a.vignette });
  return body;
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


// 写 /edit。changes 只带要改的键（后端只认带上来的键）。
// 照片 id 和内容在调用时绑定（R03/R04）；兑现为 true / false。失败时界面保留你的修改，标成「未保存」，可以重试或放弃。
export function persist(view, changes) {
  if (isSinglePairView(view)) return Promise.resolve(false);
  if (view.ed && 'recipe' in changes) view.ed.recipeFromCamera = false;
  endChange(view); // 调用方在写之前已经改好了界面：这一下操作到此为止，撤销记一步，不等服务器回来
  if ('geometry' in changes) view.boxStale = true; // 服务器会顺带收框，界面的框要等读回来才跟上（setCrop 清掉）；这期间的框写入当目标写（crop.js cropBody）
  return writeSave(saveRecord(view.photo.id), 'edit', changes, {
    onOk: (r) => {
      if (!live(view)) return;
      syncRev(view, r.rev);
      rememberRecipe(r.local_recipe);
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
  if (isSinglePairView(view)) return Promise.resolve(false);
  endChange(view); // 同 persist：框已经在界面上改好了，撤销这一步不等保存
  const rec = saveRecord(view.photo.id);
  const write = writeSave(rec, 'crop', body, {
    onOk: (c) => {
      if (!live(view)) return;
      // 后面又排了新的裁切写入（连着点比例）：这一笔的回应已经过时，只记 rev；框留给最后一笔的回应换成准的
      if (rec.fieldGen.crop !== mine) {
        syncRev(view, c.rev);
        return;
      }
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
  const mine = rec.fieldGen.crop; // writeSave 排队时已经加了一，这就是这一笔的序号
  return write;
}

// 裁剪还原：用 PUT 写回「原始、整张、0°」，不用 DELETE /crop（DELETE 会把照片的宽高从编辑记录里清掉，之后读裁剪会失败）。
// 角度要明写 0：PUT /crop 不带 angle_deg 时保持原来的角度，不会归零。调用方先把几何（拉直）清掉，两边一致。
export function resetCropRemote(view) {
  const body = { preset: 'original', rect: [0, 0, 1, 1], angle_deg: 0, ...(view.ed && view.ed.v2 ? { orientation: 'landscape' } : { flip: false }) };
  return saveCrop(view, body);
}

// ---- 撤销：每次改动前记下整套状态（最多 100 步），撤销时只把这一步改过的组写回去 ---
// 快照与「改了哪些组」在 undo_entries.js

export function beginChange(view) {
  if (view.ed && !view.pending && !isSinglePairView(view)) view.pending = snapshot(view);
}

export function endChange(view) {
  const before = view.pending;
  view.pending = null;
  // 拖动途中重试写进了内容：面板等这一步结束才刷新（W01）
  if (view.panesStale) {
    view.panesStale = false;
    view.redrawSide?.();
  }
  const after = view.ed ? snapshot(view) : null;
  if (!before || !after || before === after) return;
  // 每一步只记自己改了的组（owned），撤销 / 重做只还原这些，不会顺带把别的字段写回旧值（Y01）
  view.undo = view.undo || [];
  view.undo.push(JSON.stringify({ ...JSON.parse(before), owned: ownedGroups(before, after) }));
  if (view.undo.length > 100) view.undo.shift();
  // 新的改动之后，原来可以重做的就作废了
  view.redo = [];
  syncUndo(view);
}

export function undoChange(view) {
  if (isSinglePairView(view)) return false;
  const s = view.undo && view.undo.pop();
  if (!s || !view.ed) return false;
  view.redo = view.redo || [];
  view.redo.push(withOwned(snapshot(view), s));
  syncUndo(view);
  return restoreSnapshot(view, s);
}

// 重做（⇧⌘Z / Ctrl+Y）：把撤销掉的那一步放回来
export function redoChange(view) {
  if (isSinglePairView(view)) return false;
  const s = view.redo && view.redo.pop();
  if (!s || !view.ed) return false;
  view.undo = view.undo || [];
  view.undo.push(withOwned(snapshot(view), s));
  syncUndo(view);
  return restoreSnapshot(view, s);
}

function restoreSnapshot(view, s) {
  const o = JSON.parse(s);
  const ed = view.ed;
  const own = new Set(o.owned || Object.keys(HISTORY_GROUPS));
  // 只还原这一步改过的组；没改过的保持现在的样子（Y01）
  // 装饰回到撤销前的状态：原来继承默认的，写 null（后端按当前默认值算）；明确设置的，写原来的内容（R2-09）。
  // 只有真的变了才写回去，不然撤销会把 frame 键写进编辑记录。
  // 边框、印记分开判断、分开写：只撤边框时不带 stamp 键，不然会把另一组没写进去、等着重试的修改冲掉（Z01）
  const tf = own.has('frame') ? o : ed;
  const ts = own.has('stamp') ? o : ed;
  const frameOwn = tf.frameFrom === 'photo';
  const stampOwn = ts.stampFrom === 'photo';
  const frameChanged = own.has('frame') && (ed.frameFrom !== tf.frameFrom || (frameOwn && JSON.stringify(ed.frame) !== JSON.stringify(tf.frame)));
  const stampChanged = own.has('stamp') && (ed.stampFrom !== ts.stampFrom || (stampOwn && JSON.stringify(ed.stamp) !== JSON.stringify(ts.stamp)));
  const changes = {};
  if (own.has('recipe')) {
    ed.recipe = o.recipe;
    if ('memory' in o) ed.memory = o.memory || {}; // 配方旁边记着的旋钮档位跟着这一步还原，不留粘贴进来的别张照片的
    const body = recipeBody(view, { memory: true });
    // 记忆是空的也要写过去：不带 memory 键，服务器会留着粘贴进来的档位（render/edits.py 的 write）
    if (body && 'memory' in o) body.memory = ed.memory;
    changes.recipe = body;
  }
  if (own.has('lut')) changes.lut = ed.lut = o.lut || null;
  if (own.has('adjust')) {
    ed.adj = o.adj;
    changes.adjust = adjustBody(ed);
  }
  if (own.has('curve')) {
    ed.curve = o.curve;
    changes.curve = curveBody(ed);
  }
  if (ed.v2 && own.has('geometry') && o.geometry) changes.geometry = ed.geometry = o.geometry;
  if (frameChanged || stampChanged) {
    const fin = resolveFinish({ frame: frameOwn ? tf.frame : null, stamp: stampOwn ? ts.stamp : null }, ed.defaults);
    if (frameChanged) {
      Object.assign(ed, { frame: fin.frame, frameFrom: fin.frameFrom });
      if (ed.v2) changes.frame = frameOwn ? frameBody(ed.frame, styleOptions(ed, ed.frame.style)) : null;
    }
    if (stampChanged) {
      Object.assign(ed, { stamp: fin.stamp, stampFrom: fin.stampFrom });
      if (ed.v2) changes.stamp = stampOwn ? ed.stamp : null;
    }
  }
  if (Object.keys(changes).length) persist(view, changes);
  if (own.has('crop') && o.crop && view.crop) {
    const cur = view.crop;
    // 旧版的拉直角度存在裁剪里：只改了角度的一步也要还原（Z02）；新版的角度在 geometry 里，用（可能刚还原的）几何
    const angle = ed.v2 ? geometryOf(view).straighten || 0 : o.crop.angle_deg || 0;
    const same = JSON.stringify(cur.rect) === JSON.stringify(o.crop.rect) && cur.preset === o.crop.preset
      && (ed.v2 ? cur.orientation === o.crop.orientation : (cur.angle_deg || 0) === angle);
    restoreCropLocal(view, o.crop); // 框和画布宽高当场回去，不等保存的回应
    const body = { preset: o.crop.preset, rect: toArray(o.crop.rect), ...(ed.v2 ? { orientation: o.crop.orientation } : { angle_deg: angle }) };
    // 撤销 / 重做的框是「用户见过的那个框」，现在的画布可能已经不是它当时的了（后面又拖过透视）：当目标写（fit），服务器收成离它最近的合格框。
    // 几何的一步不能凭界面上的框「没变」就不写：服务器写几何时顺带收过框
    if (own.has('geometry') || !same) saveCrop(view, ed.v2 ? { ...body, fit: true } : body);
  }
  schedulePreview(view, { full: true });
  view.redrawSide?.();
  view.onChange?.();
  return true;
}


function cropDraft(view) {
  const c = view.crop;
  if (!c) return undefined;
  return { preset: c.preset, rect: toArray(c.rect), orientation: c.orientation };
}

// 渲染用的配方：只有 RAW 的完整配方照常用；只有 JPG 的「近似」只用底子、不带旋钮（DIAG 原因 3）；不能用的照片不带配方。
// 未选过配方的半格一对发 null：两张 RAW 在服务端各自沿用拍摄时的配方。
export function recipeForRender(view) {
  const ed = view.ed;
  if (isUntouchedPairRecipe(view)) return null;
  const caps = ed.caps && ed.caps.recipe;
  const body = recipeBody(view);
  // 本机引用自带模型。当前相机数据缺失或引用损坏时仍交给服务端明确处理。
  if (body?.kind === 'local') return body;
  if (caps === 'none') return null;
  if (body && caps === 'approx' && !view.photo.recipe_tag) body.levels = {};
  return body;
}

export function draftBody(view, stage) {
  const ed = view.ed;
  const body = { recipe: recipeForRender(view), lut: ed.lut, adjust: adjustBody(ed), curve: curveBody(ed) };
  if (!ed.v2) return body;
  Object.assign(body, { geometry: ed.geometry, frame: NO_FRAME, stamp: NO_STAMP }); // 边框和时间戳在浏览器里画
  if (stage === 'final' && view.crop) body.crop = cropDraft(view);
  return body;
}

function renderQuery(view, stage, full) {
  const ed = view.ed;
  const single = isSinglePairView(view) ? `&view=${view.pairView}` : '';
  if (ed.v2) return `?stage=${stage}&size=${full ? 'full' : 'preview'}${single}`;
  const parts = [`crop=${stage === 'final' ? 'true' : 'false'}`];
  if (!full) parts.push(`max_edge=${PREVIEW_EDGE}`);
  if (single) parts.push(`view=${view.pairView}`);
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
  const way = isSinglePairView(view)
    ? 'server'
    : gpuRoute(view, gpuContext(view), () => {
      if (view.ed === ed) schedulePreview(view, { full });
    });
  if (way !== 'server') {
    ed.seq += 1; // 还在路上的服务器预览作废
    ed.ctl?.abort();
    if (way === 'gpu') {
      ed.busy = false;
      // 半格一对：显卡接手画双联后，服务器那层图（多半是之前某个单张视图的）已经不是屏幕上的画面。
      // 丢掉它：之后退回服务器（选了还没算好的 LUT、关掉显卡）或切到单张时，先显示占位，不会闪出另一半
      if (isPairSecond(view.photo)) setUrl(ed, 'final', null);
      view.onChange?.();
    }
    return;
  }
  view.gpuActive = null;
  ed.timer = setTimeout(() => runPreview(view, full), full ? 0 : DEBOUNCE_MS);
}

// 显卡预览要的当前草稿（和 draftBody 发给服务器的是同一份）；配方卡片（look_cards.js）也用它
export function gpuContext(view) {
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
  const stage = isSinglePairView(view) ? 'final' : (view.stage === 'uncropped' ? 'uncropped' : 'final');
  if (!ed) return;
  ed.seq += 1;
  const seq = ed.seq;
  ed.ctl?.abort();
  const ctl = new AbortController();
  ed.ctl = ctl;
  ed.busy = true;
  view.onChange?.();
  try {
    const { blob, headers } = await apiBlob(`/api/photos/${view.photo.id}/render${renderQuery(view, stage, full)}`, {
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
      ed.scale = Number(headers.get('X-GXR-Scale')) || 1; // 导出的长边是这张图的几倍（边框版面用）
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
  const keyParts = [body.geometry || null, body.crop || null, body.frame || null, body.stamp || null];
  if (isSinglePairView(view)) keyParts.push(view.pairView);
  const key = JSON.stringify(keyParts);
  const hit = ed.before;
  if (hit && hit.key === key) {
    // 又回到了缓存里的几何：还在路上的别的几何的请求作废，晚到了也不能把这张换掉（N03）
    if (hit.loading) {
      ed.beforeSeq = (ed.beforeSeq || 0) + 1;
      ed.before = { key: hit.key, url: hit.url, loading: null };
    }
    return hit.url;
  }
  if (hit && hit.loading === key) return hit.url || null;
  // 每次请求带一个代号：几何参数连着改时会有几次请求同时在路上，只认最后一次的结果；
  // 晚到的旧结果直接丢掉，不装进缓存、也不重画（F08）。换掉的地址按缓存里当时真正的那个释放。
  const shown = hit ? hit.url : null;
  const ticket = (ed.beforeSeq = (ed.beforeSeq || 0) + 1);
  ed.before = { key: hit ? hit.key : null, url: shown, loading: key };
  apiBlob(`/api/photos/${view.photo.id}/render${renderQuery(view, 'final', false)}`, { body }).then(({ blob }) => {
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
  const single = isSinglePairView(view) ? `&view=${view.pairView}` : '';
  return `/api/photos/${view.photo.id}/render?stage=original&size=preview${single}`;
}

// 后端还没有原图接口（404）时退回原来的预览图，见 stage.js 的 error 处理
export function originalFallbackUrl(view) {
  return view.photo.preview_url;
}

// 关掉大图：只停预览、放掉图片。已经排队的保存不受影响（它们绑定的是保存记录，不是 ed）。
export function disposeEdit(view) {
  view.disposed = true;
  if (typeof view.logoCatalogWatch === 'function') { view.logoCatalogWatch(); view.logoCatalogWatch = null; }
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
