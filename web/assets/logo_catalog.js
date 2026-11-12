// 自定义标志目录和图片缓存。和版面算式分开，导出对话框和边框装饰不用拉整份 finish_frame_core。

let logoList = [];
let logoMap = Object.create(null);
let loaded = false;
let logoRev = 0;
let logoFp = '';
let logoEtag = '';
let loadingLogos = null;
let refreshPromise = null;
const imgCache = new Map();
const catalogListeners = new Set();
// 标志图片读失败后按 1、3、10、30 秒再试，之后每 60 秒一次。一轮里不管失败几次只排一次重试，等待期间画图不发请求。
const RETRY_DELAYS_MS = [1000, 3000, 10000, 30000, 60000];
const imageRetry = new Map();
// 等着重试的画图回调（大图叠加层、边框卡片）：到点叫它们重画，重画时才发请求。
// 画布和卡片各用一个固定的回调，反复重画不会越积越多；上限只防泄漏，挤掉的是最久没重画的。
const retryWaiters = new Map();
const MAX_WAITERS = 256;
let retrySeq = 0;

function logoFingerprint(list) {
  return list.map((item) => `${item.id}|${item.name}|${item.w}|${item.h}|${item.coverage}|${item.opaque}|${item.lum}`).join('\n');
}

function emitCatalog() {
  for (const fn of catalogListeners) fn(logoList);
  if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function') {
    window.dispatchEvent(new CustomEvent('gxr-logos'));
  }
}

export function onLogoCatalog(fn) {
  catalogListeners.add(fn);
  return () => catalogListeners.delete(fn);
}

export const catalogListenerCount = () => catalogListeners.size;

export function useLogos(list) {
  const next = Array.isArray(list) ? list.map((x) => ({ ...x })) : [];
  const nextFp = logoFingerprint(next);
  if (loaded && nextFp === logoFp) return logoList;
  logoList = next;
  logoMap = Object.create(null);
  for (const item of logoList) if (item && item.id) logoMap[item.id] = item;
  for (const key of [...imgCache.keys()]) if (!logoMap[key]) imgCache.delete(key);
  for (const key of [...imageRetry.keys()]) if (!logoMap[key]) forgetRetry(key);
  for (const key of [...retryWaiters.keys()]) if (!logoMap[key]) retryWaiters.delete(key);
  loaded = true;
  logoRev += 1;
  logoFp = nextFp;
  emitCatalog();
  return logoList;
}

export const logosLoaded = () => loaded;
export const logosRevision = () => logoRev;
export const getLogoList = () => logoList;
export const getLogoMap = () => logoMap;

export function refreshLogos() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    const headers = {};
    if (logoEtag) headers['If-None-Match'] = logoEtag;
    const res = await fetch('/api/logos', { credentials: 'same-origin', cache: 'no-store', headers });
    if (res.status === 304) return false;
    if (!res.ok) throw new Error(`http_${res.status}`);
    const etag = res.headers.get('ETag');
    if (etag) logoEtag = etag;
    const before = logoFp;
    const data = await res.json();
    useLogos((data && data.logos) || []);
    return logoFp !== before;
  })().finally(() => {
    refreshPromise = null;
  });
  return refreshPromise;
}

export function ensureLogosLoaded() {
  if (loaded) return Promise.resolve(logoList);
  if (!loadingLogos) {
    loadingLogos = refreshLogos().then(() => logoList).catch((e) => {
      loadingLogos = null;
      throw e;
    });
  }
  return loadingLogos;
}

function announceImageState() {
  if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function') {
    window.dispatchEvent(new CustomEvent('gxr-logo-image-retry'));
  }
}

function addWaiter(id, fn) {
  if (typeof fn !== 'function') return;
  let set = retryWaiters.get(id);
  if (!set) {
    set = new Set();
    retryWaiters.set(id, set);
  }
  set.delete(fn);
  set.add(fn);
  while (set.size > MAX_WAITERS) set.delete(set.values().next().value);
}

function wakeWaiters(id) {
  const set = retryWaiters.get(id);
  if (!set) return;
  retryWaiters.delete(id);
  for (const fn of set) fn();
}

function pokeLogoImages(id) {
  if (typeof document === 'undefined') return;
  const needle = `/api/logos/${encodeURIComponent(id)}/image`;
  for (const img of document.querySelectorAll('img')) {
    const src = img.getAttribute('src') || '';
    if (!src.includes(needle)) continue;
    img.src = `${src.split('?')[0]}?retry=${retrySeq}`;
  }
}

function forgetRetry(id) {
  const state = imageRetry.get(id);
  if (!state) return false;
  if (state.timer) clearTimeout(state.timer);
  imageRetry.delete(id);
  return true;
}

function dropRemovedLogo(id) {
  if (!loaded || logoMap[id]) return;
  retryWaiters.delete(id);
  if (forgetRetry(id)) announceImageState();
}

// 到点（或用户点了「现在重试」）：图片标签换个地址重新取，等着的画布重画一次，重画时再发请求。
function retryDue(id) {
  const state = imageRetry.get(id);
  if (!state) return;
  if (state.timer) clearTimeout(state.timer);
  state.timer = null;
  state.nextAt = 0;
  retrySeq += 1;
  pokeLogoImages(id);
  wakeWaiters(id);
}

function scheduleLogoRetry(id) {
  let state = imageRetry.get(id);
  const first = !state;
  if (!state) {
    state = { failures: 0, nextAt: 0, timer: null };
    imageRetry.set(id, state);
  }
  // 这一轮已经排了重试（画布和面板里的小图同时失败），不再加码。
  if (state.timer) return;
  state.failures += 1;
  const delay = RETRY_DELAYS_MS[Math.min(state.failures - 1, RETRY_DELAYS_MS.length - 1)];
  state.nextAt = Date.now() + delay;
  state.timer = setTimeout(() => retryDue(id), delay);
  // 文件真被删了：目录刷新后目录监听会改回 RICOH，这里不再重试。
  refreshLogos().then(() => dropRemovedLogo(id), () => {});
  if (first) announceImageState();
}

export const logoImageFailing = (id) => !!id && imageRetry.has(id);

export function retryLogoImageNow(id) {
  retryDue(id);
}

// 面板里的标志小图（<img>）读失败：排一轮重试。画布用的图片自己管自己，这里不动它的缓存。
export function noteLogoImageError(id) {
  scheduleLogoRetry(id);
}

// 面板里的小图读出来了：说明图片又能取了，等着的画布马上重画，失败提示收起。
export function noteLogoImageLoaded(id) {
  if (!forgetRetry(id)) return;
  wakeWaiters(id);
  announceImageState();
}

export function customLogoImage(id, onReady) {
  if (!id || typeof Image !== 'function') return null;
  let rec = imgCache.get(id);
  if (!rec) {
    const state = imageRetry.get(id);
    if (state && Date.now() < state.nextAt) {
      addWaiter(id, onReady);
      return null;
    }
    const img = new Image();
    rec = { img, loaded: false, waiting: [] };
    imgCache.set(id, rec);
    img.addEventListener('load', () => {
      rec.loaded = true;
      const recovered = forgetRetry(id);
      for (const fn of rec.waiting.splice(0)) fn();
      wakeWaiters(id);
      if (recovered) announceImageState();
    });
    img.addEventListener('error', () => {
      if (imgCache.get(id) === rec) imgCache.delete(id);
      for (const fn of rec.waiting.splice(0)) addWaiter(id, fn);
      scheduleLogoRetry(id);
    });
    const url = `/api/logos/${encodeURIComponent(id)}/image`;
    img.src = state ? `${url}?retry=${retrySeq}` : url;
  }
  if (rec.loaded) return rec.img;
  if (onReady && !rec.waiting.includes(onReady)) rec.waiting.push(onReady);
  return null;
}
