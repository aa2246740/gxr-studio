// 大图「配方」标签的图片卡片：每个配方一张卡片，画的是当前这张照片套上这个配方的样子。
// 和边框标签的卡片（finish_cards.js）一样在浏览器里本地画，不请服务器给每张卡片出一张图：
//   显卡预览在用时，用引擎按每个配方的表在 RAW 上画小图（gpu_stage.js 的 lookThumb，和舞台同一条路，点了以后舞台画出来的就是它）；
//   显卡做不了的（没有 WebGPU、只有 JPG 的近似）才请服务器出小图，一张一张来。
// 卡片慢慢出现，不挡面板也不挡舞台：先是骨架，舞台稳了（没有手在拖、参数一会儿没变、没在出图）才开始画，
// 看得见的先画；拖滑块、拖裁切框时一张都不画。图按「照片 + 相机数据版本 + 配方 + 调整 + 几何 + 裁切」缓存。
// 点一张卡片 = 点原来的那个配方按钮：写进去的编辑一模一样（recipes.js 的 switchRecipe / applyComputer）。

import { h } from './dom.js';
import { apiBlob } from './api.js';
import { state, on } from './state.js';
import { cleanLevels, draftBody, gpuContext, recipeInfo, recipeKey, selection } from './preview.js';
import { lookThumb } from './gpu_stage.js';
import { Lru, cardKey, sharedKey } from './look_model.js';

const CARD_CSS_EDGE = 150; // 卡片图的长边（CSS 像素）；高分屏按两倍画
const SETTLE_MS = 120; // 参数、输入停下来这么久才开始画
const POLL_MS = 200;
const cache = new Lru(90); // 最近用过的 90 张小图（每张约 0.25 MB）

on('camdata-changed', () => cache.clear());
on('luts-changed', () => cache.clear());

const dpr = () => Math.min(2, globalThis.devicePixelRatio || 1);
const now = () => (globalThis.performance ? performance.now() : Date.now());

// ---- 手有没有在动：拖滑块、拖框、滚面板、敲键盘时不画 ------------------------------------------
let down = false;
let lastInput = 0;
if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
  const stamp = () => { lastInput = now(); };
  document.addEventListener('pointerdown', () => { down = true; stamp(); }, true);
  for (const type of ['pointerup', 'pointercancel']) document.addEventListener(type, () => { down = false; stamp(); }, true);
  for (const type of ['keydown', 'wheel', 'input']) document.addEventListener(type, stamp, true);
  if (typeof window !== 'undefined' && window.addEventListener) window.addEventListener('blur', () => { down = false; });
}

// ---- 点这张卡片会得到什么配方（switchRecipe / applyComputer 和卡片出图共用） --------------------------

// 内置配方、Looks、相机里的配方：换配方时记住原来的旋钮，取回这个配方上次的旋钮（每个配方各自记住）。same = 已经是它了
export function recipeTarget(view, target) {
  const ed = view.ed;
  const sel = selection(view);
  const next = { unit: sel.unit, kind: target.kind, slot: target.slot, ...(target.id ? { id: target.id } : {}), levels: {} };
  if (ed.recipe && recipeKey(sel) === recipeKey(next)) return { same: true, next };
  const memory = { ...ed.memory, [recipeKey(sel)]: cleanLevels(recipeInfo(view, sel), sel.levels) };
  next.levels = cleanLevels(recipeInfo(view, next), memory[recipeKey(next)] || {});
  return { same: false, next, memory };
}

// 电脑上的配方：换成它的底子和档位
export function computerTarget(view, r) {
  const sel = selection(view);
  const target = { unit: r.recipe.unit, kind: 'builtin', slot: r.recipe.slot };
  const memory = { ...view.ed.memory, [recipeKey(sel)]: cleanLevels(recipeInfo(view, sel), sel.levels) };
  return { next: { ...target, levels: cleanLevels(recipeInfo(view, target), r.recipe.levels) }, memory };
}

// 渲染请求里的配方（和 preview.js 的 recipeBody / recipeForRender 同一个形状）。近似（只有 JPG）不带旋钮
export function renderRecipe(view, sel, approx) {
  return {
    kind: sel.kind || 'builtin',
    ...(sel.id ? { id: sel.id } : {}),
    unit: sel.unit,
    slot: sel.slot,
    levels: approx ? {} : cleanLevels(recipeInfo(view, sel), sel.levels),
  };
}

// ---- 一副卡片 ----------------------------------------------------------------------------------

function sharedOf(view, edge) {
  const ctx = gpuContext(view);
  const shared = sharedKey({
    photoId: view.photo.id, dataVersion: state.camdata?.dataVersion, edge, lut: view.ed.lut, adjust: ctx.adjust, curve: ctx.curve, geometry: ctx.geometry, crop: ctx.crop,
  });
  return { ctx, shared };
}

function paint(holder, img) {
  const canvas = holder.querySelector('canvas') || h('canvas', { 'aria-hidden': 'true' });
  canvas.width = img.w;
  canvas.height = img.h;
  canvas.getContext('2d').putImageData(img.image, 0, 0);
  holder.classList.remove('skel');
  holder.removeAttribute('aria-busy');
  if (!canvas.parentElement) holder.replaceChildren(canvas);
}

function failedHolder(holder) {
  holder.classList.remove('skel');
  holder.removeAttribute('aria-busy');
  holder.replaceChildren(h('span', { class: 'dim small' }, '没有预览'));
}

async function serverThumb(view, recipe, edge) {
  const body = { ...draftBody(view, 'final'), recipe };
  const { blob } = await apiBlob(`/api/photos/${view.photo.id}/render?stage=final&max_edge=${edge}`, { body });
  const bitmap = await createImageBitmap(blob);
  const canvas = document.createElement('canvas');
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(bitmap, 0, 0);
  bitmap.close?.();
  return { w: canvas.width, h: canvas.height, image: ctx.getImageData(0, 0, canvas.width, canvas.height) };
}

const canDraw = () => typeof globalThis.ImageData === 'function' && typeof document.createElement('canvas').getContext === 'function';

class Deck {
  constructor(view) {
    this.view = view;
    this.cards = [];
    this.root = null;
    this.dead = false;
    this.timer = 0;
    this.busy = false;
    this.detached = 0;
    this.lastSig = null;
    this.t0 = now();
    this.changedAt = this.t0;
    this.stats = { painted: 0, first: null, all: null, gpu: 0, server: 0, cached: 0 };
    this.edge = Math.round(CARD_CSS_EDGE * dpr());
  }

  // 一组卡片（一个 2 列的格子）。items：[{ id, name, desc, pressed, disabled, title, render, onPick }]
  // render：这张卡片要画的配方（渲染请求里的形状）；没有就不画（灰着的卡片）
  grid(items) {
    const cards = items.map((item) => this.card(item));
    return h('div', { class: 'fcards lcards' }, cards.map((c) => c.el));
  }

  card(item) {
    const holder = h('div', { class: item.render ? 'thumb skel' : 'thumb off', ...(item.render ? { 'aria-busy': 'true' } : {}) });
    const label = [item.name, item.desc, item.pressed ? '当前配方' : null].filter(Boolean).join('，');
    const el = h('button', {
      type: 'button',
      class: 'fcard lcard',
      'aria-pressed': String(!!item.pressed),
      'aria-disabled': item.disabled ? 'true' : null,
      'aria-label': label,
      title: item.title || null,
      tabindex: '-1',
      'data-look': item.id,
      onclick: (e) => {
        if (e.detail === 0) this.view.lookFocus = item.id; // 键盘点的：面板重搭以后把焦点放回这张卡片
        item.onPick();
      },
      onkeydown: (e) => this.key(e),
    }, holder, h('div', { class: 'nm' }, item.name), item.desc ? h('div', { class: 'ds' }, item.desc) : null);
    const card = { item, el, holder, key: null, failed: null };
    this.cards.push(card);
    return card;
  }

  // 在整副卡片里按方向走：左右前后一张，上下找最近的上一行 / 下一行。Enter / 空格 = 点
  key(e) {
    const keys = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'];
    if (!keys.includes(e.key) || e.metaKey || e.ctrlKey || e.altKey) return;
    const list = this.cards.map((c) => c.el);
    const at = list.indexOf(e.currentTarget);
    let to = at;
    if (e.key === 'ArrowLeft') to = Math.max(0, at - 1);
    else if (e.key === 'ArrowRight') to = Math.min(list.length - 1, at + 1);
    else if (e.key === 'Home') to = 0;
    else if (e.key === 'End') to = list.length - 1;
    else to = vertical(list, at, e.key === 'ArrowDown' ? 1 : -1);
    e.preventDefault();
    e.stopPropagation(); // 左右键在大图里是换照片
    if (to === at) return;
    list[at].setAttribute('tabindex', '-1');
    list[to].setAttribute('tabindex', '0');
    list[to].focus();
    list[to].scrollIntoView?.({ block: 'nearest' });
  }

  // 当前配方（没有就第一张）可以用 Tab 进入，其它卡片只用方向键
  roving() {
    const pick = this.cards.find((c) => c.item.pressed) || this.cards[0];
    if (pick) pick.el.setAttribute('tabindex', '0');
    const want = this.view.lookFocus;
    this.view.lookFocus = null;
    const target = want && this.cards.find((c) => c.item.id === want);
    if (target) {
      pick?.el.setAttribute('tabindex', '-1');
      target.el.setAttribute('tabindex', '0');
      const active = document.activeElement;
      if (!active || active === document.body) target.el.focus();
    }
  }

  start(root) {
    this.root = root;
    this.roving();
    this.stats.total = this.cards.filter((c) => c.item.render).length;
    // 重搭面板时先用缓存里已经有的图画上，不闪骨架
    if (this.view.ed && this.view.stage !== 'uncropped' && canDraw()) this.fromCache();
    this.report();
    if (this.stats.total && canDraw()) this.later(SETTLE_MS);
  }

  cancel() {
    this.dead = true;
    clearTimeout(this.timer);
  }

  later(ms) {
    if (this.dead) return;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.tick(), ms);
  }

  fromCache() {
    const { shared } = sharedOf(this.view, this.edge);
    for (const c of this.cards) {
      if (!c.item.render) continue;
      const hit = cache.get(cardKey(shared, c.item.render));
      if (hit && c.key !== cardKey(shared, c.item.render)) {
        paint(c.holder, hit);
        c.key = cardKey(shared, c.item.render);
        this.stats.cached += 1;
      }
    }
    this.count();
  }

  count() {
    const done = this.cards.filter((c) => c.item.render && c.key).length;
    this.stats.painted = done;
    const t = Math.round(now() - this.t0);
    if (done && this.stats.first === null) this.stats.first = t;
    if (done && done === this.stats.total && this.stats.all === null) this.stats.all = t;
    this.report();
  }

  report() {
    if (!this.root || !this.root.dataset) return;
    const d = this.root.dataset;
    d.looksTotal = String(this.stats.total || 0);
    d.looksPainted = String(this.stats.painted);
    d.looksFirstMs = this.stats.first === null ? '' : String(this.stats.first);
    d.looksAllMs = this.stats.all === null ? '' : String(this.stats.all);
    d.looksGpu = String(this.stats.gpu);
    d.looksServer = String(this.stats.server);
    d.looksCached = String(this.stats.cached);
  }

  tick() {
    this.timer = 0;
    const view = this.view;
    if (this.dead) return;
    if (view.disposed || !view.ed) return this.cancel();
    const first = this.cards[0] && this.cards[0].holder;
    if (!first || !first.isConnected) {
      this.detached += 1;
      if (this.detached > 8) return this.cancel(); // 面板被换掉了
      return this.later(POLL_MS);
    }
    this.detached = 0;
    // 在别的标签上、面板收起、拖着东西：一概不动
    if (view.stage === 'uncropped' || (first.closest && first.closest('[hidden]')) || down || view.pending) return this.later(POLL_MS);
    const { ctx, shared } = sharedOf(view, this.edge);
    const t = now();
    if (shared !== this.lastSig) {
      this.lastSig = shared;
      this.changedAt = t;
    }
    const todo = [];
    for (const c of this.cards) {
      if (!c.item.render) continue;
      const key = cardKey(shared, c.item.render);
      if (c.key === key || c.failed === key) continue;
      const hit = cache.get(key);
      if (hit) {
        paint(c.holder, hit);
        c.key = key;
        this.stats.cached += 1;
      } else {
        todo.push({ c, key });
      }
    }
    this.count();
    if (!todo.length) return this.later(POLL_MS * 2);
    const quiet = t - Math.max(this.changedAt, lastInput) >= SETTLE_MS && !view.ed.busy;
    if (!quiet || this.busy) return this.later(POLL_MS / 2);
    const pick = visibleFirst(todo, first);
    this.busy = true;
    this.draw(pick.c, pick.key, ctx).then((wait) => {
      this.busy = false;
      this.later(wait ? 160 : 24);
    });
  }

  // 画一张。返回 true = 要等一等再来（舞台还没稳、数据还没到）
  async draw(card, key, ctx) {
    const view = this.view;
    const ed = view.ed;
    try {
      let result = ed.v2 ? await lookThumb(view, ctx, card.item.render, this.edge) : null;
      if (result === 'wait') return true;
      let img;
      if (result) {
        img = { w: result.w, h: result.h, image: new ImageData(new Uint8ClampedArray(result.data.buffer, result.data.byteOffset, result.data.byteLength), result.w, result.h) };
        this.stats.gpu += 1;
      } else if (ed.v2) {
        img = await serverThumb(view, card.item.render, this.edge);
        this.stats.server += 1;
      } else {
        card.failed = key; // 旧版后端没有小图接口
        return false;
      }
      if (this.dead || view.disposed || view.ed !== ed) return false;
      cache.set(key, img);
      paint(card.holder, img);
      card.key = key;
      this.count();
    } catch (e) {
      if (e && e.name === 'AbortError') return false;
      if (!this.dead && !view.disposed && view.ed === ed) {
        card.failed = key;
        if (!card.key) failedHolder(card.holder);
      }
    }
    return false;
  }
}

// 看得见的先画（面板里滚到哪看哪）
function visibleFirst(todo, anyHolder) {
  const pane = anyHolder.closest && anyHolder.closest('[role="tabpanel"]');
  const box = pane && pane.getBoundingClientRect ? pane.getBoundingClientRect() : null;
  if (!box || !box.height) return todo[0];
  const seen = todo.find(({ c }) => {
    const r = c.holder.getBoundingClientRect();
    return r.bottom > box.top && r.top < box.bottom;
  });
  return seen || todo[0];
}

// 上下键：找下一行 / 上一行里水平位置最近的一张
function vertical(list, at, dir) {
  const cur = list[at].getBoundingClientRect();
  const cx = cur.left + cur.width / 2;
  let best = at;
  let bestRow = Infinity;
  let bestDx = Infinity;
  list.forEach((el, i) => {
    if (i === at) return;
    const r = el.getBoundingClientRect();
    const gap = dir > 0 ? r.top - cur.top : cur.top - r.top;
    if (gap < cur.height / 2) return; // 同一行或在反方向
    const dx = Math.abs(r.left + r.width / 2 - cx);
    if (gap < bestRow - 1 || (Math.abs(gap - bestRow) <= 1 && dx < bestDx)) {
      best = i;
      bestRow = gap;
      bestDx = dx;
    }
  });
  return best;
}

export function newDeck(view) {
  view.lookDeck?.cancel();
  const deck = new Deck(view);
  view.lookDeck = deck;
  return deck;
}

// 测试和测量用
export function clearLookCache() {
  cache.clear();
}
