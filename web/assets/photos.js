// 照片列表：/api/photos 一次最多给 500 张，这里一页一页取到底，不再用 limit=5000 悄悄截断（U12）。
// onPage(list, done)：每取到一页就回调一次（图库先画第一页，后面的接着补）。

import { api } from './api.js';

export const PAGE = 500;

export async function fetchAllPhotos(date = null, { onPage = null, isCurrent = () => true } = {}) {
  const all = [];
  for (let offset = 0; ; offset += PAGE) {
    const q = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    if (date) q.set('date', date);
    const page = await api(`/api/photos?${q}`);
    if (!isCurrent()) return null;
    all.push(...page);
    const done = page.length < PAGE;
    if (onPage) onPage(all, done);
    if (done) return all;
  }
}
