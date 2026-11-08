// 导出（对话框，⌘E）：一张（同步）或多张（后台任务）。多张的进度在顶栏，关掉对话框也看得到，可以取消。
// 有相机数据时，有 RAW 的照片导出配方效果（电脑预测）；只有 JPG 的导出相机 JPG 加上你做的调整（近似的配方不导出）。
// 导出用的是已经保存的状态，所以导出前先等草稿保存完（beforeSubmit）。
// 尺寸和品质用后端的档位（README：large/medium/small/original = 3000/2048/1600/原始，high/medium/low = 95/85/70）。

import { api } from './api.js';
import { state } from './state.js';
import { h, openModal, fmtBytes, toast, toastError, errorBody, platform, setFraction, outOfView } from './dom.js';
import { camdataGuide, camdataReady } from './camdata.js';
import { toggle } from './controls.js';
import { flushPhotos, flushDate } from './saves.js';
import { trackJob } from './jobs.js';
import { delayed, busyButton, SHOW_AFTER_MS, DETAIL_AFTER_MS } from './busy.js';

export const SIZES = [
  ['original', '原始尺寸'],
  ['large', '大', '长边 3000 像素'],
  ['medium', '中', '长边 2048 像素'],
  ['small', '小', '长边 1600 像素'],
];
export const QUALITIES = [
  ['high', '高'],
  ['medium', '中'],
  ['low', '低'],
];

// HDR（带增益图的 JPG）要 RAW。选中的照片里一张 RAW 都没有时开关不可用，也不改上次记住的选择。
// 按日期导出时不知道当天有没有 RAW，开关照常可用，没有 RAW 的那几张照常导出、结果里说明。
// 开关的勾选记在应用数据里，下次导出沿用；第一次默认关。
export const HDR_NO_RAW = 'HDR 需要 RAW 文件';
export function hdrAvailable(ids, photos, scope) {
  if (scope === 'date') return true;
  return (ids || []).some((id) => {
    const p = photos.find((q) => q.id === id);
    return !p || p.has_raw;
  });
}

// 导出响应里的 render_label 可能是代码（prediction / approx / original），也可能是中文。代码转成 COPY 的写法。
const LABEL_TEXT = { prediction: '电脑预测', approx: '近似', original: '原图' };
export function labelText(raw) {
  return LABEL_TEXT[raw] || raw || '';
}

export function qualityWord(q) {
  if (q >= 90) return '高';
  if (q >= 80) return '中';
  return '低';
}

// 导出后的文件名（只是给人看的预览，真正的名字由后端定，重名会加 -2、-3）：
// 普通 原名_gxr.jpg；没有相机 JPG 又没导入相机配方文件、画的是中性转换：原名_gxr_neutral.jpg；
// 做成了 HDR：后面再加 _hdr（原名_gxr_hdr.jpg、原名_gxr_neutral_hdr.jpg）。
// HDR 只有 RAW 才做得成，只有 JPG 的照片一定是普通名字。有 RAW 的照片做不做得成，要等导出时才知道
// （图库里的 HDR 状态说的是相机 JPG 那一版，套了配方的导出不看它），所以不按它猜。
export function exportName(photo, { hdr = false, neutral = false } = {}) {
  const stem = photo && photo.name ? photo.name.replace(/\.[^.]+$/, '') : '原名';
  const tag = `${neutral ? '_neutral' : ''}${hdr && hdrPossible(photo) ? '_hdr' : ''}`;
  return `${stem}_gxr${tag}.jpg`;
}

function hdrPossible(photo) {
  return !photo || photo.has_raw !== false;
}

export const NAME_SUFFIX_NOTE = '重名时自动加 -2、-3，不会盖掉已有的文件。';

// 对话框里「文件名」一行。单张写具体的名字；开了 HDR、照片有 RAW 时，括号里写做不成 HDR 时的名字。
// 多张写通用的写法：开了 HDR 时哪些带 _hdr；没导入相机配方文件时，只有 RAW 的照片是中性转换（_neutral）。
export function exportNameText({ single, hdr, camdata }) {
  if (single) {
    const neutral = single.has_jpg === false && !camdata;
    const name = exportName(single, { hdr, neutral });
    if (!hdr || !hdrPossible(single)) return name;
    return `${name}（做不成 HDR 时是 ${exportName(single, { neutral })}）`;
  }
  if (!hdr) return camdata ? '原名_gxr.jpg' : '原名_gxr.jpg（只有 RAW 的照片是 原名_gxr_neutral.jpg）';
  const raw = camdata ? '' : '；只有 RAW 的照片是 原名_gxr_neutral_hdr.jpg，没做成 HDR 时是 原名_gxr_neutral.jpg';
  return `原名_gxr_hdr.jpg（没做成 HDR 的照片是 原名_gxr.jpg${raw}）`;
}

// 「在访达中显示」只有 macOS 有（其它系统后端返回 501）：别的系统上不放这个按钮，改成「复制路径」（U14）
export function revealLabel() {
  return platform() === 'mac' ? '在访达中显示' : null;
}

function fieldRow(label, ...ctl) {
  return h('div', { class: 'row form' }, h('span', { class: 'lab' }, label), h('div', { class: 'ctl' }, ...ctl));
}

function segOf(options, value, onPick, label) {
  const btns = options.map(([v, text, title]) => h('button', {
    type: 'button', 'aria-pressed': String(v === value), title: title || null,
    onclick: () => {
      for (const b of btns) b.setAttribute('aria-pressed', String(b === btn(v)));
      onPick(v);
    },
  }, text));
  const btn = (v) => btns[options.findIndex((o) => o[0] === v)];
  return h('div', { class: 'seg block', role: 'group', 'aria-label': label }, btns);
}

// photos：已知的照片对象（有 has_raw / has_jpg 字段）；ids 是要导出的；date 是"这一天的全部"
// beforeSubmit：提交前要做的事（等草稿保存完）；onCancel：取消（关对话框）；onStarted：批量任务已经交给后台
export function exportForm({ ids, label, photos = [], date = null, dayLabel = null, beforeSubmit = null, onCancel = null, onStarted = null }) {
  const scopes = [];
  if (ids && ids.length) scopes.push({ key: 'ids', label, count: ids.length });
  if (date) scopes.push({ key: 'date', label: dayLabel || '这一天的全部', count: null });

  let scope = scopes[0] ? scopes[0].key : 'ids';
  let size = 'original';
  let qualityTier = 'high';
  // 精确（API 的 precise）界面不提供，固定用默认的烘焙表（COPY 3.10）
  const precise = false;
  let dest = state.settings ? state.settings.export_dir : '';
  let remembered = !!(state.prefs && state.prefs.export_hdr);
  let hdr = false;

  function rememberHdr(on) {
    remembered = on;
    hdr = on;
    const prefs = state.prefs || (state.prefs = {});
    prefs.export_hdr = on;
    paintName();
    api('/api/prefs', { method: 'PUT', body: { export_hdr: on } }).then(
      (saved) => { state.prefs = saved; },
      (error) => toastError(error, '没有记住 HDR 的选择，下次导出要重新打开。'),
    );
  }

  const scopeRow = scopes.length > 1
    ? fieldRow('范围', segOf(scopes.map((s) => [s.key, s.label]), scope, (v) => { scope = v; refresh(); }, '范围'))
    : date ? fieldRow('范围', h('span', {}, scopes[0].label)) : null;

  const destInput = h('input', {
    type: 'text', value: dest, class: 'input wide-input', 'aria-label': '导出到', spellcheck: 'false',
    oninput: (e) => {
      dest = e.target.value;
      if (dest.trim()) destInput.removeAttribute('aria-invalid');
    },
  });

  const nameText = h('span', { class: 'dim num' });
  const single = ids && ids.length === 1 ? photos.find((p) => p.id === ids[0]) : null;
  const paintName = () => { nameText.textContent = exportNameText({ single: scope === 'ids' ? single : null, hdr, camdata: camdataReady() }); };

  const hdrField = h('div', { class: 'ctl' });
  function hdrRow() {
    const ok = hdrAvailable(ids, photos, scope);
    hdr = ok && remembered;
    // 不能把 null 交给 replaceChildren：它会变成页面上的文字「null」（U3）
    const sw = toggle('HDR', hdr, (on) => { if (ok) rememberHdr(on); }, { disabled: !ok });
    // 行标题已经写了 HDR：开关自己的文字只留给读屏，看得见的位置写它是什么
    if (sw.firstChild && sw.firstChild.classList) sw.firstChild.classList.add('sr-only');
    sw.append(h('span', { class: 'small dim' }, ok ? '带增益图的 JPG，支持 HDR 的屏幕上高光更亮' : HDR_NO_RAW));
    const nodes = [sw];
    hdrField.replaceChildren(...nodes);
    paintName();
  }

  const button = h('button', { class: 'btn primary', type: 'button', onclick: submit }, '导出');
  const cancelBtn = onCancel ? h('button', { class: 'btn', type: 'button', onclick: onCancel }, '取消') : null;
  const result = h('div', { class: 'export-result', role: 'status' });
  const guide = h('div', {});

  const body = h(
    'div',
    { class: 'export-form' },
    scopeRow,
    fieldRow('格式', h('span', {}, 'JPG')),
    fieldRow('尺寸', segOf(SIZES.map(([v, text, title]) => [v, text, title]), size, (v) => { size = v; }, '尺寸')),
    fieldRow('品质', segOf(QUALITIES, qualityTier, (v) => { qualityTier = v; }, '品质')),
    h('div', { class: 'row form' }, h('span', { class: 'lab' }, 'HDR'), hdrField),
    fieldRow('导出到', destInput),
    fieldRow('文件名', h('div', {}, nameText, h('div', { class: 'small dim' }, NAME_SUFFIX_NOTE))),
    guide,
    result,
    h('div', { class: 'modal-foot' }, cancelBtn, button),
  );

  function refresh() {
    hdrRow();
    // 没导入也能导出，只是不套相机配方：写明白，免得和下面的「已导出」看起来矛盾
    guide.replaceChildren(...(camdataReady() ? [] : [
      camdataGuide({ onDone: refresh }),
      h('p', { class: 'small dim' }, '未导入相机配方文件时，导出不会带上相机配方效果：有相机 JPG 的照片导出相机 JPG；只有 RAW、又套用了配方的照片可能导不出。'),
    ]));
  }
  refresh();

  let busy = false;
  let spin = null;
  // 对话框关掉以后（含 160ms 退场动画期间）结果改用提示告诉用户：看的是对话框有没有关，不看节点还在不在页面上（N04）
  let closed = false;
  let running = null;
  // 点了马上锁住（防连点）；150ms 以后才转圈，免得快的时候一闪
  // 用 aria-disabled 而不是 disabled：禁用正拿着焦点的按钮，浏览器会把焦点丢到对话框外面（F06）
  function lock(on) {
    busy = on;
    if (on) button.setAttribute('aria-disabled', 'true');
    else button.removeAttribute('aria-disabled');
    spin?.cancel();
    spin = null;
    if (on) spin = delayed(() => button.setAttribute('aria-busy', 'true'), SHOW_AFTER_MS);
    else button.setAttribute('aria-busy', 'false');
  }

  async function submit() {
    if (busy) return;
    if (!dest.trim()) {
      destInput.setAttribute('aria-invalid', 'true');
      result.replaceChildren(resultBox('bad', '请填写导出位置（文件夹的完整路径）。'));
      destInput.focus();
      return;
    }
    if (scope === 'ids' && !(ids && ids.length)) {
      result.replaceChildren(resultBox('bad', '没有选中照片。请先在图库里选择要导出的照片。'));
      return;
    }
    // 点导出这一刻把范围和选项定下来：等保存的过程中再切范围也不影响这一次，等的和导的是同一批（N01）
    const sub = {
      scope,
      ids: scope === 'ids' ? [...ids] : [],
      date: scope === 'date' ? date : null,
      opts: { size, quality_tier: qualityTier, dest_dir: dest.trim(), source: 'auto', precise, hdr },
    };
    running = sub;
    lock(true);
    result.replaceChildren();
    // 开始以后关掉对话框不会停下导出（单张做完会提示，多张进度在顶栏），按钮叫「关闭」而不是「取消」
    if (cancelBtn) cancelBtn.textContent = '关闭';
    try {
      // 按这次真正要导出的范围等保存（F02）：这一天的全部要等这一天所有改过的照片，不只是打开对话框时的那几张
      if (beforeSubmit) await beforeSubmit({ scope: sub.scope, ids: sub.ids, date: sub.date });
      const { opts } = sub;
      if (sub.scope === 'ids' && sub.ids.length === 1) {
        // 单张超过 1 秒（大尺寸、HDR）：写出在做什么，不让人以为卡住了
        const slow = delayed(() => result.replaceChildren(h('div', { class: 'progress indet' }, h('i')),
          h('span', { class: 'small' }, opts.hdr ? '正在导出，生成 HDR 版本要多等几秒…' : '正在导出…')), DETAIL_AFTER_MS);
        let r;
        try {
          r = await api(`/api/photos/${sub.ids[0]}/export`, { method: 'POST', body: opts });
        } finally {
          slow.cancel();
        }
        result.replaceChildren(resultBox('ok', singleResult(r)));
        // 单张导出不能中途取消；对话框已经关了就用提示告诉结果（F10）
        if (closed) singleToast(r);
      } else {
        const body = sub.scope === 'date' ? { ...opts, date: sub.date } : { ...opts, ids: sub.ids };
        const job = await api('/api/export', { method: 'POST', body });
        if (onStarted) onStarted(job);
        await followBatch(job, result, photos);
      }
    } catch (e) {
      // 有修改没写进去（R05）：导出停止，不拿服务器上的旧编辑出图；给「重试保存」，保存成功后可以再导出
      if (e && e.retrySave) {
        const retry = h('button', { class: 'btn sm', type: 'button', onclick: async () => {
          retry.disabled = true;
          const ok = await e.retrySave();
          retry.disabled = false;
          if (ok) result.replaceChildren(resultBox('ok', '修改已保存，可以导出了。'));
        } }, '重试保存');
        result.replaceChildren(resultBox('bad', h('div', {}, errorBody(e, '有修改没有保存。'), h('p', { class: 'small' }, '导出已停止，没有生成文件。'), retry)));
      } else {
        result.replaceChildren(resultBox('bad', errorBody(e, '导出失败。请重试。')));
      }
      // 对话框关了：「重试保存」跟着提示走，不留在看不见的表单里（V05）；重试的结果由 retrySaves 自己提示
      if (closed) {
        if (e && e.retrySave) toastError(e, '有修改没有保存，导出已停止。', { action: { label: '重试保存', run: () => e.retrySave() } });
        else toastError(e, '导出失败。请重试。');
      }
    } finally {
      running = null;
      lock(false);
      if (!closed && result.scrollIntoView) result.scrollIntoView({ block: 'nearest' });
    }
  }

  // 对话框在单张导出途中被关掉：告诉用户它会照常做完（openExportDialog 的 onClose 调用）
  body.closedWhileBusy = () => {
    closed = true;
    if (busy && running && running.scope === 'ids' && running.ids.length === 1) toast('导出会照常做完，完成后会提示你。', 'info');
  };
  return body;
}

function singleToast(r) {
  const btn = revealButton(r.export_id);
  const actions = btn ? [{ label: revealLabel(), run: () => btn.click() }]
    : (r.path && globalThis.navigator && navigator.clipboard ? [{ label: '复制路径', run: () => copyPath(r.path) }] : []);
  toast(`已导出 1 张：${r.path}`, 'ok', { actions });
}

function copyPath(path) {
  return navigator.clipboard.writeText(path).then(() => toast('已复制路径', 'ok'), (e) => toastError(e, '无法复制路径。'));
}

function singleResult(r) {
  return h('div', {},
    h('p', { class: 'res-main' }, '已导出 1 张'),
    h('p', { class: 'small num path' }, r.path),
    h('p', { class: 'small dim num' }, `${r.width} × ${r.height} · ${fmtBytes(r.bytes)}`),
    r.warning ? h('p', { class: 'small warn-text' }, r.warning) : null,
    r.hdr === false ? h('p', { class: 'small dim' }, r.hdr_note) : null,
    h('div', { class: 'res-acts' }, revealButton(r.export_id) || copyPathButton(r.path)));
}

// 批量导出里没做成 HDR 的张数（没有 RAW、对不齐…），照常导出了 SDR
export function hdrSummary(n) {
  return `${n} 张未生成 HDR`;
}

function resultBox(kind, content) {
  return h('div', { class: `result ${kind}` }, content);
}

// 在访达中显示（COPY 3.6、3.12）：打开这次导出的第一张。POST /api/exports/{export_id}/reveal，请求体为空。
// export_id：单张导出是响应里的 export_id，批量导出是任务的 id。只有 macOS 有这个按钮（其它系统返回 null）。
function revealButton(exportId) {
  if (!revealLabel() || !exportId) return null;
  const btn = h('button', {
    class: 'btn sm',
    type: 'button',
    onclick: () => busyButton(btn, () => api(`/api/exports/${exportId}/reveal`, { method: 'POST' }), {
      onError: (e) => { toastError(e, '无法在访达中显示。文件可能已被移动。'); return false; },
    }),
  }, h('span', {}, '在访达中显示'));
  return btn;
}

function copyPathButton(path) {
  if (!path || !(globalThis.navigator && navigator.clipboard)) return null;
  return h('button', {
    class: 'btn sm', type: 'button',
    onclick: () => copyPath(path),
  }, '复制路径');
}

// 批量导出：进度交给顶栏（jobs.js）。对话框还开着就在里面显示结果，关掉了就用提示条（带「在访达中显示」）。
async function followBatch(job, out, photos = []) {
  const bar = h('i');
  const label = h('span', { class: 'small num' }, '正在准备导出…');
  const paint = (cur) => {
    const p = cur.progress || {};
    setFraction(bar, p.total ? p.done / p.total : 0);
    label.textContent = p.total ? `正在导出 ${p.done}/${p.total}` : '正在准备导出…';
  };
  paint(job);
  out.replaceChildren(h('div', { class: 'progress' }, bar), label, h('p', { class: 'small dim' }, '可以关闭这个对话框，进度会留在右上角。'));
  const cur = await trackJob(job, { label: '导出', onProgress: paint });
  const p = cur.progress;
  const ok = p.results.filter((r) => !r.error);
  const bad = p.results.filter((r) => r.error);
  const parts = [`已导出 ${ok.length} 张`];
  const warned = ok.filter((r) => r.warning).length;
  if (warned) parts.push('无法读取编辑记录，已按原图显示');
  const noHdr = ok.filter((r) => r.hdr === false).length;
  if (noHdr) parts.push(hdrSummary(noHdr));
  if (p.state === 'cancelled') parts.push(`已取消，已导出的 ${ok.length} 张已保留`);
  // 每一张失败的原因都留着（R2-06）：后端的主句原样显示，有 detail 的进详细信息
  const reasons = bad.map((r) => {
    const name = (photos.find((q) => q.id === r.id) || {}).name;
    return h('li', {}, errorBody({ message: name ? `${name}：${r.message}` : r.message, data: r.detail ? { detail: r.detail } : null }, '导出失败。请重试。'));
  });
  if (bad.length) parts.push(`${bad.length} 张没有导出`);
  // 完成时 [在访达中显示] 打开这次导出的第一张（COPY 3.6、3.12）：批量用任务的 id
  out.replaceChildren(resultBox(bad.length ? 'bad' : 'ok', h('div', {},
    h('p', { class: 'res-main' }, parts.join('，')),
    bad.length ? h('ul', { class: 'export-list' }, reasons) : null,
    ok.length ? h('div', { class: 'res-acts' }, revealButton(job.id)) : null,
  )));
  // 对话框关了（含退场动画那 160ms）就用提示：看是不是还在用户眼前，不看节点还在不在页面上（V04）
  if (outOfView(out)) {
    const btn = ok.length ? revealButton(job.id) : null;
    const actions = btn ? [{ label: revealLabel(), run: () => btn.click() }] : [];
    toast(parts.join('，'), bad.length ? 'error' : 'ok', { actions });
  }
}

// 导出前的保存关卡：选中的照片 → 等这几张；这一天的全部 → 等这一天所有这次改过的照片
export function barrierFor({ scope, ids, date }) {
  return scope === 'date' ? flushDate(date) : flushPhotos(ids || []);
}

// 导出对话框：图库的「导出」、大图的 ⌘E 都从这里打开
export function openExportDialog({ ids, label, date = null, dayLabel = null, photos = null, beforeSubmit = null }) {
  const known = photos || state.photos.slice();
  let close = null;
  // 选中的照片里有刚才在大图里改过、还没写完（或没写进去）的：先等它们，失败就停
  const form = exportForm({
    ids, label, photos: known, date, dayLabel,
    beforeSubmit: beforeSubmit || barrierFor,
    onCancel: () => close && close(),
  });
  close = openModal('导出', form, { wide: false, onClose: () => form.closedWhileBusy?.() });
  return close;
}
