// desktop-import 单元测试（2026-11-26）：拖进文件夹的展开（假的条目树：嵌套、点开头、层数和文件数上限、分批 readEntries），
// 没有用上的文件怎么数、怎么说，只选 GXRFX.BIN 的提示。CDP 拖不了文件夹，所以文件夹只在这里测。

import assert from 'node:assert/strict';
import test from 'node:test';
import { registerHooks } from 'node:module';

registerHooks({
  resolve(spec, ctx, next) {
    if (spec.endsWith('.css')) return { shortCircuit: true, url: 'data:text/javascript,export default ""' };
    try {
      return next(spec, ctx);
    } catch (err) {
      if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(`${spec}.ts`, ctx);
      throw err;
    }
  },
});

/** 假文件条目：file() 异步回一个 File。 */
function fileEntry(name) {
  return { name, isFile: true, isDirectory: false, file: (ok) => queueMicrotask(() => ok(new File([name], name))) };
}

/** 假文件夹：readEntries 每次最多给 `batch` 个，给完以后回空数组（和浏览器一样要一直读到空）。 */
function dirEntry(name, children, batch = 2) {
  return {
    name,
    isFile: false,
    isDirectory: true,
    createReader() {
      let at = 0;
      return {
        readEntries(ok) {
          const part = children.slice(at, at + batch);
          at += part.length;
          setTimeout(() => ok(part), 0);
        },
      };
    },
  };
}

const names = (walked) => walked.files.map((file) => file.name);

test('walker: nested card folder, batched readEntries, dot names skipped', async () => {
  const { walkEntries } = await import('../../mobile/src/app/file_drop.ts');
  const card = dirEntry('CARD', [
    fileEntry('GXRCE.DAT'),
    fileEntry('GXRFX.BIN'),
    dirEntry('.Trashes', [fileEntry('R0009999.JPG')]),
    fileEntry('.DS_Store'),
    dirEntry('DCIM', [
      dirEntry('100RICOH', [fileEntry('R0000001.DNG'), fileEntry('._R0000001.DNG'), fileEntry('R0000001.JPG'), fileEntry('R0000002.JPG'), fileEntry('R0000003.JPG')]),
      dirEntry('101RICOH', [fileEntry('R0010001.JPG')]),
    ]),
  ]);
  const walked = await walkEntries([card, fileEntry('loose.txt'), fileEntry('.hidden')]);
  assert.deepEqual(names(walked), ['GXRCE.DAT', 'GXRFX.BIN', 'R0000001.DNG', 'R0000001.JPG', 'R0000002.JPG', 'R0000003.JPG', 'R0010001.JPG', 'loose.txt']);
  assert.equal(walked.capped, false);
});

test('walker: depth cap stops below level 4 and says so', async () => {
  const { walkEntries, DROP_MAX_DEPTH } = await import('../../mobile/src/app/file_drop.ts');
  assert.equal(DROP_MAX_DEPTH, 4);
  // 第 0 层 a/，第 1 层 b/，……，第 4 层的文件还能读到，第 4 层的文件夹不再往下。
  const deep = dirEntry('a', [dirEntry('b', [dirEntry('c', [dirEntry('d', [fileEntry('R4.JPG'), dirEntry('e', [fileEntry('R5.JPG')])])])])]);
  const walked = await walkEntries([deep]);
  assert.deepEqual(names(walked), ['R4.JPG']);
  assert.equal(walked.capped, true);
  const shallow = await walkEntries([dirEntry('a', [dirEntry('b', [fileEntry('R2.JPG')])])]);
  assert.equal(shallow.capped, false);
});

test('walker: file cap stops at 5000 files and says so', async () => {
  const { walkEntries, DROP_MAX_FILES, DROP_CAP_NOTE } = await import('../../mobile/src/app/file_drop.ts');
  assert.equal(DROP_MAX_FILES, 5000);
  assert.ok(DROP_CAP_NOTE.includes('5000'));
  const many = Array.from({ length: 5003 }, (_, i) => fileEntry(`R${String(i).padStart(7, '0')}.JPG`));
  const walked = await walkEntries([dirEntry('DCIM', many, 100)]);
  assert.equal(walked.files.length, 5000);
  assert.equal(walked.capped, true);
  const exact = await walkEntries(Array.from({ length: 3 }, (_, i) => fileEntry(`R${i}.JPG`)), 4, 3);
  assert.equal(exact.files.length, 3);
  assert.equal(exact.capped, false, 'exactly at the cap with nothing left is not capped');
});

test('walker: unreadable files and folders are skipped, not thrown', async () => {
  const { walkEntries } = await import('../../mobile/src/app/file_drop.ts');
  const broken = { name: 'R1.JPG', isFile: true, isDirectory: false, file: (_ok, fail) => fail(new Error('gone')) };
  const badDir = { name: 'DCIM', isFile: false, isDirectory: true, createReader: () => ({ readEntries: (_ok, fail) => fail(new Error('gone')) }) };
  const walked = await walkEntries([broken, badDir, fileEntry('R2.JPG')]);
  assert.deepEqual(names(walked), ['R2.JPG']);
});

test('classify counts unusable files without the calibration zip and .cube', async () => {
  const { classify } = await import('../../mobile/src/lib/group.ts');
  const out = classify([
    { name: 'R1.DNG', lastModified: 1 },
    { name: 'R1.JPG', lastModified: 1 },
    { name: 'IMG_0001.JPG', lastModified: 1 },
    { name: 'R2.jpeg', lastModified: 1 },
    { name: 'GXRCE.DAT', lastModified: 1 },
    { name: 'GXR 校准数据 2026-11-26.zip', lastModified: 1 },
    { name: 'film.cube', lastModified: 1 },
  ]);
  assert.equal(out.ignored, 4, 'the existing subtitle count is unchanged');
  assert.equal(out.unusable, 2);
  assert.equal(out.photos.length, 1);
});

test('unusedNote: some used, nothing used, nothing to say', async () => {
  const { classify } = await import('../../mobile/src/lib/group.ts');
  const { unusedNote } = await import('../../mobile/src/app/import_note.ts');
  const f = (name) => ({ name, lastModified: 1 });
  assert.equal(unusedNote(classify([f('R1.JPG'), f('a.JPG'), f('b.JPG')])), '2 个文件不是 GXR 拍的照片（要 R 开头的 .JPG / .DNG），没有用上');
  assert.equal(unusedNote(classify([f('notes.txt')])), '没有能用的照片：这个文件不是 GXR 拍的照片（要 R 开头的 .JPG / .DNG）');
  assert.equal(unusedNote(classify([f('a.txt'), f('b.png')])), '没有能用的照片：这 2 个文件都不是 GXR 拍的照片（要 R 开头的 .JPG / .DNG）');
  assert.equal(unusedNote(classify([f('GXRFX.BIN'), f('x.txt')])), '1 个文件不是 GXR 拍的照片（要 R 开头的 .JPG / .DNG），没有用上');
  assert.equal(unusedNote(classify([f('R1.JPG'), f('GXRCE.DAT'), f('pack.zip'), f('x.cube')])), null);
});

/** 相机配方文件控制器：readDat 按 DAT 字节第一个字节决定是好是坏（1 好，2 是缺 BIN 的双文件卡 DAT，别的不是 DAT）。 */
async function controller() {
  const { CamFilesController } = await import('../../mobile/src/app/book_camfiles.ts');
  const io = {
    readFile: async (file) => new Uint8Array(await file.arrayBuffer()).buffer,
    readDat: async (bytes, library) => {
      const kind = new Uint8Array(bytes)[0];
      if (kind === 2 && !library) throw new Error('缺少 GXRFX.BIN。请将 GXRCE.DAT 和 GXRFX.BIN 一起从存储卡复制到同一文件夹，再同时导入。');
      if (kind !== 1 && kind !== 2) throw new Error('不是 GXRCE.DAT');
      return { bytes, summary: '{"generation":250}', listing: '{"units":[]}', entries: new Uint8Array(24 * 552), library: library ?? null };
    },
  };
  let changed = 0;
  const ctl = new CamFilesController(io, { onBump() {}, onChanged() { changed += 1; } });
  return { ctl, changed: () => changed };
}
const datFile = (kind) => new File([new Uint8Array([kind, 0, 0])], 'GXRCE.DAT');
const binFile = () => new File([new Uint8Array(128)], 'GXRFX.BIN');
const settle = () => new Promise((r) => setTimeout(r, 20));

test('camfiles: only GXRFX.BIN says the DAT is missing and replaces an old error', async () => {
  const { BIN_ONLY_ERROR } = await import('../../mobile/src/app/book_camfiles.ts');
  assert.equal(BIN_ONLY_ERROR, '还差 GXRCE.DAT，请把 GXRCE.DAT 和 GXRFX.BIN 一起选');
  const { ctl, changed } = await controller();
  ctl.ingest(datFile(9), 1, null);
  await settle();
  assert.equal(ctl.datError, '无法读取相机配方文件：不是 GXRCE.DAT');
  const before = changed();
  ctl.ingest(null, 0, binFile());
  await settle();
  assert.equal(ctl.datError, BIN_ONLY_ERROR, 'a broken DAT is not kept to pair with a later BIN');
  assert.ok(changed() > before, 'the screen is told to repaint');
  assert.equal(ctl.dat, null);
});

test('camfiles: DAT then BIN in two picks still pairs up', async () => {
  const { ctl } = await controller();
  ctl.ingest(datFile(2), 1, null);
  await settle();
  assert.ok(ctl.datError?.includes('缺少 GXRFX.BIN'), 'DAT alone: the core message names the missing BIN');
  ctl.ingest(null, 0, binFile());
  await settle();
  assert.equal(ctl.datError, null);
  assert.ok(ctl.dat?.library, 'the pending DAT was kept and paired with the BIN');
});

test('othersReadable: another readable file means one lost file, not a pulled card', async () => {
  const { othersReadable } = await import('../../mobile/src/app/card_probe.ts');
  // 假的读取：名字以 lost 开头的「原文件」读不了（探测时只给读取函数一个 1 字节的切片，按切片的来源判断）。
  const gone = new WeakSet();
  const ok = (name) => new File(['x'], name);
  const lost = (name) => {
    const file = new File(['x'], name);
    gone.add(file);
    return file;
  };
  const tried = [];
  const read = async (probe) => {
    tried.push(probe.size);
    return !probe.__from || !gone.has(probe.__from);
  };
  const realSlice = File.prototype.slice;
  File.prototype.slice = function slice(...args) {
    const part = realSlice.apply(this, args);
    part.__from = this;
    return part;
  };
  const OrigFile = globalThis.File;
  globalThis.File = class extends OrigFile {
    constructor(parts, name, opts) {
      super(parts, name, opts);
      if (parts?.[0]?.__from) this.__from = parts[0].__from;
    }
  };
  const photo = (jpg, gone = false) => ({ jpg, dng: null, gone });
  const trigger = photo(lost('R1.JPG'));
  try {
    assert.equal(await othersReadable([trigger, photo(lost('R2.JPG')), photo(ok('R3.JPG'))], trigger, read), true);
    assert.equal(await othersReadable([trigger, photo(lost('R2.JPG')), photo(lost('R3.JPG'))], trigger, read), false, 'nothing reads: pulled card');
    assert.equal(await othersReadable([trigger], trigger, read), false, 'no other photo to try: pulled card as before');
    assert.equal(await othersReadable([trigger, photo(ok('R4.JPG'), true)], trigger, read), false, 'photos already marked gone are not tried');
    assert.equal(await othersReadable([trigger, photo(ok('R5.JPG'))], trigger, () => Promise.reject(new Error('worker gone'))), false, 'a failed read counts as unreadable');
    assert.ok(tried.every((size) => size === 1), 'only 1 byte of each file is read');
  } finally {
    File.prototype.slice = realSlice;
    globalThis.File = OrigFile;
  }
});
