// 「未校准」小标签的提示气泡：说清楚这张照片具体为什么没有校准，不写机器值。
// 判断和后端一致（render/service.py 的 plan）：校准只有 A12 50mm 单元的 ISO 200 和 ISO 1600 两套；
// 其他单元借用 50mm 的颜色模型，其他 ISO 借用相近的一档，认不出的单元按 50mm 算；套了 LUT 的不经过相机的颜色校准。

// 与 camdata/calib.py 的 CALIBRATED_ISOS 相同（tests/test_feedback_copy.py 核对）
export const CALIBRATED_ISOS = [200, 1600];

const UNIT_NAMES = { qlaunch: '28mm', l08firm: 'MOUNT', jlaunch: '50mm' };
const TAIL = '颜色可能有些偏差。';

// unit：这张照片的镜头单元（preview.js 的 unitForPhoto；认不出是 null）
export function uncalibratedReason(view, unit) {
  const photo = view?.photo || {};
  const ed = view?.ed || {};
  if (ed.lut) return '套用了 LUT：颜色由 LUT 决定，不经过相机的颜色校准。';
  if (!unit) return `认不出这张照片用的镜头单元，按 A12 50mm 的颜色模型预测，${TAIL}`;
  if (unit !== 'jlaunch') return `这张照片是 ${UNIT_NAMES[unit] || unit} 拍的，颜色校准只做了 A12 50mm，借用 50mm 的颜色模型，${TAIL}`;
  const iso = Number(photo.iso);
  if (!CALIBRATED_ISOS.includes(iso)) {
    const known = CALIBRATED_ISOS.join(' 和 ISO ');
    return iso > 0
      ? `这张照片是 ISO ${iso}，颜色校准只做了 ISO ${known}，借用相近的一档，${TAIL}`
      : `读不到这张照片的 ISO，颜色校准只做了 ISO ${known}，借用相近的一档，${TAIL}`;
  }
  const picked = ed.recipe && ed.recipe.unit;
  if (picked && picked !== unit) return `选的配方来自 ${UNIT_NAMES[picked] || picked} 单元，不是这张照片的镜头，${TAIL}`;
  return `这张照片没有对应的颜色校准，${TAIL}`;
}
