import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { installDom, tick } from './mini_dom.mjs';

installDom();
const file = (name) => pathToFileURL(`${process.argv[2]}/${name}`).href;
const { state } = await import(file('state.js'));
const { exportForm } = await import(file('export.js'));
const scenario = process.argv[3] || 'picker';
const original = '/tmp/原来的导出文件夹';
const chosen = `/tmp/${'这是一段很长的路径/'.repeat(9)}导出照片`;
state.settings = { export_dir: original };
state.camdata = { ready: true };
let pick = { cancelled: false, path: chosen };
let pickerError = null;
let exportError = null;
const calls = [];
let batchStarted;
const started = new Promise((resolve) => { batchStarted = resolve; });
let finishBatch;
const batch = new Promise((resolve) => { finishBatch = resolve; });
let batchPolled;
const polled = new Promise((resolve) => { batchPolled = resolve; });
globalThis.apiStub = async (url, opts = {}) => {
  calls.push({ url, ...opts });
  if (url === '/api/export/destination') {
    if (pickerError) throw pickerError;
    return pick;
  }
  if (url.endsWith('/output-size')) return { sizes: { original: [640, 360] }, ratio: [16, 9] };
  if (url === '/api/photos/p1/export') {
    if (exportError) throw exportError;
    return { path: `${opts.body.dest_dir}/photo.jpg`, width: 640, height: 360, bytes: 400 };
  }
  if (url === '/api/export') return { id: 'batch', running: true, progress: { total: 2, done: 0, results: [] } };
  if (url === '/api/jobs/batch') { batchPolled(); return batch; }
  throw new Error(`Unexpected API call: ${url}`);
};
const make = (ids = ['p1']) => exportForm({ ids, label: '选择的照片', photos: ids.map((id) => ({ id, has_jpg: true, has_raw: false })), onStarted: batchStarted });
const form = make(scenario.startsWith('batch-') ? ['p1', 'p2'] : ['p1']);
document.body.append(form);
await tick();
const input = form.querySelector('[aria-label="导出到"]');
const buttons = () => form.querySelectorAll('button');
const choose = buttons().find((b) => b.textContent === '选择文件夹…');
assert.ok(choose, '导出表单必须有「选择文件夹…」按钮');
if (scenario.startsWith('batch-')) {
  const target = '/tmp/这次批量导出';
  input.value = target;
  input.dispatchEvent(new Event('input'));
  buttons().find((b) => b.textContent === '导出').click();
  await started;
  assert.equal(make().querySelector('[aria-label="导出到"]').value, original, '任务刚排队时不能记住尚未验证的目录');
  const succeeded = scenario === 'batch-partial' || scenario === 'batch-cancelled-partial';
  const cancelled = scenario.startsWith('batch-cancelled-');
  const error = { id: 'p2', error: 'bad_destination', message: '无法导出到这个位置。请换一个文件夹。' };
  const results = succeeded ? [{ id: 'p1', path: `${target}/photo.jpg` }, ...(cancelled ? [] : [error])]
    : cancelled ? [] : [{ ...error, id: 'p1' }, error];
  finishBatch({ id: 'batch', running: false, progress: { state: cancelled ? 'cancelled' : 'done', total: 2, done: results.length, results } });
  await polled;
  await tick();
  assert.equal(make().querySelector('[aria-label="导出到"]').value, succeeded ? target : original, '只有实际写出照片的批量任务才能记住目录');
  assert.ok(form.textContent.includes(`已导出 ${succeeded ? 1 : 0} 张`));
  if (!cancelled) assert.ok(form.textContent.includes(error.message), '仍须显示逐张失败原因');
  if (cancelled) assert.ok(form.textContent.includes('已取消'));
  process.exit(0);
}
if (scenario === 'cancel-after-error') {
  pickerError = new Error('无法打开文件夹选择器。请粘贴文件夹的完整路径。');
  choose.click();
  await tick();
  const previousError = pickerError.message;
  assert.ok(form.textContent.includes(previousError));
  pickerError = null;
  let cancel;
  pick = new Promise((resolve) => { cancel = resolve; });
  choose.click();
  assert.ok(!form.textContent.includes(previousError), '再次打开选择器时清掉旧错误提示');
  cancel({ cancelled: true, path: null });
  await tick();
  assert.equal(input.value, original, '取消仍须保留原路径');
  assert.equal(input.readOnly, false);
  assert.ok(!form.textContent.includes(previousError), '取消后不能留下上次的失败提示');
  process.exit(0);
}
assert.equal(calls.filter((c) => c.url === '/api/export/destination').length, 0, '只能点击后打开选择器');
choose.click();
choose.click();
await tick();
assert.equal(calls.filter((c) => c.url === '/api/export/destination').length, 1, '防止重复弹出对话框');
assert.deepEqual(calls.find((c) => c.url === '/api/export/destination').body, { dest_dir: original });
assert.equal(input.value, chosen);
assert.equal(input.title || input.getAttribute('title'), chosen);
assert.ok(form.textContent.includes(chosen), '完整路径要在输入框外可读');
pick = { cancelled: true, path: null };
choose.click();
await tick();
assert.equal(input.value, chosen, '取消不改路径');
pickerError = new Error('无法打开文件夹选择器。请粘贴文件夹的完整路径。');
choose.click();
await tick();
assert.equal(input.value, chosen);
assert.ok(form.textContent.includes(pickerError.message));
assert.equal(input.readOnly, false, '失败后仍能编辑路径');
const pasted = '/tmp/手动粘贴';
input.value = pasted;
input.dispatchEvent(new Event('input'));
assert.ok(form.textContent.includes(pasted));
assert.ok(!form.textContent.includes(pickerError.message), '手动修改后清掉选择器的旧错误');
exportError = new Error('无法导出到这个位置。请换一个文件夹。');
buttons().find((b) => b.textContent === '导出').click();
await tick();
assert.equal(calls.findLast((c) => c.url.endsWith('/export')).body.dest_dir, pasted);
assert.ok(form.textContent.includes(exportError.message), '路径校验错误必须显示');
exportError = null;
buttons().find((b) => b.textContent === '导出').click();
await tick();
assert.equal(make().querySelector('[aria-label="导出到"]').value, pasted, '下次沿用最后成功导出的位置');
assert.ok(form.textContent.includes('已导出 1 张'));
process.exit(0);
