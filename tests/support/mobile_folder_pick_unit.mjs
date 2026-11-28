// 「选文件夹」的单元测试（2026-11-28）：Mac 在卡上生成的 ._ 文件和别的点开头文件，分类时直接跳过（拖和选都走这里）；
// 选文件夹拿到的文件按拖文件夹的规矩筛（路径里点开头的段、层数和文件数上限），留下的是原来的对象，webkitRelativePath 还在。

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

const incoming = (names) => names.map((name, i) => ({ name, lastModified: 1000 + i }));

test('classify: ._ AppleDouble and other dot-files are skipped, not counted as unused', async () => {
  const { classify } = await import('../../mobile/src/lib/group.ts');
  const got = classify(incoming(['R0000001.DNG', '._R0000001.DNG', 'R0000001.JPG', '._R0000001.JPG', '.DS_Store', '._GXRCE.DAT', 'GXRCE.DAT', 'GXRFX.BIN', 'notes.txt']));
  assert.deepEqual(got.photos.map((p) => [p.stem, p.dngIndex, p.jpgIndex]), [['R0000001', 0, 2]]);
  assert.equal(got.datIndex, 6);
  assert.equal(got.datCount, 1);
  assert.equal(got.binIndex, 7);
  assert.equal(got.ignored, 1);
  assert.equal(got.unusable, 1);
});

test('classify: a batch of only ._ files says nothing', async () => {
  const { classify } = await import('../../mobile/src/lib/group.ts');
  const { unusedNote } = await import('../../mobile/src/app/import_note.ts');
  const got = classify(incoming(['._R0000001.JPG', '._R0000002.DNG']));
  assert.equal(got.unusable, 0);
  assert.equal(unusedNote(got), null);
});

const picked = (paths) => paths.map((p) => ({ name: p.split('/').pop(), webkitRelativePath: p }));

test('folderFiles: dot segments skipped, objects and relative paths kept, order unchanged', async () => {
  const { folderFiles } = await import('../../mobile/src/app/file_drop.ts');
  const files = picked([
    'CARD/GXRCE.DAT',
    'CARD/.DS_Store',
    'CARD/.Trashes/501/R0009999.JPG',
    'CARD/DCIM/100RICOH/R0000001.DNG',
    'CARD/DCIM/100RICOH/._R0000001.DNG',
    'CARD/DCIM/100RICOH/R0000001.JPG',
    'CARD/MISC/notes.txt',
  ]);
  const got = folderFiles(files);
  assert.equal(got.capped, false);
  assert.deepEqual(got.files.map((f) => f.webkitRelativePath), ['CARD/GXRCE.DAT', 'CARD/DCIM/100RICOH/R0000001.DNG', 'CARD/DCIM/100RICOH/R0000001.JPG', 'CARD/MISC/notes.txt']);
  assert.equal(got.files[1], files[3]);
});

test('folderFiles: same depth cap as a dropped folder', async () => {
  const { folderFiles, walkEntries, DROP_MAX_DEPTH } = await import('../../mobile/src/app/file_drop.ts');
  assert.equal(DROP_MAX_DEPTH, 4);
  const got = folderFiles(picked(['a/b/c/d/R4.JPG', 'a/b/c/d/e/R5.JPG']));
  assert.deepEqual(got.files.map((f) => f.name), ['R4.JPG']);
  assert.equal(got.capped, true);
  assert.equal(folderFiles(picked(['a/b/R2.JPG'])).capped, false);
  // 同一棵树拖进来，读到的也一样。
  const file = (name) => ({ name, isFile: true, isDirectory: false, file: (ok) => ok(new File([name], name)) });
  const dir = (name, children) => ({ name, isFile: false, isDirectory: true, createReader() { let done = false; return { readEntries(ok) { ok(done ? [] : children); done = true; } }; } });
  const walked = await walkEntries([dir('a', [dir('b', [dir('c', [dir('d', [file('R4.JPG'), dir('e', [file('R5.JPG')])])])])])]);
  assert.deepEqual(walked.files.map((f) => f.name), got.files.map((f) => f.name));
  assert.equal(walked.capped, got.capped);
});

test('folderFiles: file cap stops at 5000 and says so', async () => {
  const { folderFiles, DROP_MAX_FILES } = await import('../../mobile/src/app/file_drop.ts');
  const many = picked(Array.from({ length: DROP_MAX_FILES + 3 }, (_, i) => `DCIM/R${String(i).padStart(7, '0')}.JPG`));
  const got = folderFiles(many);
  assert.equal(got.files.length, DROP_MAX_FILES);
  assert.equal(got.capped, true);
  assert.equal(folderFiles(many.slice(0, DROP_MAX_FILES)).capped, false);
});

test('folderFiles: a plain file without a relative path is filtered by its name', async () => {
  const { folderFiles } = await import('../../mobile/src/app/file_drop.ts');
  const got = folderFiles([{ name: '._R0000001.JPG', webkitRelativePath: '' }, { name: 'R0000001.JPG', webkitRelativePath: '' }]);
  assert.deepEqual(got.files.map((f) => f.name), ['R0000001.JPG']);
});
