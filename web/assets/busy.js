// 加载态的分档（PR「加载态与过渡」）：
//   < 150ms：什么都不显示，免得一闪而过；
//   150ms–1s：原地的轻反馈（按钮里转圈并锁住、细进度条、骨架）；
//   > 1s：写出进度（n/N、百分比）或阶段文字；
//   后台任务（批量导出、导入）：可以取消，进度留在顶栏（jobs.js），结束后用带操作的提示告诉结果。
//   单张导出、重建索引这类一次请求做完的操作不能中途取消：对话框关掉也照常做完，完成或失败时用提示告诉结果。
// 这里只放和 DOM 无关或只碰一个元素的小工具，方便用 node 单独测（tests/test_busy.py）。

import { outOfView, toastError } from './dom.js';

export const SHOW_AFTER_MS = 150;
export const DETAIL_AFTER_MS = 1000;

// 延迟执行：ms 之内 cancel() 了就什么都不做。返回 { cancel, fired }。
export function delayed(fn, ms = SHOW_AFTER_MS, { setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
  let fired = false;
  const id = setTimer(() => {
    fired = true;
    fn();
  }, ms);
  return {
    cancel() {
      clearTimer(id);
    },
    get fired() {
      return fired;
    },
  };
}

// 开发时模拟慢操作：localStorage 里 gxr-studio.dev-delay = 毫秒数（只影响 api.js 的请求，见那里）
export function devDelay() {
  try {
    const n = Number(globalThis.localStorage?.getItem('gxr-studio.dev-delay'));
    return Number.isFinite(n) && n > 0 ? Math.min(n, 10000) : 0;
  } catch {
    return 0;
  }
}

// 异步按钮：点了以后锁住（防连点），超过 150ms 才显示转圈；结束后恢复。
// 失败时在按钮旁边写原因并给「重试」（onError 返回 false 时不写，由调用方自己处理）。
// 按钮所在的对话框已经关了（或正在退场）：旁边的字没人看得见，改用带「重试」的提示（N04）。
// 返回 task 的结果；锁住期间再点直接返回 undefined，不会重复发请求。
export async function busyButton(btn, task, { onError = null, busyText = null } = {}) {
  if (!btn || btn.dataset.busy === '1') return undefined;
  btn.dataset.busy = '1';
  btn.setAttribute('aria-disabled', 'true');
  const label = btn.querySelector ? btn.querySelector('span') : null;
  const before = label ? label.textContent : null;
  // 150ms：只转圈；1 秒还没完、给了 busyText：写出阶段文字（「正在重建…」）
  const spin = delayed(() => btn.setAttribute('aria-busy', 'true'));
  const phase = busyText && label ? delayed(() => {
    label.textContent = busyText;
    btn.dataset.phase = 'text';
  }, DETAIL_AFTER_MS) : null;
  clearInlineError(btn);
  try {
    return await task();
  } catch (e) {
    if (!onError || onError(e) !== false) {
      if (outOfView(btn)) toastError(e, '没有完成。请重试。', { action: { label: '重试', run: () => retryDetached(task) } });
      else showInlineError(btn, e, () => busyButton(btn, task, { onError, busyText }));
    }
    return undefined;
  } finally {
    spin.cancel();
    phase?.cancel();
    delete btn.dataset.phase;
    btn.dataset.busy = '';
    btn.removeAttribute('aria-busy');
    btn.removeAttribute('aria-disabled');
    if (label && before !== null) label.textContent = before;
  }
}

// 对话框关了以后从提示里重试：没有按钮可锁，失败就再给一个提示
function retryDetached(task) {
  return Promise.resolve().then(task).catch((e) => {
    toastError(e, '没有完成。请重试。', { action: { label: '重试', run: () => retryDetached(task) } });
  });
}

function clearInlineError(btn) {
  const next = btn.nextElementSibling;
  if (next && next.classList && next.classList.contains('inline-error')) next.remove();
}

function showInlineError(btn, e, retry) {
  const doc = btn.ownerDocument || globalThis.document;
  if (!doc || !btn.parentNode) return;
  const box = doc.createElement('span');
  box.className = 'inline-error';
  box.setAttribute('role', 'alert');
  const text = doc.createElement('span');
  text.textContent = (e && e.message) || '没有完成。请重试。';
  const again = doc.createElement('button');
  again.type = 'button';
  again.className = 'btn sm ghost';
  again.textContent = '重试';
  again.addEventListener('click', () => {
    box.remove();
    retry();
  });
  box.append(text, again);
  btn.after(box);
}

// 乐观更新：先改界面（apply），再保存（commit）；保存失败就把界面改回去（rollback）并把错误交给 onError。
export async function optimistic({ apply, commit, rollback, onError = null }) {
  apply();
  try {
    return await commit();
  } catch (e) {
    rollback();
    if (onError) onError(e);
    return undefined;
  }
}
