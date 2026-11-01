// 导出 JPEG：一张（同步）或多张（后台任务，轮询进度）。
// 有相机数据时，有 RAW 的照片导出配方效果（电脑预测）；只有 JPG 的导出相机 JPG 加上你做的调整（近似的配方不导出）。
// 导出用的是已经保存的状态，所以导出前先等草稿保存完（beforeSubmit，见 viewer.js）。

import { api } from './api.js';
import { state } from './state.js';
import { h, openModal, fmtBytes, toast, toastError, errorBody } from './dom.js';
import { camdataGuide, camdataReady } from './camdata.js';
import { toggle } from './controls.js';

const SIZES = [
  [null, '原始尺寸'],
  [2048, '长边 2048 像素'],
  [4096, '长边 4096 像素'],
];

// HDR（带增益图的 JPEG）要 RAW。选中的照片里一张 RAW 都没有时开关不可用，也不改上次记住的选择。
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

// photos：已知的照片对象（有 has_raw / has_jpg 字段）；ids 是要导出的；date 是"这一天的全部"
// beforeSubmit：提交前要做的事（等草稿保存完）；onCancel：取消（弹窗里用）
export function exportForm({ ids, label, photos = [], date = null, dayLabel = null, beforeSubmit = null, onCancel = null }) {
  const scopes = [];
  if (ids && ids.length) scopes.push({ key: 'ids', label, count: ids.length });
  if (date) scopes.push({ key: 'date', label: dayLabel || '这一天的全部', count: null });

  let scope = scopes[0] ? scopes[0].key : 'ids';
  let longEdge = null;
  let quality = 85;
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

  const scopeGroup = scopes.length > 1
    ? h(
        'div',
        { class: 'field' },
        h('span', { class: 'small dim' }, '范围'),
        scopes.map((s) =>
          h('label', { class: 'small' }, h('input', {
            type: 'radio',
            name: 'exp-scope',
            value: s.key,
            checked: s.key === scope,
            onchange: () => {
              scope = s.key;
              refresh();
            },
          }), ' ', s.label),
        ),
      )
    : null;

  const sizeBtns = SIZES.map(([value, text]) =>
    h('button', {
      class: 'btn',
      type: 'button',
      'aria-pressed': String(value === longEdge),
      onclick: () => {
        longEdge = value;
        sizeBtns.forEach((b, i) => b.setAttribute('aria-pressed', String(SIZES[i][0] === longEdge)));
      },
    }, text),
  );

  const qOut = h('output', {}, `${quality}`);
  const qRange = h('input', {
    type: 'range',
    min: 60,
    max: 100,
    step: 1,
    value: quality,
    'aria-label': '品质',
    oninput: (e) => {
      quality = Number(e.target.value);
      qOut.textContent = `${quality}`;
    },
  });

  const destInput = h('input', {
    type: 'text',
    value: dest,
    style: { width: '100%' },
    'aria-label': '导出到',
    oninput: (e) => {
      dest = e.target.value;
    },
  });

  const hdrField = h('div', { class: 'field' });
  function hdrRow() {
    const ok = hdrAvailable(ids, photos, scope);
    hdr = ok && remembered;
    hdrField.replaceChildren(
      toggle('HDR', hdr, (on) => { if (ok) rememberHdr(on); }, { disabled: !ok }),
      ok ? null : h('div', { class: 'small dim' }, HDR_NO_RAW),
    );
  }

  const button = h('button', { class: 'btn primary', type: 'button', onclick: submit }, '导出');
  const cancelBtn = onCancel ? h('button', { class: 'btn', type: 'button', onclick: onCancel }, '取消') : null;
  const result = h('div', {});
  const guide = h('div', {});

  const body = h(
    'div',
    {},
    scopeGroup,
    h('div', { class: 'field' }, h('span', { class: 'small dim' }, '格式'), h('span', {}, 'JPEG')),
    h('div', { class: 'field' }, h('span', { class: 'small dim' }, '尺寸'), h('div', { class: 'seg' }, sizeBtns)),
    h('div', { class: 'field' }, h('span', { class: 'small dim' }, '品质'), h('div', { class: 'field-row' }, qRange, qOut)),
    hdrField,
    h('div', { class: 'field' }, h('span', { class: 'small dim' }, '导出到'), destInput),
    h('div', { class: 'field' }, h('span', { class: 'small dim' }, '文件名'), h('span', {}, '原名_gxr.jpg')),
    guide,
    h('div', { class: 'modal-foot' }, cancelBtn, button),
    result,
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
      const opts = { long_edge: longEdge, quality, dest_dir: dest.trim(), source: 'auto', precise, hdr };
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
        await followBatch(job, result, photos);
      }
    } catch (e) {
      result.replaceChildren(resultBox('bad', errorBody(e, '导出失败。请重试。')));
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
// export_id：单张导出是响应里的 export_id，批量导出是任务的 id。非 macOS 时后端返回 501，把后端的话原样显示。
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

// 批量导出：轮询 /api/jobs/{id} 直到结束
async function followBatch(job, out, photos = []) {
  const bar = h('i');
  const label = h('span', { class: 'small' }, `正在导出 ${job.progress ? job.progress.done : 0}/${job.progress ? job.progress.total : 0}`);
  out.replaceChildren(h('div', { class: 'progress' }, bar), label);
  let cur = job;
  while (cur.running) {
    await new Promise((r) => setTimeout(r, 500));
    cur = await api(`/api/jobs/${job.id}`);
    const p = cur.progress;
    bar.style.width = p.total ? `${Math.round((p.done / p.total) * 100)}%` : '0%';
    label.textContent = `正在导出 ${p.done}/${p.total}`;
  }
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
  // 完成时 [在访达中显示] 打开这次导出的第一张（COPY 3.6、3.12）：批量用任务的 id
  out.replaceChildren(resultBox(bad.length ? 'bad' : 'ok', h('div', {},
    parts.join(' '),
    bad.length ? h('ul', { class: 'export-list' }, reasons) : null,
    ok.length ? h('br') : null,
    ok.length ? revealButton(job.id) : null,
  )));
}

// 图库里「导出选中的」：弹窗形式
export function openExportDialog({ ids, label, date = null, dayLabel = null }) {
  const photos = state.photos.slice();
  let close = null;
  const form = exportForm({ ids, label, photos, date, dayLabel, onCancel: () => close && close() });
  close = openModal('导出', form, { wide: true });
}
