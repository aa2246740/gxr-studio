// 访问本机接口的唯一入口。错误统一成 ApiError（code 是后端的错误代码，message 已经是大白话；
// data 是后端返回的完整内容，例如 409 时的 status，界面用它说明"为什么不能预览"）。

export class ApiError extends Error {
  constructor(status, code, message, data = null) {
    super(message);
    this.status = status;
    this.code = code;
    this.data = data;
  }
}

async function send(path, { method, body, signal, headers }) {
  const opts = { method, credentials: 'same-origin', headers: { ...headers }, signal };
  if (body instanceof FormData) {
    opts.body = body; // 浏览器自己带上 multipart 的边界
  } else if (body instanceof Blob) {
    opts.body = body; // 文件的原始字节（选中的 GXRCE.DAT）
    opts.headers['Content-Type'] = 'application/octet-stream';
  } else if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  try {
    return await fetch(path, opts);
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ApiError(0, 'offline', '无法连接到 GXR Studio。请确认启动窗口仍在运行，然后刷新页面。');
  }
}

async function readJson(res) {
  const text = await res.text();
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
}

// 后端没给说明时只有一句通用的话；generic 标记让界面换成这个操作自己的说法（dom.js 的 errorBody）
function errorFrom(res, data) {
  const err = new ApiError(res.status, data?.error || `http_${res.status}`, data?.message || '无法完成操作。请重试。', data);
  err.generic = !data?.message;
  return err;
}

export async function api(path, { method = 'GET', body, headers } = {}) {
  const res = await send(path, { method, body, headers });
  const data = await readJson(res);
  if (!res.ok) throw errorFrom(res, data);
  return data;
}

// 返回二进制（电脑预测的预览图）和响应头（X-GXR-Label 等）。signal 用来取消过时的预览请求。
export async function apiBlob(path, { method = 'POST', body, signal } = {}) {
  const res = await send(path, { method, body, signal });
  if (!res.ok) throw errorFrom(res, await readJson(res));
  return { blob: await res.blob(), headers: res.headers };
}
