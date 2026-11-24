// GXR Studio 静态资源 Service Worker（构建时由 Vite 注入构建哈希与精确产物白名单）。
// 仅缓存同源 GET 的应用构建产物（/assets/*、WASM、字体、index.html 与公开的默认校准包）。
// 绝不拦截或缓存用户选取的照片、DAT/BIN、LUT、blob:/data: URL 或任何其他源。

const CACHE_PREFIX = 'gxr-studio-v1-';
const CACHE_NAME = '__GXR_SW_CACHE__';
const ENTRY_SCRIPT = '__GXR_SW_ENTRY__';
const CALIB_PATH = '__GXR_SW_CALIB__';
const PRECACHE = __GXR_SW_PRECACHE__;
const ALLOWED_ASSETS = new Set(__GXR_SW_ALLOWED__);

function scopeUrl(rel) {
  return new URL(rel, self.registration.scope).href;
}

function relativePath(url) {
  const scope = new URL(self.registration.scope);
  if (url.origin !== scope.origin) return null;
  if (!url.pathname.startsWith(scope.pathname)) return null;
  return url.pathname.slice(scope.pathname.length);
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) =>
        Promise.all(
          PRECACHE.map(async (rel) => {
            const target = scopeUrl(rel);
            const res = await fetch(
              target,
              rel === 'index.html' ? { cache: 'no-cache', credentials: 'same-origin' } : { credentials: 'same-origin' },
            );
            if (res.ok && res.type === 'basic') {
              await cache.put(target, res);
            }
          }),
        ),
      )
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names
            .filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME)
            .map((name) => caches.delete(name)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

async function handleIndex(request) {
  const key = scopeUrl('index.html');
  const cache = await caches.open(CACHE_NAME);
  try {
    const res = await fetch(request.url, { cache: 'no-cache', credentials: 'same-origin' });
    if (res.ok && res.type === 'basic') {
      const html = await res.clone().text();
      if (!ENTRY_SCRIPT || html.includes(ENTRY_SCRIPT)) {
        await cache.put(key, res.clone());
      } else {
        // 服务端已更新到新构建：立即交付新 HTML 并在后台更新 SW，不覆写旧版离线缓存。
        void self.registration.update().catch(() => undefined);
      }
    }
    return res;
  } catch (err) {
    const cached = await cache.match(key);
    if (cached) return cached;
    throw err;
  }
}

async function handleStatic(request, canonicalRel) {
  const key = scopeUrl(canonicalRel);
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(key);
  if (cached) return cached;
  const res = await fetch(request);
  if (res.ok && res.type === 'basic') {
    await cache.put(key, res.clone());
  }
  return res;
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  let url;
  try {
    url = new URL(request.url);
  } catch {
    return;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return;

  const rel = relativePath(url);
  if (rel === null) return;

  if (rel === '' || rel === 'index.html') {
    event.respondWith(handleIndex(request));
    return;
  }

  if (rel === CALIB_PATH) {
    event.respondWith(handleStatic(request, CALIB_PATH));
    return;
  }

  if (url.search === '' && ALLOWED_ASSETS.has(rel)) {
    event.respondWith(handleStatic(request, rel));
  }
});
