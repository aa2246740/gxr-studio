// 画面的小标签（纯函数）：电脑预测 / 中性转换 / 内嵌预览 / 原图（COPY.md 1.8、3.3）。
// look 来自 /api/photos 的 preview_look：camera-jpg | stock04-prediction | neutral | embedded。

export const CAMERA_JPG_NOTE = '相机 JPG';

export function isPrediction(look) {
  return !!look && look.kind === 'stock04-prediction';
}

// 画面的小标签文字
export function lookNoteText(look) {
  if (isPrediction(look)) return '电脑预测';
  if (look && look.kind === 'neutral') return '中性转换';
  if (look && look.kind === 'embedded') return '内嵌预览';
  return CAMERA_JPG_NOTE;
}

// 按住空格看原图时的文字
export function originalNoteText() {
  return '原图';
}

// 图库角标：只有 RAW 的照片
export function rawOnlyBadgeText(look) {
  if (isPrediction(look)) return '电脑预测';
  return 'RAW';
}
