// 全尺寸照片的裁剪框：舞台、样式卡和尺寸行共用。同一底图、几何、裁剪只问一次。
import { api } from './api.js';
import { state } from './state.js';

export function photoSize(view) {
  const ed = view.ed;
  const c = view.crop;
  const crop = c ? { preset: c.preset, rect: [c.rect.x, c.rect.y, c.rect.w, c.rect.h] } : null;
  // 旋钮不改变像素尺寸；拖配方滑块时不重新查询。
  const recipe = ed.recipe ? { ...ed.recipe, levels: {} } : null;
  if (recipe) delete recipe.memory;
  const body = { recipe, lut: ed.lut, geometry: ed.geometry, crop };
  const key = JSON.stringify([body, state.camdata?.dataVersion]);
  if (view.outputSize?.key === key) return view.outputSize;
  const entry = { key, value: null, promise: null, stageListening: false };
  entry.promise = api(`/api/photos/${view.photo.id}/output-size`, { method: 'POST', body }).then((data) => {
    entry.value = data;
    return data;
  });
  view.outputSize = entry;
  return entry;
}
