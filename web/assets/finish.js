// 边框、时间戳、几何的默认值和优先级（纯函数，tests/test_web_frontend.py 用 node 直接测）。
// 规则（PRD 第 10 节第 5 条）：照片自己有 frame / stamp 键就用它；没有就用默认（后端渲染和导出时套用，前端不往 sidecar 里复制）。

export const DEFAULT_FRAME = {
  style: 'none',
  color: 'white',
  weight: 'medium',
  show_logo: true,
  show_params: true,
  show_date: false,
  show_frame_no: true,
  text: '',
  canvas_ratio: '4:5',
};

export const DEFAULT_STAMP = {
  enabled: false,
  content: 'date',
  date_format: "'yy mm dd",
  show_time: false,
  date_override: null,
  text: '',
  font: 'seg7',
  color: 'orange',
  size: 'medium',
  corner: 'br',
  glow: true,
};

export const DEFAULT_GEOMETRY = { straighten: 0, vertical: 0, horizontal: 0, distortion: 0, rotate90: 0, flip: false };

export const TEXT_MAX = 24;

// 不加边框：后端写 "none"，旧数据里的 null 也当成不加
export function normalizeFrame(f) {
  const out = { ...DEFAULT_FRAME, ...(f || {}) };
  if (!out.style) out.style = 'none';
  out.text = clipText(out.text, TEXT_MAX);
  return out;
}

export function normalizeStamp(s) {
  return { ...DEFAULT_STAMP, ...(s || {}) };
}

// 返回 { frame, stamp, frameFrom, stampFrom }。frameFrom 是 'photo'（照片自己的）或 'default'（默认的）。
export function resolveFinish(edit, defaults) {
  const hasOwnFrame = !!(edit && edit.frame);
  const hasOwnStamp = !!(edit && edit.stamp);
  const frameBase = hasOwnFrame ? edit.frame : defaults && defaults.frame;
  const stampBase = hasOwnStamp ? edit.stamp : defaults && defaults.stamp;
  return {
    frame: normalizeFrame(frameBase),
    stamp: normalizeStamp(stampBase),
    frameFrom: hasOwnFrame ? 'photo' : 'default',
    stampFrom: hasOwnStamp ? 'photo' : 'default',
  };
}

// 设为默认时存的结构：{ frame, stamp }
export function defaultsPayload(frame, stamp) {
  return { frame: normalizeFrame(frame), stamp: normalizeStamp(stamp) };
}

// 字数上限（按字符数，中文一个字算一个）
export function clipText(s, max = TEXT_MAX) {
  const t = String(s == null ? '' : s);
  return Array.from(t).slice(0, max).join('');
}

// 只接受 YYYY-MM-DD，而且真的是一个日期（2026-02-30 不行）
export function isDateString(s) {
  if (typeof s !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const [y, m, d] = s.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

// 时间戳的日期写法。iso 是 'YYYY-MM-DD'；format 是 DATE_FORMATS 里的 id（见 stamp_panel.js）
export function stampDateText(format, iso, time, showTime) {
  const [Y, M, D] = iso.split('-');
  const yy = Y.slice(2);
  let s;
  switch (format) {
    case 'yy mm dd':
      s = `${yy} ${M} ${D}`;
      break;
    case 'yyyy.mm.dd':
      s = `${Y}.${M}.${D}`;
      break;
    case "mm dd 'yy":
      s = `${M} ${D} '${yy}`;
      break;
    default:
      s = `'${yy} ${M} ${D}`;
  }
  if (showTime && time) s += ` ${time}`;
  return s;
}

// 时间戳的整行文字（预览用，正式的画面由后端画）
export function stampLine(stamp, photo) {
  const parts = [];
  const iso = stamp.date_override || (photo && photo.date) || null;
  if (stamp.content !== 'text' && iso) parts.push(stampDateText(stamp.date_format, iso, photo.time, stamp.show_time));
  if (stamp.content !== 'date' && stamp.text) parts.push(clipText(stamp.text));
  return parts.join('  ');
}

// 发给后端的边框键（PRD 7.1）。帧号由胶片样式自带，不单独发。
export function frameBody(frame) {
  const body = {
    style: frame.style,
    color: frame.color,
    weight: frame.weight,
    show_logo: frame.show_logo,
    show_params: frame.show_params,
    show_date: frame.show_date,
    text: clipText(frame.text),
    canvas_ratio: frame.canvas_ratio,
  };
  return body;
}
