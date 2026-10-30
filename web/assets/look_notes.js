// 没有编辑时缩略图/大图画的是什么，怎么跟用户说（纯函数，tests/test_import_summary.py 用 node 直接测）。
// look 来自 /api/photos 的 preview_look：camera-jpg | stock04-prediction | neutral | embedded。
// 铁律：只有相机 JPG 才能说"照片本身"；只有 RAW 的照片画面是电脑画的，必须说清楚是预测还是中性转换。

export const CAMERA_JPG_NOTE = '这里显示的是照片本身（不是预测）。按住 Space 看原图，松开恢复裁切。';

export function isPrediction(look) {
  return !!look && look.kind === 'stock04-prediction';
}

// 大图下面的说明（文字部分；电脑预测时界面再加一个"电脑预测"小标签）
export function lookNoteText(look) {
  if (isPrediction(look)) {
    return `${look.label}：这张照片只有 RAW，下面是按相机原厂标准算出来的模拟，不是相机拍出的 JPG。按住 Space 看没有裁切的整张，松开恢复。`;
  }
  if (look && look.kind === 'neutral') {
    return '这张照片只有 RAW，下面是中性转换（只做了白平衡，不是相机的颜色，也不是配方预测）。导入相机数据后会换成电脑预测。按住 Space 看没有裁切的整张。';
  }
  if (look && look.kind === 'embedded') {
    return '这张照片只有 RAW，而且这份 RAW 暂时读不出来，下面是 RAW 里内嵌的小预览（相机自己生成的，很小，不是配方预测）。';
  }
  return CAMERA_JPG_NOTE;
}

// 按住 Space 时的说明
export function originalNoteText(look) {
  if (isPrediction(look)) return '正在看没有任何编辑的画面：RAW 按原厂标准算出来的电脑预测，不是相机 JPG。松开 Space 恢复预览。';
  if (look && look.kind === 'neutral') return '正在看没有任何编辑的画面：RAW 的中性转换，不是相机的颜色。松开 Space 恢复预览。';
  if (look && look.kind === 'embedded') return '正在看 RAW 里内嵌的小预览（相机自己生成的，很小）。松开 Space 恢复预览。';
  return '正在看照片本身（相机 JPG）。松开 Space 恢复预览。';
}

// 图库角标：只有 RAW 的照片
export function rawOnlyBadgeText(look) {
  if (isPrediction(look)) return '只有 DNG · 电脑预测';
  if (look && look.kind === 'neutral') return '只有 DNG · 中性';
  if (look && look.kind === 'embedded') return '只有 DNG · 内嵌小图';
  return '只有 DNG';
}
