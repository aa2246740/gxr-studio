// 插卡导入：发现卡 → 自动开始导入（只读复制）→ 轮询进度 → 横幅汇总。卡上的文件从不被改动。

import { api } from './api.js';
import { state, emit } from './state.js';
import { h, toast, toastError } from './dom.js';
import { doneStatusText, hasProblems, multiCardNote, summarizeImport } from './import_summary.js';

// 本次会话里已经自动处理过的卡（卡拔掉后会移出，插回来可以再导入缺的部分）
const handled = new Set();

export function cardPresent() {
  return !!state.card;
}

// 空状态里的「导入照片」按钮：有卡就导入第一张
export function importNow() {
  if (cardPresent()) startImport();
  else toast('找不到存储卡。请重新插入后重试。', 'error');
}

export function isImporting() {
  return !!(state.job && state.job.running);
}

export async function refreshCards() {
  let cards;
  try {
    cards = await api('/api/cards');
  } catch (e) {
    setOffline(e);
    return;
  }
  setOnline();
  const card = cards[0] || null;
  await checkAccess(cards);
  const prevId = state.card ? state.card.id : null;
  state.cards = cards;
  state.card = card;
  if (!card) {
    handled.clear();
    state.cardSummary = null;
    state.cardMsg = null;
  } else if (!prevId || prevId !== card.id) {
    await loadSummary(card);
    if (!handled.has(card.id) && !isImporting()) {
      if (state.cardSummary && state.cardSummary.new_shots > 0) {
        startImport(card);
      } else {
        handled.add(card.id);
        state.cardMsg = '这张存储卡上的照片都已导入';
      }
    }
  }
  renderBanner();
  renderCardStatus();
}

// 没找到卡时，看看是不是系统不让读（macOS 没给「可移除的卷」权限）：这种情况要明说，不能一直写"未插入"
async function checkAccess(cards) {
  if (cards.length) {
    state.cardAccess = null;
    return;
  }
  try {
    const st = await api('/api/cards/status');
    state.cardAccess = st.access_denied ? st.message : null;
  } catch {
    state.cardAccess = null;
  }
}

async function loadSummary(card) {
  try {
    state.cardSummary = await api(`/api/cards/${card.id}/summary`);
  } catch (e) {
    state.cardSummary = null;
    toastError(e, '无法完成操作。请重试。');
  }
}

export async function startImport(card = state.card) {
  if (!card) return;
  handled.add(card.id);
  try {
    state.job = await api('/api/import', { method: 'POST', body: { card_id: card.id } });
    state.dismissedJobId = null;
    state.cardMsg = null;
  } catch (e) {
    if (e.code === 'import_running') {
      await loadLatestJob();
      return;
    }
    toastError(e, '无法完成操作。请重试。');
    return;
  }
  renderBanner();
  renderCardStatus();
}

export async function loadLatestJob() {
  try {
    const jobs = await api('/api/jobs');
    const job = jobs.find((j) => j.kind === 'import');
    if (job) {
      state.job = job;
      // 刷新页面时，已经结束的旧任务不再弹横幅
      if (!job.running) state.dismissedJobId = job.id;
    }
  } catch (e) {
    setOffline(e);
  }
}

// 每秒调用一次：只有导入中才请求进度
export async function tickImport() {
  const job = state.job;
  if (!job || !job.running) return;
  let fresh;
  try {
    fresh = await api(`/api/jobs/${job.id}`);
  } catch (e) {
    setOffline(e);
    return;
  }
  const wasRunning = job.running;
  state.job = fresh;
  if (wasRunning && !fresh.running) {
    emit('library-changed');
    if (state.card) await loadSummary(state.card);
    if (fresh.progress.state === 'done') {
      // 完整导完后卡上的内容都已导入，下次插卡看到的是"都导入过了"；
      // 有文件没读进来时不算处理完：关掉提示后仍会出现「开始导入」，可以重试
      if (hasProblems(fresh.progress)) handled.delete(state.card?.id);
      else handled.add(state.card?.id);
    }
    renderCardStatus();
  }
  renderBanner();
  renderCardStatus();
}

export function cancelImport() {
  const job = state.job;
  if (!job || !job.running) return;
  api(`/api/jobs/${job.id}/cancel`, { method: 'POST' }).catch((e) => toastError(e, '无法完成操作。请重试。'));
}

export function dismissBanner() {
  if (state.job) state.dismissedJobId = state.job.id;
  state.cardMsg = null;
  renderBanner();
}

function setOffline(e) {
  if (e.code === 'offline') {
    state.offline = true;
    renderCardStatus();
  }
}

function setOnline() {
  state.offline = false;
}

// 横幅：导入中 > 导入结束汇总 > 发现卡 > 无
export function renderBanner() {
  const slot = document.getElementById('banner');
  slot.replaceChildren();
  const job = state.job;
  const p = job ? job.progress : null;

  if (job && job.running) {
    const msg =
      p.state === 'scanning' || !p.total_shots
        ? '正在读取…'
        : `正在导入 ${p.done_shots}/${p.total_shots}`;
    slot.append(
      h(
        'div',
        { class: 'banner' },
        h('div', { class: 'msg' }, msg),
        h('button', { class: 'btn', type: 'button', onclick: cancelImport }, '取消'),
      ),
    );
    return;
  }

  if (job && p && job.id !== state.dismissedJobId) {
    const r = summarize(job);
    slot.append(
      h(
        'div',
        { class: `banner ${r.kind}` },
        h('div', { class: 'msg' }, r.text, r.detail ? h('div', { class: 'small dim' }, r.detail) : null),
        r.action,
        h('button', { class: 'btn ghost', type: 'button', onclick: dismissBanner, title: '关闭' }, '关闭'),
      ),
    );
    appendNotes(slot);
    return;
  }

  if (state.cardMsg) {
    slot.append(
      h(
        'div',
        { class: 'banner ok' },
        h('div', { class: 'msg' }, state.cardMsg),
        h('button', { class: 'btn ghost', type: 'button', onclick: dismissBanner }, '关闭'),
      ),
    );
    appendNotes(slot);
    return;
  }

  // 这张卡已经处理过（导入过或正在处理）就不再提示"发现相机卡"，状态栏里还能看到
  if (state.card && !isImporting() && !handled.has(state.card.id)) {
    slot.append(
      h(
        'div',
        { class: 'banner' },
        h('div', { class: 'msg' }, '已插入存储卡'),
        h('button', { class: 'btn', type: 'button', onclick: () => startImport() }, '导入'),
      ),
    );
  }
  appendNotes(slot);
}

// 导入进行中以外的提示：不止一张卡、系统不让读卡
function appendNotes(slot) {
  const multi = multiCardNote(state.cards);
  if (multi) slot.append(h('div', { class: 'banner', dataset: { note: 'multi-card' } }, h('div', { class: 'msg' }, multi)));
  if (!state.card && state.cardAccess) {
    slot.append(h('div', { class: 'banner bad', dataset: { note: 'card-access' } }, h('div', { class: 'msg' }, state.cardAccess)));
  }
}

function summarize(job) {
  const r = summarizeImport(job.progress);
  // 没有卡时不能继续，写明原因而不是放一个点了没反应的按钮
  const retry = state.card
    ? h('button', { class: 'btn', type: 'button', onclick: () => startImport() }, '导入')
    : h('span', { class: 'dim small' }, '请重新插入存储卡');
  let action = h('span');
  // 导入完成（可以推出）时提示条只留结果和 [关闭]；推出的说法只在状态栏里写（「存储卡：可以推出」）
  if (r.retry) action = retry;
  return { ...r, action };
}

export function renderCardStatus() {
  const el = document.getElementById('card-status');
  if (state.offline) {
    el.textContent = '无法连接到 GXR Studio。请确认启动窗口仍在运行，然后刷新页面。';
    el.className = 'offline';
    return;
  }
  el.className = '';
  if (isImporting()) {
    const p = state.job.progress;
    el.textContent = `正在导入 ${p.done_shots}/${p.total_shots}`;
    return;
  }
  if (!state.card) {
    el.textContent = state.cardAccess ? '需要访问存储卡的权限。请在「系统设置 › 隐私与安全性 › 文件与文件夹」中允许。' : '存储卡：未插入';
    return;
  }
  const job = state.job;
  if (job && job.progress.state === 'done' && job.id !== state.dismissedJobId) {
    el.textContent = doneStatusText(job.progress);
  } else {
    el.textContent = '存储卡：已插入';
  }
}
