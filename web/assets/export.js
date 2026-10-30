// 导出 JPEG：一张（同步）或多张（后台任务，轮询进度）。
// 有相机数据时，有 RAW 的照片导出配方效果（电脑预测）；没有相机数据时，只有 RAW 的导出中性转换，有 JPG 的导出相机 JPG。
// 导出的文件名会带「_电脑预测」（未标定的带「_电脑预测未标定」），界面上一律写明。

import { api } from './api.js';
import { state } from './state.js';
import { h, openModal, fmtBytes } from './dom.js';
import { camdataGuide, camdataReady } from './camdata.js';

const SIZES = [
  [null, '原始'],
  [3000, '长边 3000'],
  [2048, '长边 2048'],
  [1600, '长边 1600'],
];

export function qualityWord(q) {
  if (q >= 90) return '高';
  if (q >= 80) return '中';
  return '低';
}

// 这次导出用哪个画面来源：有相机数据且都有 RAW → 配方效果；否则交给后端的 auto（编辑过或只有 RAW 的出电脑预测，其余用相机 JPG）
function sourceFor(list) {
  return camdataReady() && list.length && list.every((p) => p.has_raw) ? 'recipe' : 'auto';
}

function planText(list) {
  const ready = camdataReady();
  if (!list.length) return '';
  if (sourceFor(list) === 'recipe') {
    return '将导出电脑预测的配方效果（不是相机拍出的 JPG，颜色以相机实测为准）。文件名带「_电脑预测」。';
  }
  const tail = ready ? '' : '还没导入相机数据，所以只有 RAW 的照片导出中性转换（不是相机的颜色）。';
  return `有调整过的照片、或只有 RAW 的照片，导出电脑预测（需要相机数据）；其余导出相机 JPG。${tail}`;
}

// photos：已知的照片对象（有 has_raw / has_jpg 字段）；ids 是要导出的；date 是"这一天的全部"
export function exportForm({ ids, label, photos = [], date = null, dayLabel = null }) {
  const scopes = [];
  if (ids && ids.length) scopes.push({ key: 'ids', label, count: ids.length });
  if (date) scopes.push({ key: 'date', label: dayLabel || `这一天的全部`, count: null });

  let scope = scopes[0] ? scopes[0].key : 'ids';
  let longEdge = null;
  let quality = 85;
  let precise = false;
  let dest = state.settings ? state.settings.export_dir : '';

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
          }), ' ', s.key === 'ids' ? `${s.label}` : s.label),
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

  const qOut = h('output', {}, `${quality}（${qualityWord(quality)}）`);
  const qRange = h('input', {
    type: 'range',
    min: 60,
    max: 100,
    step: 1,
    value: quality,
    'aria-label': '质量',
    oninput: (e) => {
      quality = Number(e.target.value);
      qOut.textContent = `${quality}（${qualityWord(quality)}）`;
    },
  });

  const destInput = h('input', {
    type: 'text',
    value: dest,
    style: { width: '100%' },
    'aria-label': '存到哪个文件夹',
    oninput: (e) => {
      dest = e.target.value;
    },
  });

  const plan = h('div', { class: 'export-plan small' });
  const preciseBox = h('input', {
    type: 'checkbox',
    'aria-label': '精确（更慢）',
    onchange: (e) => {
      precise = e.target.checked;
    },
  });
  const button = h('button', { class: 'btn primary', type: 'button', onclick: submit }, '开始导出');
  const result = h('div', {});
  const guide = h('div', {});

  const body = h(
    'div',
    {},
    scopeGroup,
    h('div', { class: 'field' }, h('span', { class: 'small dim' }, '尺寸'), h('div', { class: 'seg' }, sizeBtns)),
    h('div', { class: 'field' }, h('span', { class: 'small dim' }, '质量（高 95 · 中 85 · 低 70）'), h('div', { class: 'field-row' }, qRange, qOut)),
    h('div', { class: 'field' }, h('span', { class: 'small dim' }, '存到'), destInput),
    plan,
    h('label', { class: 'small' }, preciseBox, ' 精确（更慢）：逐像素计算，全尺寸约 20 秒；默认约 1–2 秒。只对电脑预测的导出有效。'),
    h('p', { class: 'small dim' }, '文件名：原名加 _gxr，例如 R0000207_gxr.jpg；电脑预测的会加 _电脑预测；同名自动加 -2，不覆盖。'),
    guide,
    h('div', { class: 'modal-foot' }, button),
    result,
  );

  function selectedPhotos() {
    if (scope === 'date') return photos.filter((p) => p.date === date);
    return photos.filter((p) => ids.includes(p.id));
  }

  function refresh() {
    plan.textContent = planText(selectedPhotos());
    guide.replaceChildren(...(camdataReady() ? [] : [camdataGuide({ onDone: refresh })]));
  }
  refresh();

  async function submit() {
    if (!dest.trim()) {
      result.replaceChildren(resultBox('bad', '请填写存到哪个文件夹（完整路径）。'));
      return;
    }
    button.disabled = true;
    const opts = { long_edge: longEdge, quality, dest_dir: dest.trim(), source: sourceFor(selectedPhotos()), precise };
    try {
      if (scope === 'ids' && ids.length === 1) {
        result.replaceChildren(h('p', { class: 'small dim' }, '正在导出…'));
        const r = await api(`/api/photos/${ids[0]}/export`, { method: 'POST', body: opts });
        result.replaceChildren(
          resultBox(
            'ok',
            h('div', {},
              r.prediction ? '导出完成（电脑预测）。' : '导出完成。',
              h('br'), h('code', {}, r.path), h('br'),
              h('span', { class: 'dim small' }, `${r.width} × ${r.height} · ${fmtBytes(r.bytes)}`),
              r.prediction && r.render_label ? h('br') : null,
              r.prediction && r.render_label ? h('span', { class: 'small dim' }, r.render_label) : null,
              r.warning ? h('br') : null,
              r.warning ? h('span', { class: 'small warn-text' }, r.warning) : null,
            ),
          ),
        );
      } else {
        const body = scope === 'date' ? { ...opts, date } : { ...opts, ids };
        const job = await api('/api/export', { method: 'POST', body });
        await followBatch(job, result, photos);
      }
    } catch (e) {
      result.replaceChildren(resultBox('bad', e.message));
    } finally {
      button.disabled = false;
      result.scrollIntoView({ block: 'nearest' }); // 结果在面板最下面，别让用户自己去找
    }
  }

  return body;
}

function resultBox(kind, content) {
  return h('div', { class: `result ${kind}` }, content);
}

// 批量导出：轮询 /api/jobs/{id} 直到结束
async function followBatch(job, out, photos = []) {
  const bar = h('i');
  const label = h('span', { class: 'small' }, '正在导出…');
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
  const predicted = ok.filter((r) => r.prediction).length;
  const reasons = [...new Set(bad.map((r) => r.message))];
  const parts = [`导出完成：${ok.length} 张 JPEG 已保存${predicted ? `（其中 ${predicted} 张是电脑预测）` : ''}。`];
  if (bad.length) {
    const names = bad.map((r) => (photos.find((q) => q.id === r.id) || {}).name).filter(Boolean);
    parts.push(`${bad.length} 张没有导出${names.length ? `（${names.join('、')}）` : ''}：${reasons.join('；')}`);
  }
  const warned = ok.filter((r) => r.warning).length;
  if (warned) parts.push(`其中 ${warned} 张的编辑记录坏了或不见了，是按没有裁切、没有编辑导出的（照片本身没事）。`);
  if (p.state === 'cancelled') parts.push('（已取消，未完成的没有导出）');
  out.replaceChildren(resultBox(bad.length ? 'bad' : 'ok', parts.join(' ')));
}

// 图库里"导出选中的"：弹窗形式
export function openExportDialog({ ids, label, date = null, dayLabel = null }) {
  const photos = state.photos.slice();
  const form = exportForm({ ids, label, photos, date, dayLabel });
  openModal('导出 JPEG', form, { wide: true });
}
