// GXR Studio 静态资源 Service Worker（构建时由 Vite 注入构建哈希与精确产物白名单）。
// 仅缓存同源 GET 的应用构建产物（/assets/*、WASM、字体、scope 根 HTML 与带 SHA 版本号的默认校准包）。
// 绝不拦截或缓存用户选取的照片、DAT/BIN、LUT、blob:/data: URL 或任何其他源。

const CACHE_PREFIX = 'gxr-studio-v1-';
const CACHE_NAME = '__GXR_SW_CACHE__';
const ENTRY_SCRIPT = '__GXR_SW_ENTRY__';
const CALIB_PATH = '__GXR_SW_CALIB_PATH__';
const CALIB_URL = '__GXR_SW_CALIB_URL__';
const PRECACHE = __GXR_SW_PRECACHE__;
const ALLOWED_ASSETS = new Set(__GXR_SW_ALLOWED__);

function scopeUrl(rel) {
  return new URL(rel || './', self.registration.scope).href;
}

function relativePath(url) {
  const scope = new URL(self.registration.scope);
  if (url.origin !== scope.origin) return null;
  if (!url.pathname.startsWith(scope.pathname)) return null;
  return url.pathname.slice(scope.pathname.length);
}

function precacheInit(rel) {
  if (rel === '') return { cache: 'no-cache', credentials: 'same-origin' };
  if (rel === CALIB_URL) return { cache: 'no-store', credentials: 'same-origin' };
  return { credentials: 'same-origin' };
}

async function installPrecache() {
  const cache = await caches.open(CACHE_NAME);
  try {
    await Promise.all(
      PRECACHE.map(async (rel) => {
        const target = scopeUrl(rel);
        const res = await fetch(target, precacheInit(rel));
        if (!res.ok || res.type !== 'basic' || res.redirected) {
          throw new Error(`precache failed for ${rel || '/'}: status=${res.status} redirected=${res.redirected}`);
        }
        await cache.put(target, res);
      }),
    );
  } catch (err) {
    await caches.delete(CACHE_NAME).catch(() => undefined);
    throw err;
  }
}

function queryClientBuild(client) {
  return new Promise((resolve) => {
    const channel = new MessageChannel();
    const timer = setTimeout(() => resolve(null), 250);
    channel.port1.onmessage = (event) => {
      clearTimeout(timer);
      resolve(typeof event.data?.build === 'string' ? event.data.build : '');
    };
    try {
      client.postMessage({ type: 'gxr-sw-ping' }, [channel.port2]);
    } catch {
      clearTimeout(timer);
      resolve(null);
    }
  });
}

/** 仅删除已无活跃窗口引用的旧版本缓存，保留仍打开旧标签页所需的历史版本缓存直到其退出或重载。 */
async function pruneUnusedCaches() {
  const oldNames = (await caches.keys()).filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME);
  if (!oldNames.length) return;
  const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  const activeBuilds = new Set([CACHE_NAME]);
  let hasUnknownClient = false;
  if (clients.length > 0) {
    const replies = await Promise.all(clients.map((client) => queryClientBuild(client)));
    for (const build of replies) {
      if (build === null || build === '') hasUnknownClient = true;
      else activeBuilds.add(build);
    }
  }
  const toDelete = hasUnknownClient ? [] : oldNames.filter((name) => !activeBuilds.has(name));
  await Promise.all(toDelete.map((name) => caches.delete(name)));
}

self.addEventListener('install', (event) => {
  event.waitUntil(installPrecache().then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim().then(() => pruneUnusedCaches()));
});

self.addEventListener('message', (event) => {
  const type = event.data?.type;
  if (type === 'gxr-sw-hello' || type === 'gxr-sw-bye') {
    event.waitUntil(pruneUnusedCaches());
  }
});

async function handleIndex(request) {
  const rootKey = scopeUrl('');
  const cache = await caches.open(CACHE_NAME);
  try {
    // 透传原始导航请求（保留 redirect: 'manual'），避免把 /index.html -> / 的 302 变成 redirected 响应。
    const res = await fetch(request);
    if (res.ok && res.type === 'basic' && !res.redirected) {
      const html = await res.clone().text();
      if (!ENTRY_SCRIPT || html.includes(ENTRY_SCRIPT)) {
        await cache.put(rootKey, res.clone());
        void pruneUnusedCaches().catch(() => undefined);
      } else {
        // 服务端已更新到新构建：立即交付新 HTML 并在后台更新 SW，不覆写旧版离线缓存。
        void self.registration.update().catch(() => undefined);
      }
    }
    return res;
  } catch (err) {
    const cached = await cache.match(rootKey);
    if (cached && !cached.redirected) return cached;
    throw err;
  }
}

async function handleStatic(request, canonicalRel) {
  const key = scopeUrl(canonicalRel);
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(key);
  if (cached && !cached.redirected) return cached;
  const res = await fetch(request);
  if (res.ok && res.type === 'basic' && !res.redirected) {
    await cache.put(key, res.clone());
  }
  return res;
}

/** 只读检索仍在保留中的旧版本缓存，让已打开的旧标签页继续加载其对应哈希的懒分包，绝不写入非白名单 URL。 */
async function matchRetainedCache(targetUrl) {
  const oldNames = (await caches.keys()).filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME);
  for (const name of oldNames) {
    const cache = await caches.open(name);
    const hit = await cache.match(targetUrl);
    if (hit && !hit.redirected) return hit;
  }
  return null;
}

async function handleRetainedOrNetwork(request, targetUrl) {
  const hit = await matchRetainedCache(targetUrl);
  if (hit) return hit;
  return fetch(request);
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
    const relWithSearch = rel + url.search;
    if (relWithSearch === CALIB_URL) {
      event.respondWith(handleStatic(request, CALIB_URL));
      return;
    }
    if (url.search.startsWith('?v=')) {
      event.respondWith(handleRetainedOrNetwork(request, scopeUrl(relWithSearch)));
    }
    return;
  }

  if (url.search === '' && rel.startsWith('assets/')) {
    if (ALLOWED_ASSETS.has(rel)) {
      event.respondWith(handleStatic(request, rel));
    } else {
      event.respondWith(handleRetainedOrNetwork(request, scopeUrl(rel)));
    }
  }
});
