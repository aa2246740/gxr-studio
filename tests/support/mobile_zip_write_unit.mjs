// 电脑上一批下载打成的 ZIP（mobile/src/lib/zip_write.ts）：用 lib/zip.ts 的读法读回来（只在这里放宽它的上限），
// 再交给 /usr/bin/unzip -t 和 macOS 的 ditto -x -k。全是合成数据，不读照片。临时文件写到 TMPDIR（外置盘）。

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';

const zw = await import('../../mobile/src/lib/zip_write.ts');
const zr = await import('../../mobile/src/lib/zip.ts');

const WIDE = { bytes: 1 << 30, entries: 70_000, entry: 1 << 30 };

function bytesOf(n, seed) {
  const out = new Uint8Array(n);
  let x = seed >>> 0 || 1;
  for (let i = 0; i < n; i++) {
    x ^= x << 13;
    x ^= x >>> 17;
    x ^= x << 5;
    out[i] = x & 0xff;
  }
  return out;
}

function sample() {
  return [
    new File([bytesOf(70_001, 1)], 'R0012345_gxr.jpg', { type: 'image/jpeg' }),
    new File([bytesOf(3, 2)], '街头 夜·一_gxr.jpg', { type: 'image/jpeg' }),
    new File([new Uint8Array(0)], 'empty_gxr.jpg', { type: 'image/jpeg' }),
    new File([bytesOf(1_048_583, 3)], 'R0012346_gxr_half.jpg', { type: 'image/jpeg' }),
  ];
}

async function track(files) {
  for (const f of files) zw.trackCrc(f, new Uint8Array(await f.arrayBuffer()));
}

async function zipBytes(files, when) {
  await track(files);
  const zip = zw.zipOf(files, when);
  assert.ok(zip, 'zipOf builds a zip for 2+ tracked files');
  return { name: zip.name, bytes: new Uint8Array(await zip.blob.arrayBuffer()), type: zip.blob.type };
}

const sha = (b) => createHash('sha256').update(b).digest('hex');

test('crc32 equals the reader crc32 (zlib) on odd lengths and known vectors', () => {
  assert.equal(zw.crc32(new TextEncoder().encode('123456789')), 0xcbf43926);
  assert.equal(zw.crc32(new Uint8Array(0)), 0);
  for (const n of [1, 7, 8, 9, 63, 4097, 100_003]) {
    const b = bytesOf(n, n);
    assert.equal(zw.crc32(b), zr.crc32(b), `length ${n}`);
  }
});

test('a stored zip reads back with lib/zip.ts: names (UTF-8), bytes and CRC are the originals', async () => {
  const files = sample();
  const { bytes, name, type } = await zipBytes(files, new Date(2026, 10, 26, 23, 40, 5));
  assert.equal(name, 'GXR-export-20261126-2340-4.zip');
  assert.match(name, /^[\x20-\x7e]+$/);
  assert.equal(type, 'application/zip');
  const read = zr.readStoredZip(bytes, WIDE);
  assert.equal(read.ok, true, read.ok ? '' : `${read.reason} ${read.detail}`);
  assert.deepEqual([...read.entries.keys()], files.map((f) => f.name));
  for (const f of files) {
    const want = new Uint8Array(await f.arrayBuffer());
    const got = read.entries.get(f.name);
    assert.equal(sha(got), sha(want), f.name);
    assert.equal(zr.crc32(got), zw.crcOf(f));
  }
  // 每个条目：方法 0（stored），通用标志第 11 位（UTF-8 名字）。
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  assert.equal(view.getUint32(0, true), 0x04034b50);
  assert.equal(view.getUint16(6, true), 0x0800);
  assert.equal(view.getUint16(8, true), 0);
  // 文件就是各段拼起来的：总长 = 每个条目（30 + 名字 + 数据）+ 中央目录（46 + 名字）+ 22。
  const enc = new TextEncoder();
  const expect = files.reduce((s, f) => s + 30 + 46 + 2 * enc.encode(f.name).length + f.size, 22);
  assert.equal(bytes.length, expect);
});

test('a CRC that does not match the bytes is caught by the reader (the check is real)', async () => {
  const files = sample().slice(0, 2);
  await track(files);
  const blob = zw.zipBlob(files.map((f) => ({ name: f.name, file: f, crc: (zw.crcOf(f) ^ 1) >>> 0 })));
  const read = zr.readStoredZip(new Uint8Array(await blob.arrayBuffer()), WIDE);
  assert.equal(read.ok, false);
  assert.match(read.detail, /^crc /);
});

test('an empty batch is refused; one file or an untracked file gives no zip', async () => {
  assert.throws(() => zw.zipBlob([]), RangeError);
  const one = sample().slice(0, 1);
  await track(one);
  assert.equal(zw.zipOf(one), null);
  assert.equal(zw.zipOf([]), null);
  const untracked = [new File([bytesOf(5, 9)], 'a.jpg'), new File([bytesOf(5, 10)], 'b.jpg')];
  assert.equal(zw.zipOf(untracked), null);
});

test('crcOf finishes a pending CRC synchronously; the background slices finish it too', async () => {
  const big = new File([bytesOf(5 << 20, 11)], 'big.jpg');
  const raw = new Uint8Array(await big.arrayBuffer());
  zw.trackCrc(big, raw);
  assert.equal(zw.crcOf(big), zr.crc32(raw));
  const later = new File([bytesOf(3 << 20, 12)], 'later.jpg');
  const raw2 = new Uint8Array(await later.arrayBuffer());
  zw.trackCrc(later, raw2);
  await new Promise((r) => setTimeout(r, 200));
  const before = zw.crcStats.finishedAtClick;
  assert.equal(zw.crcOf(later), zr.crc32(raw2));
  assert.equal(zw.crcStats.finishedAtClick, before, 'already done in the background');
  // 一片的时间有上限（这里只确认量到了；浏览器测试量真实的）。
  assert.ok(zw.crcStats.maxSliceMs < 50, `slice ${zw.crcStats.maxSliceMs} ms`);
  // 字节长度和文件对不上：不记，之后也不会给 ZIP。
  const wrong = new File([bytesOf(4, 13)], 'w.jpg');
  zw.trackCrc(wrong, bytesOf(5, 13));
  assert.equal(zw.crcOf(wrong), null);
});

test('duplicate names get a browser-style suffix; unique names stay as they are', () => {
  assert.deepEqual(zw.uniqueNames(['a.jpg', 'b.jpg']), ['a.jpg', 'b.jpg']);
  assert.deepEqual(zw.uniqueNames(['a.jpg', 'a.jpg', 'a (2).jpg', 'a.jpg']), ['a.jpg', 'a (3).jpg', 'a (2).jpg', 'a (4).jpg']);
});

test('parts: 65,000 entries or 3.5 GB per zip at most, in order, never empty', () => {
  assert.deepEqual(zw.zipParts([]), []);
  assert.deepEqual(zw.zipParts([10, 20, 30]), [[0, 1, 2]]);
  const many = zw.zipParts(new Array(65_001).fill(1));
  assert.deepEqual(many.map((p) => p.length), [65_000, 1]);
  const gb = 1_000_000_000;
  const parts = zw.zipParts([gb, gb, gb, gb, gb]);
  assert.deepEqual(parts, [[0, 1, 2], [3, 4]]);
  assert.deepEqual(zw.zipParts([1, 2, 3, 4, 5], 2), [[0, 1], [2, 3], [4]]);
  assert.ok(zw.ZIP_MAX_BYTES < 2 ** 32 && zw.ZIP_MAX_ENTRIES < 0xffff);
});

test('/usr/bin/unzip -t passes and macOS ditto -x -k unpacks the same bytes', { skip: !existsSync('/usr/bin/unzip') }, async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'gxr-zip-'));
  try {
    const files = sample();
    const { bytes } = await zipBytes(files);
    const zipPath = path.join(dir, 'batch.zip');
    writeFileSync(zipPath, bytes);
    const test = execFileSync('/usr/bin/unzip', ['-t', zipPath], { encoding: 'utf8' });
    assert.match(test, /No errors detected/);
    const unzipDir = path.join(dir, 'unzip');
    execFileSync('/usr/bin/unzip', ['-q', zipPath, '-d', unzipDir]);
    const unzipNames = readdirSync(unzipDir).map((n) => n.normalize('NFC')).sort();
    assert.deepEqual(unzipNames, files.map((f) => f.name.normalize('NFC')).sort());
    if (existsSync('/usr/bin/ditto')) {
      const dittoDir = path.join(dir, 'ditto');
      execFileSync('/usr/bin/ditto', ['-x', '-k', zipPath, dittoDir]);
      for (const f of files) {
        const want = sha(new Uint8Array(await f.arrayBuffer()));
        const disk = readdirSync(dittoDir).find((n) => n.normalize('NFC') === f.name.normalize('NFC'));
        assert.ok(disk, `ditto wrote ${f.name}`);
        assert.equal(sha(readFileSync(path.join(dittoDir, disk))), want);
        const viaUnzip = readdirSync(unzipDir).find((n) => n.normalize('NFC') === f.name.normalize('NFC'));
        assert.equal(sha(readFileSync(path.join(unzipDir, viaUnzip))), want);
      }
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
