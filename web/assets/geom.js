// 裁切框的几何（纯函数，没有 DOM，tests/test_web_frontend.py 用 node 直接测）。
// 坐标都是归一化的 {x, y, w, h}（0 到 1），指的是「未裁切的那张图」：旋转、翻转、拉直之后、裁切之前（PRD 第 10 节第 10 条）。
// canvas 是这张图的像素宽高 {w, h}。valid 是有效画面（拉直、透视之后不留空白角的范围），也是归一化的。

const EPS = 1e-9;
const MIN_SIDE = 0.02;

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

// 框在有效画面里吗（允许一点点误差）
export function insideValid(r, valid) {
  return r.x >= valid.x - EPS && r.y >= valid.y - EPS && r.x + r.w <= valid.x + valid.w + EPS && r.y + r.h <= valid.y + valid.h + EPS;
}

// 收进有效画面：先挪位置；挪不进去（框比有效画面还大）就以中心为准等比缩小。changed 表示动过。
export function fitInto(rect, valid) {
  const r = { ...rect };
  if (insideValid(r, valid)) return { rect: r, changed: false };
  const w = Math.min(r.w, valid.w);
  const h = Math.min(r.h, valid.h);
  // 等比缩小：宽高都按同一个比例收，保持框的比例
  const t = Math.min(1, valid.w / r.w, valid.h / r.h);
  const tw = r.w * t;
  const th = r.h * t;
  const cx = clamp(r.x + r.w / 2, valid.x + tw / 2, valid.x + valid.w - tw / 2);
  const cy = clamp(r.y + r.h / 2, valid.y + th / 2, valid.y + valid.h - th / 2);
  const out = { x: cx - tw / 2, y: cy - th / 2, w: Math.min(tw, w), h: Math.min(th, h) };
  return { rect: out, changed: true };
}

// 把框挪到 dx、dy（归一化）之后，但不让它跑出画布
export function moveRect(start, dx, dy) {
  return { ...start, x: clamp(start.x + dx, 0, 1 - start.w), y: clamp(start.y + dy, 0, 1 - start.h) };
}

// 拖角缩放：corner 是被拖的那个角（tl/tr/bl/br），对角固定不动。
// pointer 是指针在画布上的归一化位置。lock 是要锁的比例（宽 / 高，像素），null 表示自由。
export function resizeRect(start, corner, pointer, canvas, lock) {
  const ax = corner.includes('l') ? start.x + start.w : start.x;
  const ay = corner.includes('t') ? start.y + start.h : start.y;
  let w = Math.max(MIN_SIDE, Math.abs(pointer.x - ax));
  let h = Math.max(MIN_SIDE, Math.abs(pointer.y - ay));
  if (lock) h = Math.max(MIN_SIDE, (w * canvas.w) / lock / canvas.h);
  return {
    x: corner.includes('l') ? ax - w : ax,
    y: corner.includes('t') ? ay - h : ay,
    w,
    h,
  };
}

// 有效画面里、指定比例（宽 / 高，像素；null 表示用画布本身的比例）最大的、居中的框
export function maxRectFor(valid, canvas, ratio) {
  const rw = valid.w * canvas.w;
  const rh = valid.h * canvas.h;
  const a = ratio == null ? canvas.w / canvas.h : ratio;
  let W;
  let H;
  if (rw / rh > a) {
    H = rh;
    W = a * H;
  } else {
    W = rw;
    H = W / a;
  }
  const w = W / canvas.w;
  const h = H / canvas.h;
  return { x: valid.x + (valid.w - w) / 2, y: valid.y + (valid.h - h) / 2, w, h };
}

// 当前框占最大框的百分比（框的大小滑块）。以框的中心为准缩放，再收进有效画面。
export function zoomRect(current, maxRect, percent, valid) {
  const cx = current.x + current.w / 2;
  const cy = current.y + current.h / 2;
  const w = maxRect.w * (percent / 100);
  const h = maxRect.h * (percent / 100);
  return fitInto({ x: cx - w / 2, y: cy - h / 2, w, h }, valid).rect;
}

// 逆时针转 90°：(x, y, w, h) → (y, 1 - x - w, h, w)。框跟着画面转，不重算。
export function rotateRect90(r) {
  return { x: r.y, y: 1 - r.x - r.w, w: r.h, h: r.w };
}

// 左右镜像：(x, y, w, h) → (1 - x - w, y, w, h)
export function flipRect(r) {
  return { x: 1 - r.x - r.w, y: r.y, w: r.w, h: r.h };
}

// 旋转后画布的宽高（rot 为逆时针转了几个 90°）
export function canvasAfterRotation(canvas, rot) {
  return rot % 2 ? { w: canvas.h, h: canvas.w } : { w: canvas.w, h: canvas.h };
}

// 旧版后端（只认拉直、不认几何参数）时，前端自己估计有效画面：拉直 deg 度以后，
// 画布中心最大的、和画布同比例的框。和后端 crop.py 的 box_inside 是同一个几何关系，只是更保守。
export function legacyValid(deg, canvas) {
  const t = (Math.abs(deg) * Math.PI) / 180;
  const c = Math.cos(t);
  const s = Math.sin(t);
  const W = canvas.w;
  const H = canvas.h;
  const k = Math.min(1 / (c + (H / W) * s), 1 / ((W / H) * s + c));
  return { x: (1 - k) / 2, y: (1 - k) / 2, w: k, h: k };
}

// 接口里 rect 是 [x, y, w, h] 数组；界面里用 {x, y, w, h}
export function fromArray(a) {
  if (!Array.isArray(a) || a.length !== 4) return { x: 0, y: 0, w: 1, h: 1 };
  return { x: a[0], y: a[1], w: a[2], h: a[3] };
}

export function toArray(r) {
  return [r.x, r.y, r.w, r.h];
}

export function sameRect(a, b, eps = 1e-6) {
  return Math.abs(a.x - b.x) < eps && Math.abs(a.y - b.y) < eps && Math.abs(a.w - b.w) < eps && Math.abs(a.h - b.h) < eps;
}

// 框的像素宽高（给「裁出 4288 × 2848 像素」这一行用）
export function pixelSize(r, canvas) {
  return { w: Math.round(r.w * canvas.w), h: Math.round(r.h * canvas.h) };
}

// 字符串形式的拉直角度：+1.2°
export function fmtDeg(v) {
  const n = Math.round((Number(v) || 0) * 10) / 10;
  return `${n > 0 ? '+' : ''}${n.toFixed(1)}°`;
}
