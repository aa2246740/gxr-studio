// 保存队列：每张照片一份，按照片 id 存在模块里，不挂在可销毁的 ed 上（R04）。
// 切图时 disposeEdit() 只停预览，已经排队的写入照常写完，成功或失败都有明确结果。
// 每一笔写入在入队时就绑定照片 id 和要改的内容；expected_rev 取这份记录上的 rev（前一笔写完后的版本），
// 编辑（PUT /edit）和裁剪（PUT /crop）共用一个 rev。
// 写入失败不吞掉：记在 failed 里，flushPhotos() 据此拒绝，导出随之停止（R05）；界面上给「重试」和「放弃修改」。

import { api } from './api.js';
import { emit } from './state.js';
import { toast, toastError } from './dom.js';

const records = new Map();
const URLS = { edit: (id) => `/api/photos/${id}/edit`, crop: (id) => `/api/photos/${id}/crop` };

export function saveRecord(id) {
  let r = records.get(id);
  if (!r) {
    r = { id, rev: null, tail: Promise.resolve(), pending: 0, failed: null, savedAt: 0 };
    records.set(id, r);
  }
  return r;
}

// 保存状态：idle（这次没改过）、saving、saved、failed。界面订阅 save-status 事件
export function saveStatus(id) {
  const r = records.get(id);
  if (!r) return 'idle';
  if (r.pending) return 'saving';
  if (r.failed) return 'failed';
  return r.savedAt ? 'saved' : 'idle';
}

function announce(r) {
  emit('save-status', { id: r.id, status: saveStatus(r.id) });
}

// 写入排队：同一张照片一次只写一个，写完再写下一个。
// 返回的 Promise 兑现为 true / false（这一笔有没有写进去），不会抛出；失败记在 record.failed。
// 队列本身在失败后照常接受下一笔（下一笔不会因为前一笔失败而被跳过）。
export function enqueue(rec, job) {
  rec.pending += 1;
  announce(rec);
  const run = rec.tail.then(async () => {
    try {
      await job();
      return true;
    } catch {
      return false;
    }
  });
  rec.tail = run.then(() => undefined);
  return run.then((ok) => {
    rec.pending -= 1;
    if (ok) rec.savedAt = Date.now();
    announce(rec);
    return ok;
  });
}

function recordFailure(rec, kind, body, error) {
  const f = rec.failed || { edit: null, crop: null, error: null };
  if (kind === 'edit') f.edit = { ...(f.edit || {}), ...body };
  else f.crop = body;
  f.error = error;
  rec.failed = f;
}

// 后面写成功的键不用再重试（重试不能拿旧值盖掉新值）
function clearSucceeded(rec, kind, body) {
  const f = rec.failed;
  if (!f) return;
  if (kind === 'edit' && f.edit) {
    for (const k of Object.keys(body)) delete f.edit[k];
    if (!Object.keys(f.edit).length) f.edit = null;
  }
  if (kind === 'crop') f.crop = null;
  if (!f.edit && !f.crop) rec.failed = null;
}

// 写一笔：kind 是 edit 或 crop；body 在这里复制一份（之后界面再改也不影响这一笔）。
// onOk(响应) / onFail(错误) 由调用方决定要不要更新界面（视图可能已经关了）。
export function writeSave(rec, kind, changes, { onOk = null, onFail = null } = {}) {
  const body = { ...changes };
  return enqueue(rec, async () => {
    try {
      const r = await api(URLS[kind](rec.id), { method: 'PUT', body: { ...body, expected_rev: rec.rev } });
      rec.rev = r.rev;
      clearSucceeded(rec, kind, body);
      onOk?.(r);
    } catch (e) {
      recordFailure(rec, kind, body, e);
      toastError(e, '无法保存修改。', { action: { label: '重试', run: () => retrySaves(rec.id) } });
      onFail?.(e);
      throw e;
    }
  });
}

export class SaveError extends Error {
  constructor(rec) {
    const reason = rec.failed && rec.failed.error && rec.failed.error.message;
    super(reason ? `有修改没有保存：${reason}` : '有修改没有保存。');
    this.code = 'unsaved';
    this.photoId = rec.id;
    this.data = (rec.failed && rec.failed.error && rec.failed.error.data) || null;
    this.retrySave = () => retrySaves(rec.id);
  }
}

// 导出前调用：等这些照片排着的写入都完成；任何一笔没写进去就拒绝（导出的必须是最新的修改）
export async function flushPhotos(ids) {
  for (const id of ids) {
    const rec = records.get(id);
    if (!rec) continue;
    await rec.tail;
    if (rec.failed) throw new SaveError(rec);
  }
}

// 重试没写进去的修改：先读服务端现在的 rev（失败可能是 409），再把记下的内容写回去
export async function retrySaves(id) {
  const rec = records.get(id);
  if (!rec || !rec.failed) return true;
  const { edit, crop } = rec.failed;
  rec.failed = null;
  const fresh = await enqueue(rec, async () => {
    try {
      rec.rev = (await api(URLS.edit(id))).rev;
    } catch (e) {
      if (edit) recordFailure(rec, 'edit', edit, e);
      if (crop) recordFailure(rec, 'crop', crop, e);
      toastError(e, '无法保存修改。');
      throw e;
    }
  });
  if (!fresh) return false;
  const all = await Promise.all([edit && writeSave(rec, 'edit', edit), crop && writeSave(rec, 'crop', crop)].filter(Boolean));
  const done = all.every(Boolean);
  if (done) toast('修改已保存', 'info');
  return done;
}

// 放弃没写进去的修改（调用方随后按服务端的内容重读）
export function clearFailed(id) {
  const rec = records.get(id);
  if (!rec || !rec.failed) return;
  rec.failed = null;
  announce(rec);
}
