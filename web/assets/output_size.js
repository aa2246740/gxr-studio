// 成品尺寸：照片的全尺寸裁剪框只问一次，边框选项在本机用共享版面重算。
import { api } from './api.js';
import { h } from './dom.js';
import { state } from './state.js';
import { flushPhotos } from './saves.js';
import { cropRatio, OUTPUT_RATIOS } from './finish_layout.js';
import { makeDeco } from './stage_finish.js';
import { photoSize } from './output_photo_size.js';

export function ratioText(ratio) {
  if (!ratio) return '';
  const [w, h] = ratio;
  if (w === 191 && h === 100) return '1.91:1';
  if (w === 100 && h === 191) return '1:1.91';
  if (w === 47 && h === 20) return '2.35:1';
  if (w === 20 && h === 47) return '1:2.35';
  if (w === 8 && h === 5) return '16:10';
  if (w === 5 && h === 8) return '10:16';
  return `${w}:${h}`;
}

export function sizeText(size, ratio) {
  return `${size[0]} × ${size[1]} 像素${ratio ? ` · ${ratioText(ratio)}` : ''}`;
}

export function frameSizeLine(view) {
  const line = h('p', { class: 'small dim num frame-size', role: 'status', 'aria-live': 'polite' }, '正在计算成品尺寸…');
  const entry = photoSize(view);
  function paint() {
    if (view.disposed || !view.ed || !entry.value) return;
    const data = entry.value;
    const frame = view.ed.frame;
    const [w, h] = data.photo;
    const deco = makeDeco({ meta: { frame: data.meta } }, frame, null, w, h, paint, data.crop_preset);
    if (!deco && frame.style !== 'none') return;
    const size = deco ? [deco.width, deco.height] : [w, h];
    const ratio = frame.style === 'none' ? cropRatio(data.crop_preset, w, h) : OUTPUT_RATIOS[frame.output_ratio];
    line.textContent = `成品 ${sizeText(size, ratio)}`;
  }
  if (entry.value) paint();
  else entry.promise.then(paint, () => {
    line.replaceChildren('无法读取成品尺寸。', h('button', { type: 'button', class: 'btn sm', onclick: () => {
      view.outputSize = null;
      view.redrawSide?.();
    } }, '重试'));
  });
  line.update = paint;
  return line;
}

export function exportSizePreview({ ids, getScope, getSize }) {
  const line = h('span', { class: 'small dim num export-size', role: 'status', 'aria-live': 'polite' });
  let data = null;
  let pending = false;
  let error = false;
  let version = state.camdata?.dataVersion;
  let generation = 0;
  function paint() {
    if (version !== state.camdata?.dataVersion) {
      version = state.camdata?.dataVersion;
      data = null;
      pending = error = false;
      generation++;
    }
    if (getScope() !== 'ids' || ids?.length !== 1) {
      line.textContent = '每张照片按各自比例导出，成品尺寸可能不同。';
      return;
    }
    line.textContent = data ? sizeText(data.sizes[getSize()], data.ratio) : error ? '无法读取成品尺寸。请关闭后重试。' : '正在计算成品尺寸…';
    if (data || pending || error) return;
    pending = true;
    const mine = generation;
    flushPhotos(ids).then(() => api(`/api/photos/${ids[0]}/output-size`, { method: 'POST', body: {} })).then((value) => {
      if (mine !== generation) return;
      data = value;
      paint();
    }, () => { if (mine === generation) { error = true; paint(); } });
  }
  paint();
  return { line, paint };
}
