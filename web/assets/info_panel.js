// 「信息」标签（I）：拍摄配方、电脑编辑、拍摄信息。只读。
// 拍摄配方来自导入时保存的 EXIF 标签，与电脑编辑分开。

import { h } from './dom.js';
import { loadMeta } from './caps.js';
import { slotName, recipeInfo, KNOB_NAMES } from './preview.js';
import { fmtSigned } from './controls.js';

function kv(rows) {
  return h('dl', { class: 'kv' }, ...rows.filter(Boolean).flatMap(([k, v, cls = '']) => [h('dt', {}, k), h('dd', { class: cls }, v)]));
}

function section(title, ...body) {
  return h('section', { class: 'section' }, h('h3', { class: 'section-head' }, title), h('div', { class: 'section-body' }, ...body));
}

const ADJ_NAMES = {
  exposure_ev: '曝光', contrast: '对比度', highlight: '高光', shadow: '阴影', black_point: '黑点',
  warm: '色温', tint: '色调', saturation: '饱和度', vibrance: '自然饱和度', vignette: '暗角',
};

export function adjustSummary(adj) {
  const parts = Object.entries(adj || {})
    .filter(([, v]) => Number(v) !== 0)
    .map(([k, v]) => `${ADJ_NAMES[k] || k} ${k === 'exposure_ev' ? fmtSigned(Number(v).toFixed(1)) : fmtSigned(Number(v))}`);
  return parts.length ? parts.join(' · ') : null;
}

// 配方的名字：Looks 和相机里的自定义配方用它们自己的名字（slot 只是它们的原厂底子，不能拿来当名字）
export function recipeName(view, r) {
  if (r.kind && r.kind !== 'builtin') {
    const info = recipeInfo(view, r);
    return (info && info.name) || (r.kind === 'look' ? 'Look' : '相机里的配方');
  }
  return slotName(r.unit, r.slot);
}

function recipeText(view) {
  const r = view.ed && view.ed.recipe;
  if (!r) return null;
  const knobs = Object.entries(r.levels || {}).filter(([, v]) => Number(v) !== 0 && v !== 100)
    .map(([k, v]) => `${KNOB_NAMES[k] || k} ${fmtSigned(Number(v))}`);
  return [recipeName(view, r), ...knobs].join(' · ');
}

function editSection(view) {
  const ed = view.ed;
  if (!ed) return section('电脑编辑', h('div', { class: 'skel line' }), h('div', { class: 'skel line' }));
  const crop = view.crop && view.crop.rect && (view.crop.rect.w < 0.999 || view.crop.rect.h < 0.999) ? (view.crop.preset === 'original' ? '自由' : view.crop.preset) : null;
  const adj = adjustSummary(ed.adj);
  const rows = [
    ['配方', recipeText(view) || '无', recipeText(view) ? '' : 'dim'],
    ['LUT', ed.lut ? (ed.lutName || '已选择') : '无', ed.lut ? '' : 'dim'],
    ['调整', [adj, ed.curve ? '曲线' : null].filter(Boolean).join(' · ') || '无', adj || ed.curve ? '' : 'dim'],
    ['裁剪', crop || '原始', crop ? '' : 'dim'],
    ['边框', ed.frame && ed.frame.style !== 'none' ? '有' : '无', ed.frame && ed.frame.style !== 'none' ? '' : 'dim'],
    ['时间戳', ed.stamp && ed.stamp.enabled ? '开' : '关', ed.stamp && ed.stamp.enabled ? '' : 'dim'],
  ];
  return section('电脑编辑', kv(rows));
}

function shotSection(photo, meta) {
  const files = [photo.has_raw ? 'RAW' : null, photo.has_jpg ? 'JPG' : null].filter(Boolean).join(' + ');
  const exposure = meta ? [meta.shutter, meta.aperture, meta.iso ? `ISO ${meta.iso}` : null].filter(Boolean).join(' · ') : '';
  const when = (meta && meta.captured_at) || photo.captured_at;
  const rows = [
    ['镜头', (meta && meta.lens) || photo.lens || '未知'],
    ['曝光', exposure || (photo.iso ? `ISO ${photo.iso}` : '未知'), 'num'],
    ['拍摄时间', when ? when.replace('T', ' ') : '未知', 'num'],
    meta && meta.date_note ? ['', meta.date_note, 'dim small'] : null,
    ['文件', `${photo.name}（${files}）`],
    !photo.recipe_tag && photo.copyright != null ? ['版权', photo.copyright] : null,
    photo.hdr === 'ready' ? ['HDR', '已生成，可在导出时选择'] : null,
    photo.width && photo.height ? ['尺寸', `${photo.width} × ${photo.height}`, 'num'] : null,
  ];
  return section('拍摄信息', kv(rows));
}

export function infoPanel(view) {
  const photo = view.photo;
  const shot = h('div', {}, shotSection(photo, null));
  loadMeta(photo.id).then((res) => {
    if (res.state === 'ok' && shot.isConnected !== false) shot.replaceChildren(shotSection(photo, res.data));
  });
  const tag = photo.recipe_tag;
  const position = tag ? String(tag.position).padStart(2, '0') : '';
  const recorded = tag ? `GXR1 ${tag.catalog_identity} ${tag.unit} ${position}` : '';
  const label = !tag ? '未标记' : tag.name ? `${tag.name}（${tag.unit_label} · ${position}）` : recorded;
  const recipe = section('拍摄配方', kv([
    ['配方', label, tag ? '' : 'dim'],
    tag && !tag.catalogued ? ['', '（相机记录，Studio 目录里没有）', 'dim small'] : null,
    tag && tag.tweaked ? ['', '已微调'] : null,
  ]));
  const el = h('div', { class: 'pane info-pane' }, recipe, editSection(view), shot);
  el.dataset.ready = '1';
  el.sync = () => {
    el.replaceChildren(recipe, editSection(view), shot);
  };
  return el;
}
