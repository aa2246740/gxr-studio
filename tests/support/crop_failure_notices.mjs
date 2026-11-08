import { pathToFileURL } from 'node:url';
import { installDom } from './mini_dom.mjs';

installDom();
const [folder, choice] = process.argv.slice(2);
const { writeSave, saveRecord, saveStatus } = await import(pathToFileURL(`${folder}/saves.js`).href);
const root = document.getElementById('toast-root');
const calls = [];
const mine = { preset: '4:5', orientation: 'portrait', rect: [0.2, 0.15, 0.3, 0.6] };
const rec = saveRecord('a0'); rec.rev = 1;
let saved = { ...mine, rect: [0, 0, 1, 1], rev: 1, frame: { width: 3000, height: 2000 } };
let acceptPlain = false;
let acceptEdit = false;
globalThis.apiStub = async (url, opts = {}) => {
  calls.push({ url, ...(opts.method ? { body: structuredClone(opts.body) } : {}) });
  if (!opts.method) return structuredClone(saved);
  if (url.endsWith('/edit') && !acceptEdit) throw new Error('曝光写不进去');
  if (url.endsWith('/crop') && !opts.body.fit && !acceptPlain) {
    throw Object.assign(new Error('裁剪比例不符'), { code: 'ratio_mismatch' });
  }
  saved = { ...saved, ...opts.body, rev: saved.rev + 1 };
  return structuredClone(saved);
};
const errors = () => [...root.children].filter((t) => t.classList.contains('err') && !t.classList.contains('leaving'));
const labels = (toast) => [...toast.querySelectorAll('button')].map((b) => b.textContent);
const button = (toast, label) => [...toast.querySelectorAll('button')].find((b) => b.textContent === label);
const settle = async () => { await new Promise(setImmediate); await rec.tail; if (rec.retrying) await rec.retrying; await new Promise(setImmediate); };
const edit = { adjust: { exposure_ev: 1 } };
const kind = choice === 'stale-retry' ? 'edit' : 'crop';
await writeSave(rec, kind, kind === 'edit' ? edit : mine);
const first = errors()[0];
const old = button(first, kind === 'edit' ? '重试' : choice === 'stale-restore' ? '恢复已保存的框' : '收进画面');
await writeSave(rec, kind, kind === 'edit' ? { adjust: { exposure_ev: 2 } } : { ...mine, rect: [0.1, 0.1, 0.3, 0.6] });
const out = { beforeErrors: errors().length };
if (choice === 'restore-edit') await writeSave(rec, 'edit', edit);
if (choice === 'fit' || choice === 'restore' || choice === 'restore-edit') {
  button(errors().at(-1), choice === 'fit' ? '收进画面' : '恢复已保存的框').click();
  await settle();
}
if (choice === 'new-crop') {
  acceptPlain = true;
  await writeSave(rec, 'crop', { ...mine, rect: [0.1, 0.1, 0.2, 0.375] });
}
if (choice === 'restore-edit') {
  out.restored = { status: saveStatus('a0'), errors: errors().map(labels), edit: structuredClone(rec.failed?.edit) };
  acceptEdit = true;
  const start = calls.length;
  button(errors().at(-1), '重试')?.click();
  await settle();
  out.retryWrites = calls.slice(start).filter((c) => c.body);
}
out.resolvedErrors = errors().map(labels);
const start = calls.length;
old.click();
await settle();
out.staleCalls = calls.slice(start);
out.status = saveStatus('a0');
out.errors = errors().map(labels);
console.log(JSON.stringify(out));
process.exit(0);
