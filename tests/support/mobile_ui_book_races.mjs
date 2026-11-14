// Book 的异步时序（Sol r1 P2、P6）：在开发服务器的页面里直接 import 真的 Book，把读文件、Worker、IndexedDB
// 换成手里攥着的回复，造「清空后晚到」「旧的比新的晚到」「超大文件」这些时序。不用照片，不用相机数据。

import { pathToFileURL } from 'node:url';

const [runtime, url] = process.argv.slice(2);
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();
const browser = await launchPinnedChromium({ args: [] });
const out = { pageErrors: [] };

try {
  const page = await browser.newPage();
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  if (process.env.GXR_DEBUG) { page.on('console', (m) => console.error('console', m.text())); page.on('framenavigated', (f) => console.error('nav', f.url(), Date.now())); }
  await page.goto(url);
  // 第一次编译 Worker 时开发服务器可能整页刷新：等它停稳。
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) break;
  }
  out.cases = await page.evaluate(async () => {
    const { Book } = await import('/src/app/book.ts');
    const tick = () => new Promise((done) => setTimeout(done, 0));
    const deferred = () => {
      let resolve;
      let reject;
      const promise = new Promise((a, b) => {
        resolve = a;
        reject = b;
      });
      return { promise, resolve, reject };
    };
    const datRead = (name) => ({
      summary: JSON.stringify({ generation: 158, units: [], looks: [], recipes: [], users: [], name }),
      menu: JSON.stringify({ controls: [], recipes: [], items: [] }),
      entries: new Uint8Array(24 * 552),
    });
    const calibOk = (tag) => ({ ok: true, units: [], manifestSha256: tag, files: new Map() });
    /** 一个 Book，异步边界都交给测试来放行。`calls` 记下每个边界被调了几次。 */
    function make() {
      const pending = { read: [], dat: [], calib: [], load: [], pack: [] };
      const calls = { read: 0, dat: 0, calib: 0, load: 0, save: 0, del: 0, packFile: 0 };
      const io = {
        readFile: () => {
          calls.read += 1;
          const d = deferred();
          pending.read.push(d);
          return d.promise;
        },
        readDat: () => {
          calls.dat += 1;
          const d = deferred();
          pending.dat.push(d);
          return d.promise;
        },
        readCalib: () => {
          calls.calib += 1;
          const d = deferred();
          pending.calib.push(d);
          return d.promise;
        },
        importCalib: () => {
          calls.packFile += 1;
          const d = deferred();
          pending.pack.push(d);
          return d.promise;
        },
        restoreCalib: () => {
          calls.load += 1;
          const d = deferred();
          pending.load.push(d);
          return d.promise;
        },
        bake: () => Promise.reject(new Error('no bakes here')),
        loadPack: () => {
          calls.load += 1;
          const d = deferred();
          pending.load.push(d);
          return d.promise;
        },
        savePack: async () => {
          calls.save += 1;
        },
        deletePack: async () => {
          calls.del += 1;
        },
      };
      return { book: new Book(io), pending, calls };
    }
    const file = (name, size = 10) => ({ name, size, lastModified: 1 });
    const result = {};

    // 1. setDat(A) 还在读，清空，A 才读完：dat 仍是空的，不忙。
    {
      const { book, pending } = make();
      const run = book.setDat(file('GXRCE.DAT'), 1);
      book.clearDat();
      pending.read[0].resolve(new ArrayBuffer(8));
      await tick();
      pending.dat[0]?.resolve(datRead('A'));
      await run;
      result.clearThenLateSuccess = { dat: book.dat, busy: book.datBusy, error: book.datError };
    }
    // 2. 清空之后 A 读失败：不留错误那一行。
    {
      const { book, pending } = make();
      const run = book.setDat(file('GXRCE.DAT'), 1);
      book.clearDat();
      pending.read[0].reject(new Error('NotReadableError'));
      await run;
      result.clearThenLateFailure = { dat: book.dat, busy: book.datBusy, error: book.datError };
    }
    // 3. 先选 A，再选 B；B 先好，A 最后才好：留下的是 B。
    {
      const { book, pending } = make();
      const a = book.setDat(file('GXRCE.DAT'), 1);
      const b = book.setDat(file('GXRCE.DAT'), 1);
      pending.read[1].resolve(new ArrayBuffer(8));
      await tick();
      pending.dat[0].resolve(datRead('B'));
      await b;
      pending.read[0].resolve(new ArrayBuffer(8));
      await tick();
      pending.dat[1]?.resolve(datRead('A'));
      await a;
      result.olderLate = { name: book.dat ? JSON.parse(book.dat.read.summary).name : null, busy: book.datBusy };
    }
    // 4. 导入校准数据还在核对时删除：核对完也不复活。
    {
      const { book, pending, calls } = make();
      const run = book.importPack(file('GXR 校准数据.zip'));
      await tick();
      const removed = book.removePack();
      (pending.pack[0] ?? pending.read[0]).resolve(pending.pack[0] ? { ...calibOk('X'), saved: true } : new ArrayBuffer(8));
      await tick();
      pending.calib[0]?.resolve(calibOk('X'));
      await run;
      await removed;
      result.removeThenLateImport = { pack: book.pack, busy: book.packBusy, error: book.packError, deletes: calls.del };
    }
    // 5. 打开页面时的恢复比后来的导入晚到：留下导入的那份。
    {
      const { book, pending } = make();
      const restoring = book.restore();
      const importing = book.importPack(file('GXR 校准数据.zip'));
      await tick();
      if (pending.pack[0]) pending.pack[0].resolve({ ...calibOk('NEW'), saved: true });
      else {
        pending.read[0].resolve(new ArrayBuffer(8));
        await tick();
        pending.calib[0].resolve(calibOk('NEW'));
      }
      await importing;
      const old = { ...calibOk('OLD'), name: 'old.zip', savedAt: 1 };
      if (pending.calib.length > 1 || !pending.pack.length) {
        pending.load[0].resolve({ zip: new ArrayBuffer(8), name: 'old.zip', savedAt: 1 });
        await tick();
        pending.calib[pending.calib.length - 1]?.resolve(calibOk('OLD'));
      } else pending.load[0].resolve(old);
      await restoring;
      result.lateRestore = { sha: book.pack?.manifestSha256 ?? null };
    }
    // 6. 超过上限的文件一个字节都不读（P6）：直接给出「太大」那一行。
    {
      const { book, calls } = make();
      const ok = await book.importPack(file('huge.zip', 4 * 2 ** 30));
      result.oversized = { ok, reads: calls.read + calls.packFile + calls.calib, error: book.packError };
    }
    // 6b. 正常大小的包：File 直接交给 Worker 那一边，主线程一次 readFile 都没有（不在主线程复制整包）。
    {
      const { book, pending, calls } = make();
      const run = book.importPack(file('GXR 校准数据.zip', 17350));
      await tick();
      pending.pack[0].resolve({ ...calibOk('OK'), saved: true });
      const ok = await run;
      result.inLimit = { ok, packFile: calls.packFile, reads: calls.read, sha: book.pack?.manifestSha256 ?? null };
    }
    // 7. IndexedDB：删除留下的时间比晚到的旧导入新，旧导入不写；更新的导入照常写。
    {
      const idb = await import('/src/lib/idb.ts');
      const zip = (n) => new Uint8Array([n]).buffer;
      await idb.savePack({ zip: zip(1), name: 'a.zip', savedAt: 5 });
      await idb.deletePack(6);
      await idb.savePack({ zip: zip(2), name: 'late.zip', savedAt: 5.5 });
      const afterLate = await idb.loadPack();
      await idb.savePack({ zip: zip(3), name: 'b.zip', savedAt: 7 });
      const afterNew = await idb.loadPack();
      await idb.deletePack(8);
      result.idb = { afterLate, afterNew: afterNew?.name ?? null, cleared: await idb.loadPack() };
    }

    return result;
  });
} finally {
  await browser.close();
}
process.stdout.write(`${JSON.stringify(out)}\n`);
