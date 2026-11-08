import { pathToFileURL } from 'node:url';
import { installDom } from './mini_dom.mjs';

installDom();
const [folder, choice, code] = process.argv.slice(2);
const file = (name) => pathToFileURL(`${folder}/${name}`).href;
const { writeSave, saveRecord, saveStatus } = await import(file('saves.js'));
const { on } = await import(file('state.js'));
const root = document.getElementById('toast-root');
const writes = [];
const reads = [];
const adopted = [];
let saved = { rev: 1, preset: '4:5', orientation: 'portrait', rect: [0.1, 0.1, 0.4, 0.75], frame: { width: 3000, height: 2000 } };
let acceptPlain = false;
let failRead = choice === 'restore-read-failure';
let failFit = choice === 'fit-failure';
let holdWrite = null;
const mine = { preset: '4:5', orientation: 'portrait', rect: [0.2, 0.15, 0.3, 0.6] };
const rec = saveRecord('a0'); rec.rev = 1;
const other = saveRecord('b0'); other.rev = 7;
on('save-retried', (e) => adopted.push(e.detail));
globalThis.apiStub = async (url, opts = {}) => {
  if (!opts.method) {
    reads.push(url);
    if (url.endsWith('/crop') && failRead) { failRead = false; throw new Error('暂时读不到'); }
    return structuredClone(saved);
  }
  writes.push({ url, body: structuredClone(opts.body) });
  if (url.endsWith('/edit')) throw new Error('曝光写不进去');
  if (holdWrite) await holdWrite;
  if (opts.body.fit && failFit) { failFit = false; throw new Error('暂时写不进去'); }
  if (!opts.body.fit && !acceptPlain) { const e = new Error('裁剪框超出画面范围'); e.code = code; throw e; }
  saved = { ...saved, ...opts.body, rect: opts.body.fit ? [0.2, 0.15, 0.3, 0.5625] : opts.body.rect, rev: saved.rev + 1 };
  return structuredClone(saved);
};
await writeSave(rec, 'crop', mine);
const out = { before: { status: saveStatus('a0'), writes: writes.length, saved: saved.rect }, labels: [...root.lastChild.querySelectorAll('button')].map((b) => b.textContent) };
const label = choice.startsWith('fit') || choice.startsWith('stale') ? '收进画面' : '恢复已保存的框';
const button = [...root.lastChild.querySelectorAll('button')].find((b) => b.textContent === label);
const settle = async () => { await new Promise(setImmediate); await rec.tail; if (rec.retrying) await rec.retrying; await new Promise(setImmediate); };
if (button) {
  let release = null;
  let queued = null;
  if (choice === 'restore-edit-failure') await writeSave(rec, 'edit', { adjust: { exposure_ev: 1 } });
  if (choice === 'stale') { acceptPlain = true; await writeSave(rec, 'crop', { ...mine, rect: [0.1, 0.1, 0.2, 0.375] }); }
  if (choice === 'stale-pending') {
    acceptPlain = true;
    holdWrite = new Promise((resolve) => { release = resolve; });
    queued = writeSave(rec, 'crop', { ...mine, rect: [0.1, 0.1, 0.2, 0.375] });
    await new Promise(setImmediate);
  }
  const before = writes.length;
  button.click();
  if (release) { release(); await queued; }
  await settle();
  if (choice === 'restore-read-failure') {
    out.failedRead = { status: saveStatus('a0'), crop: rec.failed?.crop, adopted: adopted.length };
    const again = [...root.lastChild.querySelectorAll('button')].find((b) => b.textContent === label);
    again?.click(); await settle();
  }
  if (choice === 'fit-failure') {
    out.failedFit = { status: saveStatus('a0'), shape: rec.failed?.cropShape, writes: writes.length - before };
    const again = [...root.lastChild.querySelectorAll('button')].find((b) => b.textContent === label);
    again?.click(); await settle();
  }
  out.after = { status: saveStatus('a0'), writes: writes.slice(before), reads, saved: saved.rect, failedEdit: rec.failed?.edit || null, adopted, otherRev: other.rev };
}
console.log(JSON.stringify(out));
process.exit(0);
