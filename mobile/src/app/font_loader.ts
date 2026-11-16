// 主线程按需加载画布字体：只有真的用到某种边框/时间戳样式才 fetch + FontFace 注册。

import { FONT_FAMILIES, type FontKey } from '../lib/fonts';
import { FONT_URLS } from '../lib/font_assets';

const loading = new Map<FontKey, Promise<void>>();
const ready = new Set<FontKey>();

export function fontReady(id: FontKey): boolean {
  return ready.has(id);
}

export function fontCss(id: FontKey, px: number): string {
  return `${px}px "${FONT_FAMILIES[id]}", sans-serif`;
}

export function loadFontFace(id: FontKey): Promise<void> {
  if (ready.has(id)) return Promise.resolve();
  const existing = loading.get(id);
  if (existing) return existing;
  const task = (async () => {
    const res = await fetch(FONT_URLS[id], { credentials: 'same-origin' });
    if (!res.ok) throw new Error(`font_${id}_${res.status}`);
    const face = new FontFace(FONT_FAMILIES[id], await res.arrayBuffer());
    await face.load();
    document.fonts.add(face);
    ready.add(id);
  })().catch((err) => {
    loading.delete(id);
    throw err;
  });
  loading.set(id, task);
  return task;
}

/** 确认用到的字体是否都已注册；没好就发起加载，全部好了以后调 `onReady`。 */
export function ensureFontsLoaded(ids: readonly FontKey[], onReady?: () => void): boolean {
  const missing = ids.filter((id) => !ready.has(id));
  if (!missing.length) return true;
  if (onReady) {
    for (const id of missing) {
      void loadFontFace(id).then(
        () => onReady(),
        () => undefined,
      );
    }
  } else {
    for (const id of missing) void loadFontFace(id).catch(() => undefined);
  }
  return false;
}
