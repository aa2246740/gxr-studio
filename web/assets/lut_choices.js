// LUT 页的颜色、镜头组件和名称；颜色计算仍由现有接口负责。
import { h } from './dom.js';

export const UNIT_NAMES = { jlaunch: 'A12 50mm', qlaunch: 'A12 28mm', l08firm: 'MOUNT' };
export const unitName = (unit) => Object.hasOwn(UNIT_NAMES, unit) ? UNIT_NAMES[unit] : '未知镜头组件';
export const fitSupported = (flow) => !!flow.units?.find((unit) => unit.id === flow.unit)?.fit_supported;

export function mostUsedUnit(photos) {
  const counts = Object.fromEntries(Object.keys(UNIT_NAMES).map((unit) => [unit, 0]));
  for (const photo of photos) if (Object.hasOwn(counts, photo.unit)) counts[photo.unit]++;
  return Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0];
}

export function suggestedName(cube, recipes) {
  const raw = cube.title || cube.filename?.split(/[\\/]/).pop() || cube.name || 'LUT 配方';
  const base = raw.replace(/\.cube$/i, '').replace(/[\x00-\x1f\x7f\\/_]+/g, ' ').replace(/\s+/g, ' ').trim() || 'LUT 配方';
  const used = new Set(recipes.map((r) => r.name));
  for (let n = 1; ; n++) {
    const suffix = n === 1 ? '' : ` ${n}`;
    const name = Array.from(base).slice(0, 40 - suffix.length).join('') + suffix;
    if (!used.has(name)) return name;
  }
}

export function spaceFields(flow, onChange) {
  const sp = flow.spaces || [];
  const disabled = flow.spaceSaving || !!flow.busy || flow.job?.state === 'running';
  const opt = (s, value) => h('option', { value: s.id, selected: s.id === value }, s.label);
  const select = h('select', {
    'aria-label': '输入色彩空间', disabled,
    onchange: (e) => { flow.space = e.target.value; onChange(); },
  },
  !flow.space ? h('option', { value: '', selected: true, disabled: true }, '请按 LUT 说明选择') : null,
  h('optgroup', { label: '普通照片与视频显示' }, sp.filter((s) => s.kind === 'display').map((s) => opt(s, flow.space))),
  h('optgroup', { label: '相机 Log 与高级空间' }, sp.filter((s) => s.kind === 'log').map((s) => opt(s, flow.space))));
  const source = flow.cube.default_input_source;
  const tag = { declared: '文件已写明', guessed: '推测', fallback: '默认', manual: '已手选' }[source];
  const adv = h('details', { class: 'small' },
    h('summary', {}, '输出色彩空间'),
    h('div', { class: 'field-row' },
      h('select', { 'aria-label': '输出色彩空间', disabled, onchange: (e) => { flow.output = e.target.value; onChange(); } },
        sp.filter((s) => s.kind === 'display').map((s) => opt(s, flow.output))),
      h('select', { 'aria-label': '输出色域', disabled, onchange: (e) => { flow.outputGamut = e.target.value; onChange(); } },
        [['', '默认'], ['rec709', 'Rec.709'], ['rec2020', 'Rec.2020']].map(([v, t]) => h('option', { value: v, selected: v === flow.outputGamut }, t)))));
  return h('div', { class: 'field lut-space', dataset: { role: 'lut-space' } },
    h('label', {}, h('span', {}, '输入色彩空间'), select),
    h('div', { class: 'small dim' }, tag ? h('span', { class: 'tag' }, tag) : null, ' ', flow.cube.default_input_note), adv);
}

export function unitFields(flow, onChange) {
  const photo = flow.photos?.find((p) => p.id === flow.photoId);
  const supported = fitSupported(flow);
  return h('div', { class: 'field lut-unit', dataset: { role: 'lut-unit' } },
    h('label', {}, h('span', {}, '为哪个镜头组件制作'),
      h('select', { 'aria-label': '配方适用的镜头组件', disabled: !!flow.busy || flow.job?.state === 'running',
        onchange: (e) => onChange(e.target.value) },
      Object.entries(UNIT_NAMES).map(([id, label]) => h('option', { value: id, selected: id === flow.unit }, label)))),
    h('p', { class: `small ${supported ? 'dim' : 'warn-text'}`, role: 'status' }, supported
      ? '现在只能为 A12 50mm 生成。生成的 LUT 配方只用于 50mm 拍的照片。'
      : `${unitName(flow.unit)} 还没有单独的校准数据，暂时不能生成。现在只能为 A12 50mm 生成配方。`),
    photo && photo.unit !== flow.unit ? h('p', { class: 'small warn-text' },
      `预览照片来自${unitName(photo.unit)}。配方只会出现在${unitName(flow.unit)}拍的照片上。`) : null);
}
