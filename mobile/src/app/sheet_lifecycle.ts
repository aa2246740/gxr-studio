// 底部面板生命周期登记表：切换照片、退出编辑器或清空会话时，统一取消所有挂在宿主上的工作表，
// 清掉未落定的输入防抖定时器和正在进行的异步提交，恢复已落定的模型状态。

import { noteRegistered } from './keys';

const registry = new WeakMap<HTMLElement, Set<() => void>>();

export function registerSheet(host: HTMLElement, onCancel: () => void): () => void {
  let set = registry.get(host);
  if (!set) {
    set = new Set();
    registry.set(host, set);
  }
  set.add(onCancel);
  const live = set;
  noteRegistered(onCancel, () => live.has(onCancel)); // Esc 关最上面的这一层就是「取消」（keys.ts）
  return () => {
    set?.delete(onCancel);
  };
}

export function cancelHostSheets(host: HTMLElement): void {
  const set = registry.get(host);
  if (!set || set.size === 0) return;
  const callbacks = [...set];
  set.clear();
  for (const fn of callbacks) {
    try {
      fn();
    } catch {
      /* 单个面板清理异常不应阻断后续销毁。 */
    }
  }
}

export function activeSheetCount(host: HTMLElement): number {
  return registry.get(host)?.size ?? 0;
}

export interface StageGestureHooks {
  hasPhoto: () => boolean;
  onHoldStart: () => void;
  onHoldEnd: () => void;
  onSwipe: (dir: -1 | 1) => void;
}

export function bindStageGesture(stage: HTMLElement, signal: AbortSignal, hooks: StageGestureHooks): void {
  stage.addEventListener(
    'pointerdown',
    (event) => {
      if (event.button !== 0 || event.clientX < 24) return;
      const startX = event.clientX;
      const startY = event.clientY;
      const pointer = event.pointerId;
      let moved = false;
      let held = false;
      const gAc = new AbortController();
      const abortGesture = () => gAc.abort();
      signal.addEventListener('abort', abortGesture, { once: true });
      const timer = window.setTimeout(() => {
        if (signal.aborted || moved || !hooks.hasPhoto()) return;
        held = true;
        hooks.onHoldStart();
      }, 450);
      gAc.signal.addEventListener('abort', () => window.clearTimeout(timer), { once: true });
      stage.addEventListener(
        'pointermove',
        (ev) => {
          if (ev.pointerId === pointer && Math.hypot(ev.clientX - startX, ev.clientY - startY) > 12) moved = true;
        },
        { signal: gAc.signal },
      );
      const up = (ev: PointerEvent) => {
        if (ev.pointerId !== pointer) return;
        gAc.abort();
        signal.removeEventListener('abort', abortGesture);
        const dx = ev.clientX - startX;
        const dy = ev.clientY - startY;
        // 松手的位置也算移动：页面忙的时候 pointermove 可能晚到，按住计时先到了，横着滑出去仍然算换片。
        if (Math.hypot(dx, dy) > 12) moved = true;
        const swiped = ev.type === 'pointerup' && moved && Math.abs(dx) > 56 && Math.abs(dx) > Math.abs(dy);
        if (held) {
          hooks.onHoldEnd();
          if (!swiped) return;
        }
        if (swiped && hooks.hasPhoto()) hooks.onSwipe(dx < 0 ? 1 : -1);
      };
      stage.addEventListener('pointerup', up, { signal: gAc.signal });
      stage.addEventListener('pointercancel', up, { signal: gAc.signal });
    },
    { signal },
  );
}
