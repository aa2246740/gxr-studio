// 「未校准」小标签的提示气泡：说清楚这张照片具体为什么没有校准，不写机器值。
// 仅实拍校准的 ISO 显示为已校准；其它 ISO 借用相近分支。
export const CALIBRATED_ISOS = [200, 1600];
const UNIT_ISOS = { jlaunch: CALIBRATED_ISOS, qlaunch: [200], l08firm: [200] };

const UNIT_NAMES = { qlaunch: '28mm', l08firm: 'MOUNT', jlaunch: '50mm' };
const TAIL = '颜色可能有些偏差。';

// unit：这张照片的镜头单元（preview.js 的 unitForPhoto；认不出是 null）
export function uncalibratedReason(view, unit) {
  const photo = view?.photo || {};
  const ed = view?.ed || {};
  if (ed.lut) return '套用了 LUT：颜色由 LUT 决定，不经过相机的颜色校准。';
  if (!unit) return `认不出这张照片用的镜头单元，按 A12 50mm 的颜色模型预测，${TAIL}`;
  const iso = Number(photo.iso);
  const calibrated = UNIT_ISOS[unit] || [];
  if (!calibrated.includes(iso)) {
    const known = calibrated.join(' 和 ISO ');
    return iso > 0
      ? `这张照片是 ISO ${iso}，颜色校准只做了 ISO ${known}，借用相近的一档，${TAIL}`
      : `读不到这张照片的 ISO，颜色校准只做了 ISO ${known}，借用相近的一档，${TAIL}`;
  }
  const picked = ed.recipe && ed.recipe.unit;
  if (picked && picked !== unit) return `选的配方来自 ${UNIT_NAMES[picked] || picked} 单元，不是这张照片的镜头，${TAIL}`;
  return `这张照片没有对应的颜色校准，${TAIL}`;
}
