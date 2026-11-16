// 主线程按需加载画布字体：只有真的用到某种边框/时间戳样式才 fetch + FontFace 注册。
// 失败时有严格次数上限与指数退避，绝不空转微任务，也不会在切换/销毁后触发过期回调。

import { FONT_FAMILIES, type FontKey } from '../lib/fonts';
import { FONT_URLS } from '../lib/font_assets';

export const MAX_FONT_ATTEMPTS = 3;
export const FONT_RETRY_BACKOFF_MS = [200, 800, 2500] as const;

const loading = new Map<FontKey, Promise<void>>();
const ready = new Set<FontKey>();
const attempts = new Map<FontKey, number>();
const nextRetryAt = new Map<FontKey, number>();
const exhausted = new Set<FontKey>();

let clock = (): number => Date.now();

export function setFontLoaderHooksForTest(hooks: { clock?: () => number }): void {
  if (hooks.clock) clock = hooks.clock;
}

export function resetFontLoaderForTest(): void {
  loading.clear();
  ready.clear();
  attempts.clear();
  nextRetryAt.clear();
  exhausted.clear();
  clock = () => Date.now();
}

export function fontReady(id: FontKey): boolean {
  return ready.has(id);
}

export function fontCss(id: FontKey, px: number): string {
  return `${px}px "${FONT_FAMILIES[id]}", sans-serif`;
}

export function loadFontFace(id: FontKey): Promise<void> {
  if (ready.has(id)) return Promise.resolve();
  if (exhausted.has(id)) return Promise.reject(new Error(`font_${id}_exhausted`));
  const existing = loading.get(id);
  if (existing) return existing;
  const now = clock();
  const waitUntil = nextRetryAt.get(id) ?? 0;
  if (now < waitUntil) {
    return Promise.reject(new Error(`font_${id}_backoff`));
  }

  const task = (async () => {
    const res = await fetch(FONT_URLS[id], { credentials: 'same-origin' });
    if (!res.ok) throw new Error(`font_${id}_${res.status}`);
    const face = new FontFace(FONT_FAMILIES[id], await res.arrayBuffer());
    await face.load();
    document.fonts.add(face);
    ready.add(id);
    attempts.delete(id);
    nextRetryAt.delete(id);
    exhausted.delete(id);
  })().catch((err) => {
    loading.delete(id);
    const nextCount = (attempts.get(id) ?? 0) + 1;
    attempts.set(id, nextCount);
    if (nextCount >= MAX_FONT_ATTEMPTS) {
      exhausted.add(id);
    } else {
      const delay = FONT_RETRY_BACKOFF_MS[Math.min(nextCount - 1, FONT_RETRY_BACKOFF_MS.length - 1)];
      nextRetryAt.set(id, clock() + delay);
    }
    throw err;
  });
  loading.set(id, task);
  return task;
}

/** 确认用到的字体是否都已注册；没好且未达重试上限/退避期时发起加载，全部好了以后再看 `isAlive` 决定是否调 `onReady`。 */
export function ensureFontsLoaded(
  ids: readonly FontKey[],
  onReady?: () => void,
  isAlive: () => boolean = () => true,
): boolean {
  const missing = ids.filter((id) => !ready.has(id));
  if (!missing.length) return true;
  const now = clock();
  for (const id of missing) {
    if (exhausted.has(id)) continue;
    if (!loading.has(id) && now < (nextRetryAt.get(id) ?? 0)) continue;
    void loadFontFace(id).then(
      () => {
        if (onReady && isAlive()) onReady();
      },
      () => undefined,
    );
  }
  return false;
}
