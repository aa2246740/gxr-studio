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
    // gen：每排进一笔写入加一。读编辑记录的一方据此判断读到的是不是已经过时（F04）
    // kindGen：编辑、裁剪各自排进过几笔。重试写完时据此判断界面上有没有更新的修改（N02）
    r = { id, date: null, rev: null, tail: Promise.resolve(), pending: 0, gen: 0, kindGen: { edit: 0, crop: 0 }, failed: null, savedAt: 0, retrying: null };
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
  rec.gen += 1;
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
  rec.kindGen[kind] += 1;
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

// 「这一天的全部」导出前调用（F02）：这一天里所有这次改过的照片都要写完。
// 不知道是哪天拍的记录也算进来（宁可多等一张，也不拿旧编辑出图）。
export async function flushDate(date) {
  const ids = [...records.values()].filter((r) => !r.date || !date || r.date === date).map((r) => r.id);
  await flushPhotos(ids);
}

// 读这张照片的编辑记录（或裁剪），读到的必须是最新的：先等排着的写入，读的过程中又有写入就重读（F04）。
// read() 返回读到的内容；一直被新的写入打断（连续 5 次）就放弃并抛出，调用方保持现状。
export async function readFresh(id, read) {
  const rec = saveRecord(id);
  for (let i = 0; i < 5; i += 1) {
    await rec.tail;
    const gen = rec.gen;
    const out = await read();
    if (rec.gen === gen && !rec.pending) return out;
  }
  throw new Error('这张照片正在保存，稍后再读。');
}

// 重试没写进去的修改（F01）：整个重试是保存队列里的一笔——先读服务端现在的 rev（失败可能是 409），
// 再把还没写进去的内容写回去。导出前的 flush 会等到这一笔做完。
// 失败记录一直保留到重试写成功；重试排队期间又写成功的新修改，会把同名的旧内容从失败记录里去掉，
// 所以重试只补写还没被新修改盖掉的部分，不会拿旧值覆盖新值。
// 成功后发 save-retried 事件，带上写入接口的回应（就是服务器上现在的编辑记录 / 裁剪），
// 正在显示这张照片的视图直接用它更新，不再另外去读（F03；N02：另读一次可能失败，也可能盖掉更新的草稿）。
// 重试排队以后界面又排进了同一类的新写入，那一类就不交给视图：新写入来自界面上现在的样子，以它为准。
export function retrySaves(id) {
  const rec = records.get(id);
  if (!rec || !rec.failed) return Promise.resolve(true);
  if (rec.retrying) return rec.retrying;
  const since = { ...rec.kindGen };
  const written = { edit: null, crop: null };
  const put = async (kind, body) => {
    const r = await api(URLS[kind](id), { method: 'PUT', body: { ...body, expected_rev: rec.rev } });
    rec.rev = r.rev;
    clearSucceeded(rec, kind, body);
    written[kind] = r;
  };
  const job = async () => {
    if (!rec.failed) return;
    try {
      rec.rev = (await api(URLS.edit(id))).rev;
      // 读的是执行时的失败记录（排队期间可能已经被新修改去掉了一部分）
      if (rec.failed && rec.failed.edit) await put('edit', { ...rec.failed.edit });
      if (rec.failed && rec.failed.crop) await put('crop', { ...rec.failed.crop });
    } catch (e) {
      if (rec.failed) rec.failed.error = e;
      throw e;
    }
  };
  rec.retrying = enqueue(rec, job).then((ok) => {
    rec.retrying = null;
    if (ok && !rec.failed) {
      toast('修改已保存', 'info');
      const fresh = (kind) => (rec.kindGen[kind] === since[kind] ? written[kind] : null);
      emit('save-retried', { id, edit: fresh('edit'), crop: fresh('crop'), rev: rec.rev });
    } else {
      toastError(rec.failed && rec.failed.error, '无法保存修改。', { action: { label: '重试', run: () => retrySaves(id) } });
    }
    announce(rec);
    return ok && !rec.failed;
  });
  return rec.retrying;
}

// 放弃没写进去的修改（调用方随后按服务端的内容重读）
export function clearFailed(id) {
  const rec = records.get(id);
  if (!rec || !rec.failed) return;
  rec.failed = null;
  announce(rec);
}
