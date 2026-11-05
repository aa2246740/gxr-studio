// 顶栏右侧的后台任务（批量导出等）：进度 n/N、可以取消；关掉对话框后进度还在顶栏（U13）。
// 轮询：同一时间只有一个请求在路上；页面藏到后台时暂停，回来立刻刷新一次（U15）。

import { api } from './api.js';
import { h, icon, toastError } from './dom.js';

const POLL_MS = 600;
const tracked = new Map(); // job id → { job, label, resolve, reject, cancelling }
let timer = null;
let inFlight = false;

function slot() {
  return document.getElementById('tb-jobs');
}

function pct(p) {
  return p && p.total ? Math.round((p.done / p.total) * 100) : 0;
}

export function jobText(label, p) {
  if (!p || !p.total) return `正在${label}…`;
  return `正在${label} ${p.done}/${p.total}`;
}

function render() {
  const el = slot();
  if (!el) return;
  const items = [...tracked.values()].map((t) => {
    const p = t.job.progress || {};
    const ring = h('span', { class: 'ring', 'aria-hidden': 'true' });
    ring.style.setProperty('--p', String(pct(p)));
    const cancel = h('button', {
      type: 'button', class: 'btn icon sm', title: `取消${t.label}`, 'aria-label': `取消${t.label}`,
      'aria-disabled': t.cancelling ? 'true' : null,
      onclick: () => cancelJob(t.job.id),
    }, icon('x'));
    return h('div', { class: 'jobchip', role: 'status' }, ring, h('span', { class: 'num' }, t.cancelling ? '正在取消…' : jobText(t.label, p)), cancel);
  });
  el.replaceChildren(...items);
  el.hidden = items.length === 0;
}

export function cancelJob(id) {
  const t = tracked.get(id);
  if (!t || t.cancelling) return;
  t.cancelling = true;
  render();
  api(`/api/jobs/${id}/cancel`, { method: 'POST' }).catch((e) => {
    t.cancelling = false;
    render();
    toastError(e, `无法取消${t.label}。请再试一次。`);
  });
}

async function poll() {
  timer = null;
  if (inFlight || !tracked.size) return;
  if (document.hidden) return; // 回到前台时 visibilitychange 会重新开始
  inFlight = true;
  for (const [id, t] of [...tracked]) {
    try {
      const fresh = await api(`/api/jobs/${id}`);
      t.job = fresh;
      t.onProgress?.(fresh);
      if (!fresh.running) {
        tracked.delete(id);
        t.resolve(fresh);
      }
    } catch (e) {
      // 断网就下次再问；任务没了（服务重启）就结束
      if (e && e.status === 404) {
        tracked.delete(id);
        t.reject(e);
      }
    }
  }
  inFlight = false;
  render();
  schedule();
}

function schedule() {
  if (timer || !tracked.size || document.hidden) return;
  timer = setTimeout(poll, POLL_MS);
}

if (typeof document !== 'undefined' && document.addEventListener) {
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && tracked.size && !inFlight) {
      clearTimeout(timer);
      timer = null;
      poll();
    }
  });
}

// 跟踪一个后台任务，直到结束。返回结束时的任务（state = done / cancelled / failed）
export function trackJob(job, { label, onProgress = null } = {}) {
  return new Promise((resolve, reject) => {
    if (!job.running) {
      resolve(job);
      return;
    }
    tracked.set(job.id, { job, label, resolve, reject, onProgress, cancelling: false });
    render();
    schedule();
  });
}

export function runningJobs() {
  return tracked.size;
}
