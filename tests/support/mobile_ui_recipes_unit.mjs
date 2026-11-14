// UI-M2 的纯函数：校准数据 zip 的读法、校准包核对、镜头单元和 ISO 模型的选择、配方列表。
// 用 Node 自带的测试跑。Python 那边（tests/test_mobile_ui_unit.py）生成合成的包，路径从环境变量传进来。

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { checkPack, crossed, modelIso, TEXT, uncalibratedReason, unitLine, unitOfLens } from '../../mobile/src/lib/calib.ts';
import { mergeMeta, photoMetaFromJpeg, photoMetaFromTiff } from '../../mobile/src/lib/exif.ts';
import { classify } from '../../mobile/src/lib/group.ts';
import { bakeParams, BASE_NAMES, cardTag, levelsFor, listing, namesOnly, shelf } from '../../mobile/src/lib/recipes.ts';
import { crc32, plainName, readStoredZip } from '../../mobile/src/lib/zip.ts';

const enc = new TextEncoder();

function u16(v) {
  return [v & 255, (v >> 8) & 255];
}

function u32(v) {
  return [v & 255, (v >>> 8) & 255, (v >>> 16) & 255, (v >>> 24) & 255];
}

/** 最小的 stored zip。`opts` 用来做坏数据：改方法、改 CRC、加 ZIP64 标记。 */
function storedZip(files, opts = {}) {
  const locals = [];
  const centrals = [];
  let offset = 0;
  for (const [name, data] of files) {
    const nameBytes = enc.encode(name);
    const crc = opts.badCrc === name ? (crc32(data) ^ 1) >>> 0 : crc32(data);
    const method = opts.method?.[name] ?? 0;
    const size = opts.zip64 === name ? 0xffffffff : data.length;
    const local = [...u32(0x04034b50), ...u16(20), ...u16(0), ...u16(method), ...u16(0), ...u16(0), ...u32(crc), ...u32(size), ...u32(size), ...u16(nameBytes.length), ...u16(0), ...nameBytes, ...data];
    const central = [
      ...u32(0x02014b50), ...u16(20), ...u16(20), ...u16(0), ...u16(method), ...u16(0), ...u16(0), ...u32(crc), ...u32(size), ...u32(size),
      ...u16(nameBytes.length), ...u16(0), ...u16(0), ...u16(0), ...u16(0), ...u32(0), ...u32(offset), ...nameBytes,
    ];
    locals.push(...local);
    centrals.push(...central);
    offset += local.length;
  }
  const end = [...u32(0x06054b50), ...u16(0), ...u16(0), ...u16(files.length), ...u16(files.length), ...u32(centrals.length), ...u32(offset), ...u16(0)];
  return new Uint8Array([...locals, ...centrals, ...end]);
}

test('crc32 matches zlib', () => {
  assert.equal(crc32(enc.encode('123456789')), 0xcbf43926);
  assert.equal(crc32(new Uint8Array()), 0);
});

test('stored zip reads entries and rejects bad data without throwing', () => {
  const a = enc.encode('{"x":1}');
  const b = new Uint8Array([1, 2, 3, 4, 5]);
  const ok = readStoredZip(storedZip([['manifest.json', a], ['ctx.npz', b]]));
  assert.equal(ok.ok, true);
  assert.deepEqual([...ok.entries.keys()], ['manifest.json', 'ctx.npz']);
  assert.deepEqual([...ok.entries.get('ctx.npz')], [...b]);
  const cases = [
    [new Uint8Array(), 'not_zip'],
    [enc.encode('PK not really a zip file at all, just text'), 'not_zip'],
    [storedZip([['a.bin', b]], { method: { 'a.bin': 8 } }), 'compressed'],
    [storedZip([['a.bin', b]], { badCrc: 'a.bin' }), 'broken'],
    [storedZip([['a.bin', b]], { zip64: 'a.bin' }), 'broken'],
    [storedZip([['../a.bin', b]]), 'broken'],
    [storedZip([['dir/a.bin', b]]), 'broken'],
    [storedZip([['a.bin', b], ['a.bin', b]]), 'broken'],
  ];
  for (const [bytes, reason] of cases) {
    const out = readStoredZip(bytes);
    assert.equal(out.ok, false);
    assert.equal(out.reason, reason, out.detail);
  }
  // 截断在任何位置都只给原因，不抛。
  const whole = storedZip([['manifest.json', a], ['ctx.npz', b]]);
  for (let cut = 0; cut < whole.length; cut += 3) assert.equal(readStoredZip(whole.subarray(0, cut)).ok, false);
  // 改坏任何一个字节也不抛（结果可能是坏，也可能还读得出）。
  for (let at = 0; at < whole.length; at += 1) {
    const broken = whole.slice();
    broken[at] ^= 0xff;
    readStoredZip(broken);
  }
  assert.equal(readStoredZip(whole, { bytes: 10, entries: 64, entry: 64 }).reason, 'too_big');
  assert.equal(readStoredZip(whole, { bytes: 1 << 20, entries: 1, entry: 64 }).reason, 'too_big');
  assert.equal(readStoredZip(whole, { bytes: 1 << 20, entries: 64, entry: 4 }).reason, 'too_big');
  assert.equal(plainName('ctx_jlaunch_200.npz'), true);
  for (const name of ['', '.', '..', 'a/b', 'a\\b', 'x\u0000y']) assert.equal(plainName(name), false);
});

test('the zip the desktop writes, and the same pack compressed', async () => {
  const stored = process.env.GXR_M2_STORED_ZIP;
  const deflated = process.env.GXR_M2_DEFLATED_ZIP;
  if (!stored || !deflated) return;
  const zip = readStoredZip(new Uint8Array(readFileSync(stored)));
  assert.equal(zip.ok, true);
  const pack = await checkPack(zip.entries);
  assert.equal(pack.ok, true, JSON.stringify(pack));
  assert.deepEqual(pack.units.map((u) => u.unit), ['jlaunch', 'qlaunch', 'l08firm']);
  assert.deepEqual(Object.keys(pack.units[0].models), ['200', '1600']);
  const squeezed = readStoredZip(new Uint8Array(readFileSync(deflated)));
  assert.equal(squeezed.ok, false);
  assert.equal(squeezed.reason, 'compressed');
});

test('pack check uses the desktop wording for each failure', async () => {
  const ctx = new Uint8Array([9, 9, 9]);
  const ring = enc.encode('{"schema":"gxr-sector-model/2"}');
  const sha = async (bytes) => [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map((x) => x.toString(16).padStart(2, '0')).join('');
  const models = { 200: { ctx: 'c200.npz', ring: 'r200.json' }, 1600: { ctx: 'c1600.npz', ring: 'r1600.json' } };
  const listed = {};
  for (const name of ['c200.npz', 'c1600.npz']) listed[name] = { size: ctx.length, sha256: await sha(ctx) };
  for (const name of ['r200.json', 'r1600.json']) listed[name] = { size: ring.length, sha256: await sha(ring) };
  const manifest = { schema: 'gxr-studio/calib/2', units: { qlaunch: { camera_stock_sha256: 'ab'.repeat(32), models } }, files: listed };
  const files = (m, extra = {}) => {
    const map = new Map([['manifest.json', enc.encode(JSON.stringify(m))], ['c200.npz', ctx], ['c1600.npz', ctx], ['r200.json', ring], ['r1600.json', ring]]);
    for (const [k, v] of Object.entries(extra)) {
      if (v === null) map.delete(k);
      else map.set(k, v);
    }
    return map;
  };
  const good = await checkPack(files(manifest));
  assert.equal(good.ok, true);
  assert.equal(good.units[0].unit, 'qlaunch');
  const clone = () => JSON.parse(JSON.stringify(manifest));
  const bad = async (m, extra, message) => {
    const out = await checkPack(files(m, extra));
    assert.equal(out.ok, false);
    assert.equal(out.message, message);
  };
  await bad(manifest, { 'c200.npz': new Uint8Array([9, 9, 8]) }, TEXT.changed);
  await bad(manifest, { 'r1600.json': null }, TEXT.filesMissing);
  await bad({ ...clone(), schema: 'gxr-studio/calib/1' }, {}, TEXT.outdated);
  await bad({ ...clone(), units: { other: clone().units.qlaunch } }, {}, TEXT.units);
  const noBranch = clone();
  delete noBranch.units.qlaunch.models['1600'];
  await bad(noBranch, {}, TEXT.badList);
  const outside = clone();
  outside.units.qlaunch.models['200'].ctx = 'not-listed.npz';
  await bad(outside, {}, TEXT.badList);
  const escape = clone();
  escape.files['../x'] = { size: 1, sha256: '0'.repeat(64) };
  await bad(escape, {}, TEXT.badList);
  await bad(manifest, { 'manifest.json': new Uint8Array([0xff, 0xfe]) }, TEXT.unreadable);
  await bad(manifest, { 'manifest.json': null }, TEXT.missing);
});

test('lens unit and ISO model follow unit_of_lens and model_for', () => {
  assert.equal(unitOfLens('GR LENS A12 50mm F2.5 MACRO'), 'jlaunch');
  assert.equal(unitOfLens('GR LENS A12 28mm F2.5'), 'qlaunch');
  assert.equal(unitOfLens('GXR MOUNT A12'), 'l08firm');
  assert.equal(unitOfLens('A12 MOUNT 50mm'), 'l08firm');
  assert.equal(unitOfLens('RICOH LENS S10 24-72mm'), null);
  assert.equal(unitOfLens(null), null);
  assert.equal(modelIso(null), 200);
  assert.equal(modelIso(200), 200);
  assert.equal(modelIso(565), 200);
  assert.equal(modelIso(566), 1600);
  assert.equal(modelIso(3200), 1600);
  assert.equal(crossed('jlaunch', 'jlaunch', 200), false);
  assert.equal(crossed('jlaunch', 'jlaunch', 1600), false);
  assert.equal(crossed('jlaunch', 'jlaunch', 400), true);
  assert.equal(crossed('qlaunch', 'qlaunch', 1600), true);
  assert.equal(crossed(null, 'jlaunch', 200), true);
  assert.equal(crossed('jlaunch', 'jlaunch', null), true);
  assert.equal(crossed('qlaunch', 'jlaunch', 200), true);
  assert.match(uncalibratedReason('jlaunch', 'jlaunch', 400), /ISO 400，颜色校准只做了 ISO 200 和 ISO 1600/);
  assert.match(uncalibratedReason(null, 'jlaunch', 200), /认不出这张照片用的镜头单元/);
  assert.match(uncalibratedReason('jlaunch', 'qlaunch', 200), /28mm 单元/);
  assert.equal(unitLine('jlaunch'), '50mm · ISO 200、1600');
  assert.equal(unitLine('l08firm'), 'MOUNT · ISO 200');
});

/** 小端 TIFF：IFD0 有 UniqueCameraModel，Exif IFD 有 ISO 和（可选的）镜头名。字符串都比 4 字节长，放在后面。 */
function tiff({ lens, unique, iso }) {
  const exifCount = lens === null ? 1 : 2;
  const exifAt = 8 + 2 + 2 * 12 + 4;
  let tail = exifAt + 2 + exifCount * 12 + 4;
  const strings = [];
  const put = (text) => {
    const bytes = [...enc.encode(text), 0];
    const at = tail;
    strings.push(...bytes);
    tail += bytes.length;
    return { at, n: bytes.length };
  };
  const uq = put(unique);
  const ifd0 = [...u16(0xc614), ...u16(2), ...u32(uq.n), ...u32(uq.at), ...u16(0x8769), ...u16(4), ...u32(1), ...u32(exifAt)];
  const exif = [...u16(0x8827), ...u16(3), ...u32(1), ...u16(iso), 0, 0];
  if (lens !== null) {
    const ln = put(lens);
    exif.push(...u16(0xa434), ...u16(2), ...u32(ln.n), ...u32(ln.at));
  }
  return new Uint8Array([0x49, 0x49, 42, 0, ...u32(8), ...u16(2), ...ifd0, ...u32(0), ...u16(exifCount), ...exif, ...u32(0), ...strings]);
}

test('lens and ISO from EXIF, with the DNG fallback the desktop uses', () => {
  const dng = tiff({ lens: null, unique: 'RICOH GXR MOUNT A12', iso: 1600 });
  assert.deepEqual(photoMetaFromTiff(dng), { lens: 'GXR MOUNT A12', iso: 1600 });
  const named = tiff({ lens: 'GR LENS A12 50mm F2.5 MACRO', unique: 'RICOH GXR A12', iso: 0 });
  assert.deepEqual(photoMetaFromTiff(named), { lens: 'GR LENS A12 50mm F2.5 MACRO', iso: null });
  const body = tiff({ lens: 'GR LENS A12 28mm F2.5', unique: 'RICOH GXR', iso: 400 });
  const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xe1, ...[(body.length + 8) >> 8, (body.length + 8) & 255], ...enc.encode('Exif'), 0, 0, ...body, 0xff, 0xd9]);
  // JPG 没有 UniqueCameraModel 这条退路。
  assert.deepEqual(photoMetaFromJpeg(jpeg), { lens: 'GR LENS A12 28mm F2.5', iso: 400 });
  assert.deepEqual(photoMetaFromJpeg(new Uint8Array([1, 2, 3])), { lens: null, iso: null });
  assert.deepEqual(mergeMeta({ lens: null, iso: 200 }, { lens: 'GXR MOUNT A12', iso: 1600 }), { lens: 'GXR MOUNT A12', iso: 200 });
  assert.deepEqual(mergeMeta(null, null), { lens: null, iso: null });
});

test('several GXRCE.DAT: the newest by lastModified is used', () => {
  const out = classify([
    { name: 'GXRCE.DAT', lastModified: 5 },
    { name: 'R1.DNG', lastModified: 1 },
    { name: 'gxrce.dat', lastModified: 9 },
    { name: 'GXRCE.DAT', lastModified: 9 },
  ]);
  assert.equal(out.datIndex, 2);
  assert.equal(out.datCount, 3);
  assert.equal(out.ignored, 0);
});

function menu() {
  const controls = ['amount', 'contrast', 'highlight', 'shadow', 'saturation', 'fade'].map((name) => ({ name, label: name }));
  const items = [];
  const recipes = [];
  for (let unit = 0; unit < 3; unit += 1) {
    for (let slot = 1; slot <= 8; slot += 1) {
      const names = slot === 3 ? [1, 2, 3, 5] : slot === 4 ? [1, 2, 3, 4] : [0, 1, 2, 3, 4];
      recipes.push({ unit, slot, first_item: items.length, item_count: names.length });
      for (const control of names) items.push({ control, min_level: -4, max_level: 4, default_level: control === 0 ? 100 : 0, points: [] });
    }
  }
  return { controls, items, recipes };
}

function summary() {
  const units = ['jlaunch', 'qlaunch', 'l08firm'].map((unit, unit_index) => ({
    unit,
    unit_index,
    entries: Array.from({ length: 8 }, (_, i) => ({ slot: i + 1, sha256: `${unit_index}${i + 1}`.padEnd(64, '0') })),
  }));
  return {
    generation: 158,
    units,
    looks: [
      { name: 'Acros', base: 3, unit_mask: 0b111, sha256: 'a'.repeat(64) },
      { name: 'Velvia 50', base: 1, unit_mask: 0b010, sha256: 'b'.repeat(64) },
    ],
    recipes: [],
    users: [
      { name: 'MY BW', base: 3, levels: [2, 0, 0, 3] },
      { name: 'MY POP', base: 1, levels: [1, -1, 0, 2, 0, 0] },
    ],
  };
}

test('recipe listing follows the desktop order, names and knob rules', () => {
  const list = listing(summary(), menu(), 'jlaunch');
  assert.deepEqual(list.builtin.map((c) => c.name), BASE_NAMES);
  assert.deepEqual(list.builtin.map((c) => c.desc).slice(0, 4), ['饱和度高', '饱和度略低', '黑白，对比高', '原厂标准色彩']);
  assert.equal(list.builtin[0].entrySha256, '01'.padEnd(64, '0'));
  assert.deepEqual(list.builtin[0].levels, [100, 0, 0, 0, 0]);
  assert.deepEqual(list.builtin[3].levels, [0, 0, 0, 0]);
  // jlaunch 只拿到对这个单元开放的 Looks；文件里的条目字节手机上拿不到，先标成还画不了。
  assert.deepEqual(list.looks.map((c) => [c.name, c.desc, c.pending]), [['Acros', '黑白', true]]);
  assert.deepEqual(listing(summary(), menu(), 'qlaunch').looks.map((c) => c.name), ['Acros', 'Velvia 50']);
  // 用户配方：黑白底子没有饱和度旋钮，饱和度不是 0 就用不了（桌面 knob_not_available）。
  assert.equal(list.users[0].levels, null);
  assert.equal(list.users[0].error, '无法保存配方。有一个旋钮无法使用。');
  assert.deepEqual(list.users[1].levels, [100, 1, -1, 0, 2]);
  assert.equal(list.users[1].entrySha256, list.builtin[0].entrySha256);
  assert.equal(list.users[1].desc, '相机里的配方');
  assert.equal(shelf(list, 'all').length, 8 + 1 + 2);
  assert.equal(shelf(list, 'mine').length, 2);
  assert.equal(cardTag(list.builtin[6]), '相机里 · 07');
  assert.equal(JSON.parse(bakeParams(list.builtin[0], 'qlaunch')).knobs.unit, 1);
  assert.deepEqual(JSON.parse(bakeParams(list.users[1], 'jlaunch')), { step: 0.375, knobs: { levels: [100, 1, -1, 0, 2], unit: 0, slot: 1 } });
  assert.equal(bakeParams(list.users[0], 'jlaunch'), null);
  assert.deepEqual(levelsFor(menu(), 0, 4, { amount: 100 }), { levels: [0, 0, 0, 0] });
  assert.deepEqual(levelsFor(menu(), 0, 4, { amount: 50 }), { error: '无法保存配方。有一个旋钮无法使用。' });
  assert.deepEqual(namesOnly().map((c) => c.name), BASE_NAMES);
});
