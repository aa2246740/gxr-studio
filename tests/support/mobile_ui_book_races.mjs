// Book 的异步时序（Sol r1 P2、P6）：在开发服务器的页面里直接 import 真的 Book，把读文件、Worker、IndexedDB
// 换成手里攥着的回复，造「清空后晚到」「旧的比新的晚到」「超大文件」这些时序。不用照片，不用相机数据。

import { pathToFileURL } from 'node:url';

import { readFileSync } from 'node:fs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = manifestPath ? JSON.parse(readFileSync(manifestPath, 'utf8')) : {};
const packB64 = manifest.pack ? readFileSync(manifest.pack).toString('base64') : null;
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
  out.cases = await page.evaluate(async (packB64) => {
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
      summary: JSON.stringify({ generation: 158, name }),
      listing: JSON.stringify({ units: [] }),
      entries: new Uint8Array(24 * 552),
      bytes: new ArrayBuffer(8),
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
          return true;
        },
        watermark: async () => 0,
        resolve: () => Promise.reject(new Error('no resolves here')),
        calibCheck: () => Promise.reject(new Error('no checks here')),
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
    // 8. 缓存上限（Sol r1）：JS 16 张，显卡 24 张；舞台那张不会被挤掉。
    {
      const { book } = make();
      const added = new Map();
      const engine = {
        hasBake: (k) => added.has(k),
        addBake: (k, meta, data) => added.set(k, data),
      };
      book.attach(engine);
      book.io.bake = async () => ({ meta: { nx: 1, ny: 1, origin: [0, 0], step: 0.375 }, data: new ArrayBuffer(16) });
      // color_resolve 换成假的：每个选择一个不同的条目 sha256。
      book.io.resolve = async (_dat, recipe) => {
        const sel = JSON.parse(recipe);
        return { entry: new Uint8Array(552), entrySha256: `e${sel.levels.amount}`, levels: sel.levels };
      };
      book.dat = { name: 'GXRCE.DAT', read: { summary: '{}', listing: '{}', entries: new Uint8Array(24 * 552), bytes: new ArrayBuffer(8) }, generation: 158, listing: { units: [] } };
      const pair = { ctx: 'c', ring: 'r' };
      book.pack = { name: 'p', savedAt: 1, manifestSha256: 'x', units: [{ unit: 'jlaunch' }], files: new Map([['c', new Uint8Array(1)], ['r', new Uint8Array(1)]]) };
      book.picks.set('jlaunch', { generation: book.generationId(), names: { 200: pair, 1600: pair } });
      const card = (i) => ({ id: `builtin:${i}`, kind: 'builtin', name: String(i), desc: null, slot: 1, recipeId: null, number: '01', controls: [{ name: 'amount', label: 'Amt', min: 0, max: 99, default: i, cap_lower: false, cap_upper: false, amount_step: 1, levels: [i] }] });
      const first = await book.bake(card(0), 'jlaunch', 200);
      const stage = book.gpuKey(first);
      book.pin(first);
      for (let i = 1; i < 40; i++) book.gpuKey(await book.bake(card(i), 'jlaunch', 200));
      const stillStage = book.gpuKey(first);
      result.caps = { engineKeys: added.size, js: book.done.size, stageKept: stillStage === stage, stage: !!stage };
    }
    // 9. 配方卡小图的全局上限：40 张照片、每张 11 张卡之后不超过上限，打开的那张一张不少，挤掉的位图已关闭；清空全部关闭。
    {
      const { LookCache } = await import('/src/app/looks.ts');
      const cache = new LookCache();
      const blank = new ImageData(192, 128);
      const photos = Array.from({ length: 40 }, (_, i) => ({ id: `r${i}`, version: 1, looks: new Map() }));
      const all = [];
      cache.setOpen(photos[0]);
      for (const photo of photos) {
        for (let k = 0; k < 11; k++) {
          const bitmap = await createImageBitmap(blank);
          all.push({ photo, bitmap });
          cache.put(photo, `bake:${k}`, 1, bitmap);
        }
      }
      const stats = cache.stats();
      const openKept = [...photos[0].looks.values()].filter((look) => look.bitmap.width === 192).length;
      const closedButListed = photos.some((photo) => [...photo.looks.values()].some((look) => look.bitmap.width === 0));
      const evicted = all.filter(({ photo, bitmap }) => ![...photo.looks.values()].some((look) => look.bitmap === bitmap));
      const evictedClosed = evicted.every(({ bitmap }) => bitmap.width === 0);
      const newest = [...photos[39].looks.keys()].length;
      cache.clear();
      result.looks = {
        bytes: stats.bytes,
        limit: stats.limit,
        entries: stats.entries,
        openKept,
        closedButListed,
        evicted: evicted.length,
        evictedClosed,
        newest,
        afterClear: cache.stats().entries,
        allClosed: all.every(({ bitmap }) => bitmap.width === 0),
        mapsEmpty: photos.every((photo) => photo.looks.size === 0),
      };
    }
    // 10. 真的 Worker 和 IndexedDB：删除事务失败时保留校准包、写一行，重开还在；再点一次删除成功，重开也没了（Sol r2）。
    if (packB64) {
      const api = await import('/src/core/api.ts');
      const idb = await import('/src/lib/idb.ts');
      const bytes = Uint8Array.from(atob(packB64), (c) => c.charCodeAt(0));
      const real = () => ({
        readFile: async () => null,
        readDat: api.readDat,
        importCalib: api.importCalib,
        restoreCalib: api.restoreCalib,
        bake: api.bakeIn,
        resolve: api.resolveRecipe,
        calibCheck: api.calibCheck,
        deletePack: idb.deletePack,
        watermark: idb.loadWatermark,
      });
      const zipFile = (name) => new File([bytes], name, { lastModified: 1 });
      const fresh = async () => {
        const book = new Book(real());
        await book.restore();
        return book.pack?.name ?? null;
      };
      const book = new Book(real());
      const imported = await book.importPack(zipFile('A.zip'));
      book.io.deletePack = () => Promise.reject(new DOMException('the transaction was aborted', 'AbortError'));
      await book.removePack();
      const failed = { pack: book.pack?.name ?? null, error: book.packError, busy: book.packBusy, reopened: await fresh() };
      book.io.deletePack = idb.deletePack;
      await book.removePack();
      const retried = { pack: book.pack, error: book.packError, busy: book.packBusy, reopened: await fresh() };
      result.failedDelete = { imported, failed, retried };
    }
    // 11. 版本水位（Sol r2）：时钟固定在 1000，导入 A 再删除（墓碑 1001）；新会话导入 B 要排在墓碑之后、真的存下来；
    //     读不到水位的会话导入 C 会被拒绝写入，界面要照实说没存上，重开仍是 B。
    if (packB64) {
      const api = await import('/src/core/api.ts');
      const idb = await import('/src/lib/idb.ts');
      const bytes = Uint8Array.from(atob(packB64), (c) => c.charCodeAt(0));
      await new Promise((done, fail) => {
        const req = indexedDB.deleteDatabase('gxr-mobile');
        req.onsuccess = done;
        req.onerror = fail;
        req.onblocked = done;
      });
      const real = () => ({
        readFile: async () => null,
        readDat: api.readDat,
        importCalib: api.importCalib,
        restoreCalib: api.restoreCalib,
        bake: api.bakeIn,
        resolve: api.resolveRecipe,
        calibCheck: api.calibCheck,
        deletePack: idb.deletePack,
        watermark: idb.loadWatermark,
      });
      const zipFile = (name) => new File([bytes], name, { lastModified: 1 });
      const realNow = Date.now;
      try {
        Date.now = () => 1000;
        const first = new Book(real());
        const a = await first.importPack(zipFile('A.zip'));
        await first.removePack();
        const markAfterDelete = await idb.loadWatermark();
        const second = new Book(real());
        await second.restore();
        const restoredTomb = second.pack;
        const b = await second.importPack(zipFile('B.zip'));
        const bState = { ok: b, error: second.packError, savedAt: second.pack?.savedAt ?? null };
        const third = new Book(real());
        await third.restore();
        const blind = new Book({ ...real(), watermark: () => Promise.reject(new Error('unreadable')) });
        const c = await blind.importPack(zipFile('C.zip'));
        const cState = { ok: c, error: blind.packError, pack: blind.pack?.name ?? null };
        const fourth = new Book(real());
        await fourth.restore();
        result.clockRollback = { a, markAfterDelete, restoredTomb, b: bState, reopenedB: third.pack?.name ?? null, c: cState, reopenedAfterC: fourth.pack?.name ?? null };
      } finally {
        Date.now = realNow;
      }
    }
    // 7. IndexedDB：删除留下的时间比晚到的旧导入新，旧导入不写；更新的导入照常写。
    {
      const idb = await import('/src/lib/idb.ts');
      const zip = (n) => new Uint8Array([n]).buffer;
      // 比前面的真实存储场景留下的时间都新（它们用的是 Date.now()）。
      const t = Date.now() + 1e6;
      await idb.savePack({ zip: zip(1), name: 'a.zip', savedAt: t + 5 });
      await idb.deletePack(t + 6);
      await idb.savePack({ zip: zip(2), name: 'late.zip', savedAt: t + 5.5 });
      const afterLate = await idb.loadPack();
      await idb.savePack({ zip: zip(3), name: 'b.zip', savedAt: t + 7 });
      const afterNew = await idb.loadPack();
      await idb.deletePack(t + 8);
      result.idb = { afterLate, afterNew: afterNew?.name ?? null, cleared: await idb.loadPack() };
    }

    return result;
  }, packB64);
} finally {
  await browser.close();
}
process.stdout.write(`${JSON.stringify(out)}\n`);
