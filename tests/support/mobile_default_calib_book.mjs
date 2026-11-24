// 默认校准在 Book 里的先后时序：在开发服务器的页面里 import 真的 Book，读存下的包、下载默认校准、导入、删除都换成
// 手里攥着的回复。不用照片，不用相机数据。由 mobile_default_calib_browser.mjs 在页面准备好之后调用。

export function bookCases(page) {
  return page.evaluate(async () => {
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
    const pack = (tag) => ({ ok: true, units: [{ unit: 'jlaunch' }], manifestSha256: tag, files: new Map([['manifest.json', new Uint8Array([1])]]), saved: true });
    const fail = (detail) => ({ ok: false, zip: null, message: null, detail });
    const zip = (name) => new File([new Uint8Array([80, 75, 5, 6])], name);
    function make(stored = null) {
      const calls = { restore: 0, default: 0, del: 0 };
      const pendingDefault = [];
      const pendingImport = [];
      const io = {
        readFile: async () => null,
        readDat: async () => {
          throw new Error('unused');
        },
        importCalib: () => {
          const d = deferred();
          pendingImport.push(d);
          return d.promise;
        },
        restoreCalib: async () => {
          calls.restore += 1;
          return stored;
        },
        bake: async () => {
          throw new Error('unused');
        },
        resolve: async () => {
          throw new Error('unused');
        },
        calibCheck: async () => {
          throw new Error('unused');
        },
        deletePack: async () => {
          calls.del += 1;
          return true;
        },
        watermark: async () => 0,
        defaultCalib: () => {
          calls.default += 1;
          const d = deferred();
          pendingDefault.push(d);
          return d.promise;
        },
      };
      return { book: new Book(io), calls, pendingDefault, pendingImport };
    }
    const view = (t) => ({
      kind: t.book.pack?.kind ?? null,
      name: t.book.pack?.name ?? null,
      error: t.book.defaultError,
      loading: t.book.loadingDefault,
      unit: t.book.unitState('jlaunch').state,
      packError: t.book.packError,
      calls: { ...t.calls },
    });
    const out = {};

    // 1. 没存过包：读回存下的包之后才下载默认校准（不挡首屏），下载、核对期间是「正在核对」，用上以后旧的表作废。
    {
      const t = make();
      const g0 = t.book.generationId();
      const restoring = t.book.restore();
      const beforeRestore = t.calls.default;
      await restoring;
      const loading = view(t);
      t.pendingDefault[0].resolve(pack('DEFAULT'));
      await tick();
      out.fresh = { beforeRestore, loading, ready: view(t), bumped: t.book.generationId() !== g0 };
    }

    // 2. 默认校准还在下载时导入了自己的包：导入的盖过默认校准；默认校准晚到也不换掉它，也不让旧的表作废。
    // 3. 恢复默认（删掉导入的包）：直接用这次会话已经核对过的默认校准，不再下载。
    {
      const t = make();
      await t.book.restore();
      const importing = t.book.importPack(zip('mine.zip'));
      await tick();
      t.pendingImport[0].resolve(pack('MINE'));
      const imported = await importing;
      const g1 = t.book.generationId();
      t.pendingDefault[0].resolve(pack('DEFAULT'));
      await tick();
      out.late = { imported, view: view(t), sameGeneration: t.book.generationId() === g1 };
      await t.book.removePack();
      await tick();
      out.restored = { view: view(t), bumped: t.book.generationId() !== g1 };
    }

    // 4. 默认校准用不了：下载不下来、文件不对、Worker 出错，都是中性转换并写明原因，不抛；恢复默认时再试一次。
    {
      const t = make();
      await t.book.restore();
      t.pendingDefault[0].resolve(fail('fetch'));
      await tick();
      const offline = view(t);
      const u = make();
      await u.book.restore();
      u.pendingDefault[0].resolve(fail('sha256'));
      await tick();
      const tampered = view(u);
      const w = make();
      await w.book.restore();
      w.pendingDefault[0].reject(new Error('worker died'));
      await tick();
      const crashed = view(w);
      // 再试：导入一份，再恢复默认，这次默认校准下载成功。
      const importing = t.book.importPack(zip('mine.zip'));
      await tick();
      t.pendingImport[0].resolve(pack('MINE'));
      await importing;
      const removing = t.book.removePack();
      await removing;
      const retrying = view(t);
      t.pendingDefault[1].resolve(pack('DEFAULT'));
      await tick();
      out.failed = { offline, tampered, crashed, retrying, retried: view(t) };
    }

    // 5. 存过自己的包：读回来就用它，默认校准一次也不下载。
    {
      const t = make({ ...pack('STORED'), name: 'mine.zip', savedAt: 5 });
      await t.book.restore();
      await tick();
      out.stored = view(t);
    }

    // 6. 存下的包读回来核对没过：写出原因，同时用默认校准。
    {
      const t = make({ ok: false, zip: null, message: '校准数据被改动，请重新导入', detail: 'x', name: 'mine.zip', savedAt: 5 });
      await t.book.restore();
      t.pendingDefault[0].resolve(pack('DEFAULT'));
      await tick();
      out.storedBroken = view(t);
    }
    // 7. calibSettled：没在等就马上好；默认校准落定时好；一直不来就到时好。都不留监听。
    {
      const idle = make({ ...pack('STORED'), name: 'mine.zip', savedAt: 5 });
      await idle.book.restore();
      const idleDone = await Promise.race([idle.book.calibSettled(10_000).then(() => true), tick().then(() => false)]);
      const t = make();
      const listenersBefore = t.book.listeners.size;
      await t.book.restore();
      const waiting = t.book.calibSettled(10_000).then(() => true);
      t.pendingDefault[0].resolve(pack('DEFAULT'));
      const landed = await Promise.race([waiting, new Promise((done) => setTimeout(() => done(false), 1000))]);
      const listenersAfterLanded = t.book.listeners.size;
      const u = make();
      await u.book.restore();
      const t0 = performance.now();
      await u.book.calibSettled(50);
      const timeoutMs = performance.now() - t0;
      out.settled = {
        idle: idleDone,
        landed,
        listenersBefore,
        listenersAfterLanded,
        timedOut: true,
        timeoutMs,
        stillLoading: u.book.loadingDefault,
        listenersAfterTimeout: u.book.listeners.size,
      };
    }
    // 8. 一轮只有一个截止时间：晚一点再等的人不会再多等 ms；到时以后再问马上就好；下一轮（恢复默认时重试）重新算。
    {
      const t = make();
      await t.book.restore();
      const t0 = performance.now();
      const first = t.book.calibSettled(300).then(() => performance.now() - t0);
      await new Promise((done) => setTimeout(done, 150));
      const second = t.book.calibSettled(300).then(() => performance.now() - t0);
      const [firstMs, secondMs] = await Promise.all([first, second]);
      const t1 = performance.now();
      await t.book.calibSettled(300);
      const afterDeadlineMs = performance.now() - t1;
      // 默认校准用不了，再导入一份、恢复默认：新的一轮，重新等。
      t.pendingDefault[0].resolve(fail('fetch'));
      await tick();
      const importing = t.book.importPack(zip('mine.zip'));
      await tick();
      t.pendingImport[0].resolve(pack('MINE'));
      await importing;
      await t.book.removePack();
      const t2 = performance.now();
      await t.book.calibSettled(120);
      const nextRoundMs = performance.now() - t2;
      out.deadline = { firstMs, secondMs, afterDeadlineMs, nextRoundMs, loading: t.book.loadingDefault, calls: { ...t.calls } };
    }

    // 9. 默认校准用不了以后，导入失败不会让它重新下载，也不会闪一下「正在加载」。
    {
      const t = make();
      await t.book.restore();
      t.pendingDefault[0].resolve(fail('fetch'));
      await tick();
      const before = view(t);
      let flashed = false;
      t.book.onChange(() => {
        if (t.book.loadingDefault) flashed = true;
      });
      const importing = t.book.importPack(zip('bad.zip'));
      await tick();
      t.pendingImport[0].resolve({ ok: false, zip: 'compressed', message: null, detail: 'x' });
      const imported = await importing;
      await tick();
      out.badImport = { before, imported, flashed, after: view(t) };
    }
    return out;
  });
}
