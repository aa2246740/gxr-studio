// 插卡导入：发现卡 → 自动开始导入（只读复制）→ 轮询进度 → 横幅汇总。卡上的文件从不被改动。

import { api } from './api.js';
import { state, emit } from './state.js';
import { h, toast, toastError, setFraction, platform } from './dom.js';
import { doneStatusText, ejectWords, hasProblems, importToast, multiCardNote, progressLine, summarizeImport } from './import_summary.js';

// 本次会话里已经自动处理过的卡（卡拔掉后会移出，插回来可以再导入缺的部分）。键是挂载会话号（card_id）
const handled = new Set();
// 已经自动发起过导入的卡。发现新卡时如果别的卡还在导入，就先不记，等空闲了再判断一次（不只在 id 刚变时判断）
const autoTried = new Set();

// 用户自己按「导入照片」发起的任务：结束时弹一条提示，说清楚发生了什么（包括"没有新照片"）
let manualJobId = null;

// 这个导入任务是哪张卡的（任务和结果都带着来源挂载会话号）
function jobCard(job) {
  return job && job.progress ? job.progress.card_id || null : null;
}

// 导入结果只给它自己那张卡看：换了卡，旧卡的"都已导入""可以推出"不能出现在新卡上
function jobIsForCurrentCard(job) {
  return !state.card || jobCard(job) === state.card.id;
}

// 当前这张卡还没自动导入过、也没有别的导入在跑：现在发起
function maybeAutoImport() {
  const card = state.card;
  if (!card || autoTried.has(card.id) || isImporting()) return;
  autoTried.add(card.id);
  startImport(card);
}

export function cardPresent() {
  return !!state.card;
}

// 空状态里的「导入照片」按钮：有卡就导入第一张
export function importNow() {
  if (cardPresent()) startImport(state.card, { manual: true });
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
    autoTried.clear();
    state.cardSummary = null;
    state.cardMsg = null;
  } else {
    if (!prevId || prevId !== card.id) await loadSummary(card);
    // 只看文件名、大小证明不了"都已导入"：每张卡（每个挂载会话）都导入一次，导入会读内容核对，结束提示按核对结果说
    maybeAutoImport();
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
    toastError(e, '无法读取存储卡上的照片列表。请重试。');
  }
}

export async function startImport(card = state.card, { manual = false } = {}) {
  if (!card) return;
  handled.add(card.id);
  try {
    state.job = await api('/api/import', { method: 'POST', body: { card_id: card.id } });
    state.dismissedJobId = null;
    state.cardMsg = null;
    if (manual) manualJobId = state.job.id;
  } catch (e) {
    if (e.code === 'import_running') {
      await loadLatestJob();
      if (manual && state.job?.running) manualJobId = state.job.id;
      renderCardStatus();
      return;
    }
    // 任务没建起来：回到可以重试的状态，横幅里出现「导入」按钮（不自动连续重发）
    handled.delete(card.id);
    toastError(e, '导入没有开始。请重试。');
    renderBanner();
    renderCardStatus();
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
      // 核对通过才算处理完；有文件没读进来、核对不了时不算：关掉提示后仍会出现「开始导入」，可以重试。
      // 按任务自己的来源卡记，不按"现在插着的卡"记：导入期间换了卡，结果不能算到新卡头上
      const done = jobCard(fresh);
      if (hasProblems(fresh.progress)) handled.delete(done);
      else if (done) handled.add(done);
    }
    // 导入期间插上的另一张卡：现在空闲了，轮到它
    maybeAutoImport();
    if (manualJobId === fresh.id) {
      manualJobId = null;
      const t = importToast(fresh.progress);
      toast(ejectWords(t.text, platform()), t.kind);
    }
    renderCardStatus();
  }
  renderBanner();
  renderCardStatus();
}

export function cancelImport() {
  const job = state.job;
  if (!job || !job.running) return;
  api(`/api/jobs/${job.id}/cancel`, { method: 'POST' }).catch((e) => toastError(e, '没有取消成功。请重试。'));
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

// ---- 顶栏的存储卡状态（.cardchip）和它的弹出面板（#card-pop 里的 #banner） -------------------------
// 原来页面顶上的横幅和底部状态栏合并成这一个：平时只是一枚小标签，需要处理时（发现新卡、导入有问题）自动展开面板。

let attention = null; // 上次自动展开面板时的原因，同一件事只自动展开一次

function popEl() {
  return document.getElementById('card-pop');
}

export function setCardPopover(open) {
  const pop = popEl();
  const chip = document.getElementById('card-chip');
  if (!pop) return;
  // 面板的右边和存储卡标签对齐
  if (open && chip?.getBoundingClientRect && pop.style) {
    const r = chip.getBoundingClientRect();
    pop.style.right = `${Math.max(12, Math.round(globalThis.innerWidth - r.right))}px`;
  }
  pop.hidden = !open;
  chip?.setAttribute?.('aria-expanded', String(!!open));
}

function cardPopoverOpen() {
  const pop = popEl();
  return !!pop && pop.hidden === false;
}

// 顶栏的存储卡按钮：点一下开 / 关；点面板外面或按 Esc 关（app.js 启动时调用一次）
export function bindCardChip() {
  const chip = document.getElementById('card-chip');
  if (!chip || !chip.addEventListener) return;
  chip.addEventListener('click', () => setCardPopover(!cardPopoverOpen()));
  document.addEventListener('pointerdown', (e) => {
    if (!cardPopoverOpen()) return;
    const pop = popEl();
    if (pop.contains(e.target) || chip.contains(e.target)) return;
    setCardPopover(false);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !cardPopoverOpen()) return;
    e.stopPropagation();
    setCardPopover(false);
    chip.focus();
  }, true);
}

function autoOpen(reason) {
  if (!reason || reason === attention) return;
  attention = reason;
  setCardPopover(true);
}

// 面板内容：导入中 > 导入结束汇总 > 发现卡 > 无
export function renderBanner() {
  const slot = document.getElementById('banner');
  slot.replaceChildren();
  const job = state.job;
  const p = job ? job.progress : null;

  if (job && job.running) {
    const line = progressLine(p);
    const bar = h('i', {});
    setFraction(bar, line.fraction ?? 0);
    slot.append(
      h(
        'div',
        { class: 'banner' },
        h('div', { class: 'msg' }, line.text, h('div', { class: `progress${line.fraction === null ? ' indet' : ''}` }, bar),
          h('div', { class: 'small dim' }, '只读复制，卡上的文件不会被改动。')),
        h('div', { class: 'acts' }, h('button', { class: 'btn sm', type: 'button', onclick: cancelImport }, '取消导入')),
      ),
    );
    return;
  }

  if (job && p && job.id !== state.dismissedJobId && jobIsForCurrentCard(job)) {
    const r = summarize(job);
    slot.append(
      h(
        'div',
        { class: `banner ${r.kind}` },
        h('div', { class: 'msg' }, r.text, r.detail ? h('div', { class: 'small dim' }, r.detail) : null),
        h('div', { class: 'acts' }, r.action, h('button', { class: 'btn sm ghost', type: 'button', onclick: dismissBanner, title: '关闭' }, '关闭')),
      ),
    );
    appendNotes(slot);
    if (r.kind === 'bad') autoOpen(`bad:${job.id}`);
    return;
  }

  if (state.cardMsg) {
    slot.append(
      h(
        'div',
        { class: 'banner ok' },
        h('div', { class: 'msg' }, state.cardMsg),
        h('div', { class: 'acts' }, h('button', { class: 'btn sm ghost', type: 'button', onclick: dismissBanner }, '关闭')),
      ),
    );
    appendNotes(slot);
    return;
  }

  // 这张卡已经处理过（导入过或正在处理）就不再提示"发现相机卡"，顶栏里还能看到
  if (state.card && !isImporting() && !handled.has(state.card.id)) {
    slot.append(
      h(
        'div',
        { class: 'banner' },
        h('div', { class: 'msg' }, '已插入存储卡', h('div', { class: 'small dim' }, `「${state.card.label}」`)),
        h('div', { class: 'acts' }, h('button', { class: 'btn sm primary', type: 'button', onclick: () => startImport() }, '导入')),
      ),
    );
    autoOpen(`card:${state.card.id}`);
  }
  appendNotes(slot);
}

// 导入进行中以外的提示：不止一张卡、系统不让读卡
function appendNotes(slot) {
  const multi = multiCardNote(state.cards);
  if (multi) slot.append(h('div', { class: 'banner', dataset: { note: 'multi-card' } }, h('div', { class: 'msg' }, multi)));
  if (!state.card && state.cardAccess) {
    slot.append(h('div', { class: 'banner bad', dataset: { note: 'card-access' } }, h('div', { class: 'msg' }, state.cardAccess)));
    autoOpen('access');
  }
}

function summarize(job) {
  const s = summarizeImport(job.progress);
  const r = { ...s, text: ejectWords(s.text, platform()) };
  // 没有卡时不能继续，写明原因而不是放一个点了没反应的按钮
  const retry = state.card
    ? h('button', { class: 'btn sm primary', type: 'button', onclick: () => startImport() }, '导入')
    : h('span', { class: 'dim small' }, '请重新插入存储卡');
  let action = null;
  // 导入完成（可以推出）时只留结果和 [关闭]；推出的说法只在顶栏的存储卡标签里写（「存储卡：可以推出」）
  if (r.retry) action = retry;
  return { ...r, action };
}

// 顶栏标签：文字 + 状态（className）+ 导入进度环（--p）
function paintChip(kind, pct = null) {
  const chip = document.getElementById('card-chip');
  if (!chip) return;
  chip.className = `cardchip${kind ? ` ${kind}` : ''}`;
  const ico = document.getElementById('card-icon');
  if (!ico) return;
  // 导入中：进度环（不知道总数时转圈）；有卡：小圆点；没有卡：空心卡片
  if (kind === 'importing') {
    let ring = ico.firstChild && ico.firstChild.classList?.contains('ring') ? ico.firstChild : null;
    if (!ring) {
      ring = h('span', { class: 'ring' });
      ico.replaceChildren(ring);
    }
    ring.classList?.toggle('indet', pct === null);
    ring.style?.setProperty?.('--p', String(pct ?? 0));
    return;
  }
  const want = kind === 'ok' ? 'dot' : kind === 'bad' || kind === 'offline' ? 'dot bad' : 'glyph';
  if (ico.firstChild && ico.firstChild.className === want) return;
  ico.replaceChildren(h('span', { class: want }));
}

// 其他页面（相机页）想跟着导入状态变：状态有变化才通知，一秒一次的轮询不会让按钮被反复重画
let lastSignature = '';
function announceImportState() {
  const p = state.job?.progress;
  const sig = [state.card?.id, state.job?.id, state.job?.running, p?.state, p?.phase, p?.done_shots, p?.total_shots, state.dismissedJobId === state.job?.id].join('|');
  if (sig === lastSignature) return;
  lastSignature = sig;
  emit('import-state');
}

export function renderCardStatus() {
  announceImportState();
  const el = document.getElementById('card-status');
  if (state.offline) {
    el.textContent = '无法连接到 GXR Studio';
    el.className = 'offline';
    paintChip('offline');
    document.getElementById('card-chip')?.setAttribute?.('title', '请确认启动窗口仍在运行，然后刷新页面。');
    return;
  }
  el.className = '';
  document.getElementById('card-chip')?.setAttribute?.('title', '存储卡与导入');
  if (isImporting()) {
    const p = state.job.progress;
    const line = progressLine(p);
    el.textContent = line.text;
    paintChip('importing', line.fraction === null ? null : Math.round(line.fraction * 100));
    return;
  }
  if (!state.card) {
    el.textContent = state.cardAccess ? '需要访问存储卡的权限' : '存储卡：未插入';
    paintChip(state.cardAccess ? 'bad' : 'empty');
    return;
  }
  const job = state.job;
  if (job && job.progress.state === 'done' && job.id !== state.dismissedJobId && jobIsForCurrentCard(job)) {
    el.textContent = ejectWords(doneStatusText(job.progress), platform());
    paintChip(hasProblems(job.progress) ? 'bad' : 'ok');
  } else {
    el.textContent = '存储卡：已插入';
    paintChip('ok');
  }
}
