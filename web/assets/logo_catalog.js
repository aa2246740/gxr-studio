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

export function useLogos(list) {
  const next = Array.isArray(list) ? list.map((x) => ({ ...x })) : [];
  const nextFp = logoFingerprint(next);
  if (loaded && nextFp === logoFp) return logoList;
  logoList = next;
  logoMap = Object.create(null);
  for (const item of logoList) if (item && item.id) logoMap[item.id] = item;
  for (const key of [...imgCache.keys()]) if (!logoMap[key]) imgCache.delete(key);
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

export function customLogoImage(id, onReady) {
  if (!id || typeof Image !== 'function') return null;
  let rec = imgCache.get(id);
  if (!rec) {
    const img = new Image();
    rec = { img, loaded: false, waiting: [] };
    imgCache.set(id, rec);
    img.addEventListener('load', () => {
      rec.loaded = true;
      for (const fn of rec.waiting.splice(0)) fn();
    });
    img.addEventListener('error', () => {
      imgCache.delete(id);
      if (logoMap[id]) useLogos(logoList.filter((item) => item.id !== id));
    });
    img.src = `/api/logos/${encodeURIComponent(id)}/image`;
  }
  if (rec.loaded) return rec.img;
  if (onReady) rec.waiting.push(onReady);
  return null;
}
