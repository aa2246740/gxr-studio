// 导出（对话框，⌘E）：一张（同步）或多张（后台任务）。多张的进度在顶栏，关掉对话框也看得到，可以取消。
// 有相机数据时，有 RAW 的照片导出配方效果（电脑预测）；只有 JPG 的导出相机 JPG 加上你做的调整（近似的配方不导出）。
// 导出用的是已经保存的状态，所以导出前先等草稿保存完（beforeSubmit）。
// 尺寸和品质用后端的档位（README：large/medium/small/original = 3000/2048/1600/原始，high/medium/low = 95/85/70）。

import { api } from './api.js';
import { state } from './state.js';
import { h, openModal, fmtBytes, toast, toastError, errorBody, platform, setFraction } from './dom.js';
import { camdataGuide, camdataReady } from './camdata.js';
import { toggle } from './controls.js';
import { flushPhotos } from './saves.js';
import { trackJob } from './jobs.js';

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

// 导出后的文件名（只是给人看的预览，真正的名字由后端定，重名会加 -2、-3）
export function exportName(photo) {
  if (!photo || !photo.name) return '原名_gxr.jpg';
  return `${photo.name.replace(/\.[^.]+$/, '')}_gxr.jpg`;
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
    api('/api/prefs', { method: 'PUT', body: { export_hdr: on } }).then(
      (saved) => { state.prefs = saved; },
      (error) => toastError(error, '无法保存修改。请重试。'),
    );
  }

  const scopeRow = scopes.length > 1
    ? fieldRow('范围', segOf(scopes.map((s) => [s.key, s.label]), scope, (v) => { scope = v; refresh(); }, '范围'))
    : null;

  const destInput = h('input', {
    type: 'text', value: dest, class: 'input wide-input', 'aria-label': '导出到', spellcheck: 'false',
    oninput: (e) => {
      dest = e.target.value;
      if (dest.trim()) destInput.removeAttribute('aria-invalid');
    },
  });

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
  }

  const button = h('button', { class: 'btn primary', type: 'button', onclick: submit }, '导出');
  const cancelBtn = onCancel ? h('button', { class: 'btn', type: 'button', onclick: onCancel }, '取消') : null;
  const result = h('div', { class: 'export-result', role: 'status' });
  const guide = h('div', {});
  const single = ids && ids.length === 1 ? photos.find((p) => p.id === ids[0]) : null;

  const body = h(
    'div',
    { class: 'export-form' },
    scopeRow,
    fieldRow('格式', h('span', {}, 'JPG')),
    fieldRow('尺寸', segOf(SIZES.map(([v, text, title]) => [v, text, title]), size, (v) => { size = v; }, '尺寸')),
    fieldRow('品质', segOf(QUALITIES, qualityTier, (v) => { qualityTier = v; }, '品质')),
    h('div', { class: 'row form' }, h('span', { class: 'lab' }, 'HDR'), hdrField),
    fieldRow('导出到', destInput),
    fieldRow('文件名', h('span', { class: 'dim num' }, single ? exportName(single) : '原名_gxr.jpg')),
    guide,
    result,
    h('div', { class: 'modal-foot' }, cancelBtn, button),
  );

  function refresh() {
    hdrRow();
    guide.replaceChildren(...(camdataReady() ? [] : [camdataGuide({ onDone: refresh })]));
  }
  refresh();

  async function submit() {
    if (!dest.trim()) {
      result.replaceChildren(resultBox('bad', '导出失败。请重试。'));
      return;
    }
    button.disabled = true;
    try {
      if (beforeSubmit) await beforeSubmit();
      const opts = { size, quality_tier: qualityTier, dest_dir: dest.trim(), source: 'auto', precise, hdr };
      if (scope === 'ids' && ids.length === 1) {
        result.replaceChildren(h('p', { class: 'small dim' }, '正在导出…'));
        const r = await api(`/api/photos/${ids[0]}/export`, { method: 'POST', body: opts });
        result.replaceChildren(
          resultBox(
            'ok',
            h('div', {},
              '已导出 1 张',
              h('br'), h('code', {}, r.path), h('br'),
              h('span', { class: 'dim small' }, `${r.width} × ${r.height} · ${fmtBytes(r.bytes)}`),
              h('br'), revealButton(r.export_id),
              r.warning ? h('br') : null,
              r.warning ? h('span', { class: 'small warn-text' }, r.warning) : null,
              r.hdr === false ? h('br') : null,
              r.hdr === false ? h('span', { class: 'small dim' }, r.hdr_note) : null,
            ),
          ),
        );
      } else {
        const body = scope === 'date' ? { ...opts, date } : { ...opts, ids };
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
    } finally {
      button.disabled = false;
      result.scrollIntoView({ block: 'nearest' });
    }
  }

  return body;
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
  return h('button', {
    class: 'btn small',
    type: 'button',
    onclick: async () => {
      try {
        await api(`/api/exports/${exportId}/reveal`, { method: 'POST' });
      } catch (e) {
        toastError(e, '无法在访达中显示。请重试。');
      }
    },
  }, '在访达中显示');
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
  if (!out.isConnected) {
    const btn = ok.length ? revealButton(job.id) : null;
    const actions = btn ? [{ label: revealLabel(), run: () => btn.click() }] : [];
    toast(parts.join('，'), bad.length ? 'error' : 'ok', { actions });
  }
}

// 导出对话框：图库的「导出」、大图的 ⌘E 都从这里打开
export function openExportDialog({ ids, label, date = null, dayLabel = null, photos = null, beforeSubmit = null }) {
  const known = photos || state.photos.slice();
  let close = null;
  // 选中的照片里有刚才在大图里改过、还没写完（或没写进去）的：先等它们，失败就停
  const form = exportForm({
    ids, label, photos: known, date, dayLabel,
    beforeSubmit: beforeSubmit || (() => flushPhotos(ids || [])),
    onCancel: () => close && close(),
  });
  close = openModal('导出', form, { wide: false });
  return close;
}
