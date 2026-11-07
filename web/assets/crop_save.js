// 写框，被拒了怎么办（转 90° / 翻转、撤销 / 重做几何这一步用）。
// - 被拒的原因是框的形状（界面的框落后于服务器的画布，或者撤销 / 重做拿到的框是重读完成之前的）：
//   让服务器按比例重新算一个居中的最大框（它还会收进有效画面），不留「有修改没有保存」。
// - 别的原因（断网、500、版本冲突）：照常提示，重试用的还是这个框，不能悄悄换成居中的最大框。
// - 重算也失败：同样提示，重试用的也是这个框。
// 重算之前确认这一笔还是最新的：这期间用户又排进了新的框（点比例等），那一步更新，不能被覆盖。

import { saveCrop, retrySaves } from './preview.js';
import { saveRecord, SHAPE_REJECTED } from './saves.js';
import { toastError } from './dom.js';

export function saveCropOrRefit(view, body) {
  const rec = saveRecord(view.photo.id);
  const done = attempt(view, rec, body).finally(() => {
    if (rec.settling === done) rec.settling = null;
  });
  rec.settling = done; // 导出前的 flush 要等到这里，不能把第一次被拒当成最终失败
  return done;
}

async function attempt(view, rec, body) {
  const retry = { action: { label: '重试', run: () => retrySaves(view.photo.id) } };
  view.lastCropFailure = null;
  const first = saveCrop(view, body, { quiet: true });
  const mine = rec.fieldGen.crop; // 排队时已经加了一：之后再变，就是用户又排进了新的框
  if (await first) return true;
  const failure = view.lastCropFailure;
  if (!SHAPE_REJECTED.has(failure && failure.code)) {
    toastError(failure, '无法保存修改。', retry);
    return false;
  }
  if (rec.fieldGen.crop !== mine) return false;
  if (await saveCrop(view, { ...body, rect: null }, { quiet: true })) return true;
  if (rec.failed) rec.failed.crop = body;
  toastError(view.lastCropFailure, '无法保存修改。', retry);
  return false;
}
