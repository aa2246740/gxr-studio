// calibGate（导出前等默认校准落定）的排队：用 Node 直接跑 TypeScript，Book 换成手里攥着回复的假对象。
// 假 Book 照真 Book 的规矩：一轮只有一个截止时间，到时以后再问马上就好（Book.calibSettled 自己的测试在浏览器里）。

import assert from 'node:assert/strict';
import test from 'node:test';
import { calibGate } from '../../mobile/src/app/export_calib_wait.ts';

const flush = async () => {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
};

function fakeBook() {
  const book = {
    loadingDefault: true,
    pastDeadline: false,
    calls: [],
    waits: [],
    calibSettled(ms) {
      book.calls.push(ms);
      if (book.pastDeadline || !book.loadingDefault) return Promise.resolve();
      let resolve;
      const promise = new Promise((done) => {
        resolve = done;
      });
      book.waits.push(resolve);
      return promise;
    },
  };
  return book;
}

/** 照 startQuick：一张照片，已经有在做的批次就不再开始（filterQuickPhotos 的去重）。 */
function exporter() {
  const state = { started: [], skipped: 0, order: [] };
  const tap = (n) => () => {
    state.order.push(n);
    if (state.started.includes('p')) state.skipped += 1;
    else state.started.push('p');
  };
  return { state, tap };
}

test('not loading: the export starts at once and nothing waits', () => {
  const book = fakeBook();
  book.loadingDefault = false;
  const gate = calibGate(() => book, () => 1, 4000);
  const { state, tap } = exporter();
  gate(tap(1));
  assert.deepEqual(state.order, [1]);
  assert.equal(book.calls.length, 0);
});

test('taps during the wait share one wait and start in order, so the dedupe still holds', async () => {
  const book = fakeBook();
  const gate = calibGate(() => book, () => 1, 4000);
  const { state, tap } = exporter();
  gate(tap(1));
  gate(tap(2));
  gate(tap(3));
  await flush();
  assert.deepEqual(state.order, [], 'nothing starts before the default settles');
  assert.deepEqual(book.calls, [4000], 'one wait for all three taps');
  book.loadingDefault = false;
  book.waits[0]();
  await flush();
  assert.deepEqual(state.order, [1, 2, 3]);
  assert.deepEqual(state.started, ['p']);
  assert.equal(state.skipped, 2);
  assert.deepEqual(book.calls, [4000], 'no second wait after the first tap starts');
});

test('after a timeout the queued taps start together, later taps do not wait again, and the dedupe holds', async () => {
  const book = fakeBook();
  const gate = calibGate(() => book, () => 1, 4000);
  const { state, tap } = exporter();
  gate(tap(1));
  gate(tap(2));
  await flush();
  // 到时了，默认校准还在下载：排着的两次一起开始，只等过一次。
  book.pastDeadline = true;
  book.waits[0]();
  await flush();
  assert.deepEqual(state.order, [1, 2]);
  assert.deepEqual(book.calls, [4000]);
  assert.equal(book.waits.length, 1);
  // 再点两次（还在下载）：不再等满 4 秒，马上按先后开始，照样去重。
  gate(tap(3));
  gate(tap(4));
  await flush();
  assert.deepEqual(state.order, [1, 2, 3, 4]);
  assert.deepEqual(state.started, ['p']);
  assert.equal(state.skipped, 3);
  assert.equal(book.waits.length, 1, 'no new pending wait after the deadline');
});

test('clearing the photos during the wait drops the earlier taps; a new tap joins the same wait', async () => {
  const book = fakeBook();
  let session = 1;
  const gate = calibGate(() => book, () => session, 4000);
  const { state, tap } = exporter();
  gate(tap(1));
  session = 2;
  gate(tap(2));
  await flush();
  assert.deepEqual(book.calls, [4000], 'the new session does not start a second wait');
  book.loadingDefault = false;
  book.waits[0]();
  await flush();
  assert.deepEqual(state.order, [2]);
  assert.deepEqual(book.calls, [4000], 'the dropped tap does not leave a second wait behind');
});

test('an error from one start is reported and does not stop the next queued tap', async () => {
  const book = fakeBook();
  const reported = [];
  const before = globalThis.reportError;
  globalThis.reportError = (err) => reported.push(err.message);
  try {
    const gate = calibGate(() => book, () => 1, 4000);
    const order = [];
    gate(() => {
      order.push(1);
      throw new Error('boom');
    });
    gate(() => order.push(2));
    book.loadingDefault = false;
    book.waits[0]();
    await flush();
    assert.deepEqual(order, [1, 2]);
    assert.deepEqual(reported, ['boom']);
  } finally {
    globalThis.reportError = before;
  }
});
