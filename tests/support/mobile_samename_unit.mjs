// 同名照片（机主 2026-11-28）的 Node 单测：new_photo.ts 的 Intake（替换还是另外留下）、按文件夹和拍摄时间配 RAW+JPG、
// 指纹（大小加头尾各 64 KiB，不看修改时间）、名字「名字 (2)」和导出文件名、半格第 1 张按文件夹 / 拍摄时间挑。
// 不读照片：文件是内存里造的小 TIFF / JPEG，只带一个 DateTime。

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

const { Intake, EDGE, gather, keptNote, newPhoto, printOf } = await import('../../mobile/src/app/new_photo.ts');
const { copyFileName, nameOf, photoName } = await import('../../mobile/src/app/model.ts');
const { findFirstHalfNeighbor } = await import('../../mobile/src/lib/halfframe.ts');

/** 只有 IFD0 一项 DateTime (0x0132) 的小端 TIFF。`fill` 个字节的填充让文件大过头尾两段。 */
function tiff(when, fill = 0, seed = 0) {
  const text = Buffer.from(`${when}\0`, 'latin1');
  const head = Buffer.alloc(8 + 2 + 12 + 4);
  head.write('II', 0, 'latin1');
  head.writeUInt16LE(42, 2);
  head.writeUInt32LE(8, 4);
  head.writeUInt16LE(1, 8);
  head.writeUInt16LE(0x0132, 10);
  head.writeUInt16LE(2, 12);
  head.writeUInt32LE(text.length, 14);
  head.writeUInt32LE(head.length, 18);
  head.writeUInt32LE(0, 22);
  const pad = Buffer.alloc(fill, seed & 255);
  return Buffer.concat([head, text, pad]);
}

/** 带同一个 TIFF 的 APP1 Exif 的 JPEG（只有头，够读拍摄时间）。 */
function jpeg(when, fill = 0, seed = 0) {
  const body = Buffer.concat([Buffer.from('Exif\0\0', 'latin1'), tiff(when)]);
  const app1 = Buffer.alloc(4);
  app1.writeUInt16BE(0xffe1, 0);
  app1.writeUInt16BE(body.length + 2, 2);
  return Buffer.concat([Buffer.from([0xff, 0xd8]), app1, body, Buffer.from([0xff, 0xd9]), Buffer.alloc(fill, seed & 255)]);
}

function file(name, bytes, { folder = '', mtime = 1_700_000_000_000 } = {}) {
  const out = new File([bytes], name, { lastModified: mtime });
  if (folder) Object.defineProperty(out, 'webkitRelativePath', { value: `${folder}/${name}` });
  return out;
}

/** 读不了的文件（卡拔了、被删了）：slice 出来的东西 arrayBuffer 会失败。 */
function goneFile(name, bytes) {
  const out = new File([bytes], name);
  out.slice = () => ({ arrayBuffer: () => Promise.reject(new DOMException('gone', 'NotReadableError')) });
  return out;
}

const T1 = '2026:11:20 10:00:01';
const T2 = '2026:11:20 10:05:09';

function host() {
  const photos = new Map();
  const busy = new Set();
  const reads = [];
  let session = 1;
  let settled = [];
  let wake = null;
  const h = {
    photos,
    busy: (photo) => busy.has(photo.id),
    session: () => session,
    read: (photo, s) => reads.push({ id: photo.id, version: photo.version, session: s }),
    settled: (kept) => {
      settled.push(kept);
      wake?.();
    },
  };
  const intake = new Intake(h);
  return {
    photos,
    busy,
    reads,
    intake,
    settled: () => settled,
    clear: () => {
      session += 1;
      photos.clear();
    },
    /** 等这一次排在后面的比对做完（settled 被调一次）。 */
    idle: () => new Promise((done) => {
      const before = settled.length;
      const poll = () => (settled.length > before ? done() : setTimeout(poll, 5));
      wake = null;
      poll();
    }),
    reset: () => {
      settled = [];
    },
  };
}

/** 读好了的样子（缩略图好了、没拔卡、拍摄时间知道）。 */
function ready(photo, ms = null) {
  photo.status = 'ready';
  photo.captureMs = ms;
}

const ids = (photos) => [...photos.keys()];

test('a new name lands at once with the plain id; nothing waits', () => {
  const t = host();
  t.intake.take([file('R0001.DNG', tiff(T1)), file('R0001.JPG', jpeg(T1))]);
  assert.deepEqual(ids(t.photos), ['r0001']);
  const photo = t.photos.get('r0001');
  assert.equal(photo.version, 1);
  assert.equal(photo.copy, undefined);
  assert.ok(photo.dng && photo.jpg);
  assert.equal(t.reads.length, 1);
  assert.equal(t.intake.waiting(), 0);
});

test('an identical re-copy with a new mtime is the same copy: replaced, not kept twice', async () => {
  const t = host();
  t.intake.take([file('R0001.DNG', tiff(T1, 300_000, 7), { mtime: 1 })]);
  const photo = t.photos.get('r0001');
  ready(photo);
  photo.knobs.exposure_ev = 0.7;
  const again = file('R0001.DNG', tiff(T1, 300_000, 7), { mtime: 999_999 });
  t.intake.take([again]);
  assert.equal(t.intake.waiting(), 1, 'a healthy photo with the same name waits for the comparison');
  await t.idle();
  assert.deepEqual(ids(t.photos), ['r0001']);
  assert.equal(photo.dng, again);
  assert.equal(photo.version, 2);
  assert.equal(photo.knobs.exposure_ev, 0.7, 'edits stay on a replaced photo');
  assert.deepEqual(t.settled(), [0]);
  assert.equal(t.intake.waiting(), 0);
});

test('a different file with the same name next to a healthy photo is kept as 「名字 (2)」', async () => {
  const t = host();
  t.intake.take([file('R0001.JPG', jpeg(T1, 1000, 1))]);
  const first = t.photos.get('r0001');
  ready(first);
  t.intake.take([file('R0001.JPG', jpeg(T2, 1000, 2))]);
  await t.idle();
  assert.equal(t.photos.size, 2);
  const second = [...t.photos.values()].find((p) => p !== first);
  assert.equal(first.version, 1, 'the first copy is untouched');
  assert.equal(second.copy, 2);
  assert.match(second.id, /^r0001~[0-9a-z]+-[0-9a-z]{14}$/);
  assert.equal(photoName(second), 'R0001 (2)');
  assert.equal(photoName(first), 'R0001');
  assert.deepEqual(t.settled(), [1]);
  assert.equal(keptNote(1), '有 1 张同名照片另外留下了');
  assert.equal(keptNote(3), '有 3 张同名照片另外留下了');
});

test('the kept copy id comes from the file: the same bytes give the same id in a new session', async () => {
  const make = async () => {
    const t = host();
    t.intake.take([file('R0001.JPG', jpeg(T1, 1000, 1))]);
    ready(t.photos.get('r0001'));
    t.intake.take([file('R0001.JPG', jpeg(T2, 1000, 2), { mtime: Date.now() })]);
    await t.idle();
    return ids(t.photos);
  };
  assert.deepEqual(await make(), await make());
});

test('pending, bad, gone, uploading or unreadable originals are replaced as before', async () => {
  const cases = {
    pending: (p) => { p.status = 'pending'; },
    bad: (p) => { p.status = 'bad'; },
    gone: (p) => { ready(p); p.gone = true; },
    uploading: (p, t) => { ready(p); t.busy.add(p.id); },
  };
  for (const [label, spoil] of Object.entries(cases)) {
    const t = host();
    t.intake.take([file('R0001.JPG', jpeg(T1, 10, 1))]);
    const photo = t.photos.get('r0001');
    spoil(photo, t);
    const next = file('R0001.JPG', jpeg(T2, 10, 2));
    t.intake.take([next]);
    assert.equal(t.intake.waiting(), 0, `${label}: replaced at once, same timing as before`);
    assert.deepEqual(ids(t.photos), ['r0001'], label);
    assert.equal(photo.jpg, next, label);
    assert.equal(photo.version, 2, label);
  }
  // 状态看着好，文件却读不了了（被删、被挪走）：比对时试读失败，也照旧替换。
  const t = host();
  t.intake.take([goneFile('R0001.JPG', jpeg(T1, 10, 1))]);
  const photo = t.photos.get('r0001');
  ready(photo);
  const next = file('R0001.JPG', jpeg(T1, 10, 1));
  t.intake.take([next]);
  await t.idle();
  assert.deepEqual(ids(t.photos), ['r0001']);
  assert.equal(photo.jpg, next);
  assert.deepEqual(t.settled(), [0]);
});

test('two folders in one pick: both stay, each RAW pairs with the JPG in its own folder', async () => {
  const t = host();
  const a = { dng: file('R0001.DNG', tiff(T1, 10, 1), { folder: 'card/A' }), jpg: file('R0001.JPG', jpeg(T1, 10, 1), { folder: 'card/A' }) };
  const b = { dng: file('R0001.DNG', tiff(T2, 10, 2), { folder: 'card/B' }), jpg: file('R0001.JPG', jpeg(T2, 10, 2), { folder: 'card/B' }) };
  // 交错着给：先 A 的 RAW，再 B 的两个，最后 A 的 JPG。
  t.intake.take([a.dng, b.dng, b.jpg, a.jpg]);
  await t.idle();
  const all = [...t.photos.values()];
  assert.equal(all.length, 2);
  const first = all.find((p) => !p.copy);
  const second = all.find((p) => p.copy === 2);
  assert.equal(first.dng, a.dng);
  assert.equal(first.jpg, a.jpg);
  assert.equal(first.folder, 'card/A');
  assert.equal(second.dng, b.dng);
  assert.equal(second.jpg, b.jpg);
  assert.equal(second.folder, 'card/B');
  assert.deepEqual(t.settled(), [1]);
});

test('the same file in two folders is one photo, not two', async () => {
  const t = host();
  t.intake.take([file('R0001.JPG', jpeg(T1, 10, 1), { folder: 'x/A' }), file('R0001.JPG', jpeg(T1, 10, 1), { folder: 'x/B' })]);
  await t.idle();
  assert.equal(t.photos.size, 1);
  assert.deepEqual(t.settled(), [0]);
});

test('without folders, same-name RAWs and JPGs in one pick pair by capture time', async () => {
  const t = host();
  const d1 = file('R0001.DNG', tiff(T1, 10, 1));
  const d2 = file('R0001.DNG', tiff(T2, 10, 2));
  const j1 = file('R0001.JPG', jpeg(T1, 10, 3));
  const j2 = file('R0001.JPG', jpeg(T2, 10, 4));
  const { items, buckets } = gather([d1, j2, d2, j1]);
  assert.equal(items.length, 0);
  assert.equal(buckets.length, 1);
  t.intake.take([d1, j2, d2, j1]);
  await t.idle();
  const all = [...t.photos.values()];
  assert.equal(all.length, 2);
  const byDng = new Map(all.map((p) => [p.dng, p]));
  assert.equal(byDng.get(d1).jpg, j1, 'the T1 RAW pairs with the T1 JPG');
  assert.equal(byDng.get(d2).jpg, j2, 'the T2 RAW pairs with the T2 JPG');
  assert.equal(all.filter((p) => p.copy === 2).length, 1);
});

test('across picks, a RAW joins a JPG-only photo only when the capture time (or folder) matches', async () => {
  // 同一次拍的：照旧配成一对。
  let t = host();
  t.intake.take([file('R0001.JPG', jpeg(T1, 10, 1))]);
  let photo = t.photos.get('r0001');
  ready(photo, new Date(2026, 10, 20, 10, 0, 1).getTime());
  const dng = file('R0001.DNG', tiff(T1, 10, 1));
  t.intake.take([dng]);
  await t.idle();
  assert.deepEqual(ids(t.photos), ['r0001']);
  assert.equal(photo.dng, dng);
  // 不是同一次拍的：另外留下。
  t = host();
  t.intake.take([file('R0001.JPG', jpeg(T1, 10, 1))]);
  photo = t.photos.get('r0001');
  ready(photo, new Date(2026, 10, 20, 10, 0, 1).getTime());
  t.intake.take([file('R0001.DNG', tiff(T2, 10, 2))]);
  await t.idle();
  assert.equal(t.photos.size, 2);
  assert.equal(photo.dng, null);
  // RAW、JPG 分在两个文件夹里（RAW/、JPG/）：不是同一个文件夹，就按拍摄时间配；同一次拍的配上。
  t = host();
  t.intake.take([file('R0001.JPG', jpeg(T1, 10, 1), { folder: 'JPG' })]);
  photo = t.photos.get('r0001');
  ready(photo, new Date(2026, 10, 20, 10, 0, 1).getTime());
  const split = file('R0001.DNG', tiff(T1, 10, 1), { folder: 'RAW' });
  t.intake.take([split]);
  await t.idle();
  assert.deepEqual(ids(t.photos), ['r0001']);
  assert.equal(photo.dng, split);
  // 同一个文件夹的另一半：不看时间直接配。
  t = host();
  t.intake.take([file('R0001.JPG', jpeg(T1, 10, 1), { folder: 'A' })]);
  photo = t.photos.get('r0001');
  ready(photo, new Date(2026, 10, 20, 10, 0, 1).getTime());
  const sameFolder = file('R0001.DNG', tiff(T2, 10, 2), { folder: 'A' });
  t.intake.take([sameFolder]);
  await t.idle();
  assert.equal(t.photos.size, 1);
  assert.equal(photo.dng, sameFolder);
});

test('one pick with RAW/ and JPG/ subfolders pairs each shot across the two folders', async () => {
  const t = host();
  const raws = [file('R0001.DNG', tiff(T1, 10, 1), { folder: 'card/RAW' }), file('R0002.DNG', tiff(T2, 10, 2), { folder: 'card/RAW' })];
  const jpgs = [file('R0001.JPG', jpeg(T1, 10, 1), { folder: 'card/JPG' }), file('R0002.JPG', jpeg(T2, 10, 2), { folder: 'card/JPG' })];
  t.intake.take([...raws, ...jpgs]);
  await t.idle();
  assert.equal(t.photos.size, 2);
  assert.equal(t.photos.get('r0001').dng, raws[0]);
  assert.equal(t.photos.get('r0001').jpg, jpgs[0]);
  assert.equal(t.photos.get('r0002').dng, raws[1]);
  assert.equal(t.photos.get('r0002').jpg, jpgs[1]);
  assert.deepEqual(t.settled(), [0]);
});

test('re-picking both folders in reverse order maps each file back to its own copy', async () => {
  const t = host();
  const a = file('R0001.JPG', jpeg(T1, 10, 1), { folder: 'A' });
  const b = file('R0001.JPG', jpeg(T2, 10, 2), { folder: 'B' });
  t.intake.take([a, b]);
  await t.idle();
  const first = t.photos.get('r0001');
  const second = [...t.photos.values()].find((p) => p.copy === 2);
  ready(first);
  ready(second);
  first.knobs.exposure_ev = 0.5;
  second.knobs.exposure_ev = -1;
  const a2 = file('R0001.JPG', jpeg(T1, 10, 1), { mtime: 5 });
  const b2 = file('R0001.JPG', jpeg(T2, 10, 2), { mtime: 6 });
  t.intake.take([b2, a2]);
  await t.idle();
  assert.equal(t.photos.size, 2);
  assert.equal(first.jpg, a2);
  assert.equal(second.jpg, b2);
  assert.equal(first.knobs.exposure_ev, 0.5);
  assert.equal(second.knobs.exposure_ev, -1);
  assert.deepEqual(t.settled().slice(-1), [0]);
});

test('clearing the photos while a comparison waits drops it', async () => {
  const t = host();
  t.intake.take([file('R0001.JPG', jpeg(T1, 10, 1))]);
  ready(t.photos.get('r0001'));
  const before = t.reads.length;
  t.intake.take([file('R0001.JPG', jpeg(T2, 10, 2))]);
  assert.equal(t.intake.waiting(), 1);
  t.clear();
  assert.equal(t.intake.waiting(), 0);
  await new Promise((done) => setTimeout(done, 50));
  assert.equal(t.photos.size, 0);
  assert.equal(t.reads.length, before);
  assert.deepEqual(t.settled(), []);
});

test('the print reads only the head and tail, and ignores mtime', async () => {
  const big = Buffer.alloc(EDGE * 4, 9);
  const same = await printOf(file('R1.DNG', big, { mtime: 1 }));
  assert.equal(same, await printOf(file('R1.DNG', Buffer.from(big), { mtime: 2 })));
  const middle = Buffer.from(big);
  middle[EDGE * 2] = 1; // 只改中间：头尾一样、大小一样，算同一份（只读两头的代价）
  assert.equal(same, await printOf(file('R1.DNG', middle)));
  const tail = Buffer.from(big);
  tail[tail.length - 1] = 1;
  assert.notEqual(same, await printOf(file('R1.DNG', tail)));
  assert.notEqual(same, await printOf(file('R1.DNG', Buffer.alloc(EDGE * 4 + 1, 9))));
  assert.equal(await printOf(goneFile('R1.DNG', big)), null);
  let read = 0;
  const counted = file('R2.DNG', big);
  const slice = counted.slice.bind(counted);
  counted.slice = (a, b) => {
    const part = slice(a, b);
    read += part.size;
    return part;
  };
  await printOf(counted);
  assert.equal(read, EDGE * 2, 'reads 64 KiB at each end, never the whole file');
});

test('names: 「名字 (N)」 everywhere, export file names, clip sources', () => {
  assert.equal(photoName({ stem: 'R0001' }), 'R0001');
  assert.equal(photoName({ stem: 'R0001', copy: 3 }), 'R0001 (3)');
  assert.equal(copyFileName('R0001_gxr.jpg', 'R0001', 2), 'R0001 (2)_gxr.jpg');
  assert.equal(copyFileName('R0001_1_gxr_half.jpg', 'R0001_1', 2), 'R0001_1 (2)_gxr_half.jpg');
  assert.equal(copyFileName('other.jpg', 'R0001', 2), 'other (2).jpg');
  const kept = newPhoto('r0001~x', 'R0001', 0, 2);
  const api = { photo: (id) => (id === kept.id ? kept : undefined) };
  assert.equal(nameOf(api, { id: kept.id, stem: 'R0001' }), 'R0001 (2)');
  assert.equal(nameOf(api, { id: 'gone', stem: 'R0009' }), 'R0009');
});

test('half-frame first frame: same folder first, otherwise the nearest capture time', async () => {
  const jpg = (name) => new File([Buffer.from('x')], name);
  const cand = (id, folder, captureMs) => ({ id, stem: 'R0001', diptych: false, jpg: jpg('R0001.JPG'), frame: { orientation: 1 }, folder, captureMs });
  const dip = (folder, captureMs) => ({ id: 'r0002', stem: 'R0002', diptych: true, jpg: jpg('R0002.JPG'), frame: { orientation: 1 }, folder, captureMs });
  const yes = async () => true;
  const a = cand('r0001', 'A', 1000);
  const b = cand('r0001~b', 'B', 500_000);
  assert.equal(await findFirstHalfNeighbor(dip('B', 2000), [a, b], yes), b, 'same folder wins over a closer time');
  assert.equal(await findFirstHalfNeighbor(dip('C', 499_000), [a, b], yes), b, 'no same-folder candidate: the nearest capture time');
  const c = cand('r0001', '', 1000);
  const d = cand('r0001~d', '', 500_000);
  assert.equal(await findFirstHalfNeighbor(dip('', 499_000), [c, d], yes), d, 'no folders: the nearest capture time');
  assert.equal(await findFirstHalfNeighbor(dip('', 2000), [c], yes), c, 'one candidate: as before');
});
