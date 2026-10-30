// 新接口的探测和降级（IMPLEMENTATION_PLAN 第 4 节）。
// 每个接口的结果是：{ state: 'ok', data } | { state: 'missing' }（404，电脑端还没有这个接口）| { state: 'error', message }。
// 探测只做一次，结果留在会话里。新接口不在时，对应的面板显示「还没准备好」，不写假选项。

import { api, ApiError } from './api.js';

const cache = new Map();

// what：出错时说明读的是什么（COPY 3.10：通用错误要写出对象）
async function probe(path, what) {
  try {
    return { state: 'ok', data: await api(path) };
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return { state: 'missing' };
    return { state: 'error', message: e.message || `无法读取${what}。请重试。` };
  }
}

function cached(key, path, what) {
  if (!cache.has(key)) cache.set(key, probe(path, what));
  return cache.get(key);
}

// 边框和时间戳的选项（样式列表、格式、字体、颜色）。它存在，就说明这一版电脑端支持新的编辑项。
export const loadFrames = () => cached('frames', '/api/frames', '边框');

export async function v2Ready() {
  return (await loadFrames()).state === 'ok';
}

// 默认的边框和时间戳设置
export const loadDefaults = () => cached('defaults', '/api/defaults/finish', '边框和时间戳');

export async function saveDefaults(body) {
  const data = await api('/api/defaults/finish', { method: 'PUT', body });
  cache.set('defaults', Promise.resolve({ state: 'ok', data }));
  return data;
}

// 每张照片各自的能力（能不能套配方）和拍摄信息。按照片缓存。
const photoCache = new Map();

export function loadPhotoCaps(id) {
  const key = `caps:${id}`;
  if (!photoCache.has(key)) photoCache.set(key, probe(`/api/photos/${id}/capabilities`, '配方'));
  return photoCache.get(key);
}

export function loadMeta(id) {
  const key = `meta:${id}`;
  if (!photoCache.has(key)) photoCache.set(key, probe(`/api/photos/${id}/meta`, '拍摄信息'));
  return photoCache.get(key);
}

// 旧版后端的配方能力：没有 RAW 就不能套配方（和 /capabilities 的 none 一样）；有 RAW 就是完整配方
export function fallbackCaps(photo) {
  if (photo && photo.has_raw) return { raw: true, recipe: 'full', reason: '' };
  return {
    raw: false,
    recipe: 'none',
    reason: '配方需要 RAW 文件',
  };
}
