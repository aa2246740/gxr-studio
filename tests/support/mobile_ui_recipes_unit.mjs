// UI-M2 的纯函数：校准数据 zip 的读法、校准包核对、镜头单元和 ISO 模型的选择、配方列表。
// 用 Node 自带的测试跑。Python 那边（tests/test_mobile_ui_unit.py）生成合成的包，路径从环境变量传进来。

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { checkPack, crossed, modelIso, TEXT, uncalibratedReason, unitLine, unitOfLens } from '../../mobile/src/lib/calib.ts';
import { mergeMeta, photoMetaFromJpeg, photoMetaFromTiff } from '../../mobile/src/lib/exif.ts';
import { classify } from '../../mobile/src/lib/group.ts';
import { BASE_NAMES, cardTag, choices, defaults, effective, levelText, namesOnly, nearestIndex, selection, selectionKey, shelf, shelves } from '../../mobile/src/lib/recipes.ts';
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
    // opts.local：只改本地头（中央目录保持合法），用来造两边不一致的归档。
    const lo = opts.local?.[name] ?? {};
    const localName = lo.name ? enc.encode(lo.name) : nameBytes;
    const local = [
      ...u32(0x04034b50), ...u16(20), ...u16(lo.flags ?? 0), ...u16(method), ...u16(0), ...u16(0), ...u32(lo.crc ?? crc), ...u32(lo.packed ?? size), ...u32(lo.size ?? size),
      ...u16(localName.length), ...u16(0), ...localName, ...data,
    ];
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

test('local headers that disagree with the central directory are rejected (Sol r1)', () => {
  const data = enc.encode('{"schema":"x"}');
  const conflicts = {
    name: { name: '../escape.bin' },
    size: { size: 0xffffffff },
    crc: { crc: 0 },
    packed: { packed: 100000 },
    encrypted: { flags: 1 },
    descriptor: { flags: 8 },
  };
  for (const [label, local] of Object.entries(conflicts)) {
    const out = readStoredZip(storedZip([['manifest.json', data]], { local: { 'manifest.json': local } }));
    assert.equal(out.ok, false, label);
    assert.equal(out.reason, 'broken', `${label}: ${out.detail}`);
  }
  // 一样长但不同的名字也拒绝。
  const same = readStoredZip(storedZip([['manifest.json', data]], { local: { 'manifest.json': { name: 'manifest.jsoN' } } }));
  assert.equal(same.ok, false);
  // 中央目录声明的长度比实际条目多出来，也拒绝。
  const whole = storedZip([['manifest.json', data]]);
  const end = whole.length - 22;
  const padded = whole.slice();
  padded[end + 12] += 4;
  assert.equal(readStoredZip(padded).ok, false);
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
  // 每个单元能不能用由核心的 color_calib_check 判断；这里只列出来。
  assert.deepEqual(pack.units[0], { unit: 'jlaunch' });
  const squeezed = readStoredZip(new Uint8Array(readFileSync(deflated)));
  assert.equal(squeezed.ok, false);
  assert.equal(squeezed.reason, 'compressed');
});

test('pack check covers the zip layer; model_for checks are left to the core', async () => {
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
  // 版本、单元、分支文件名这些是 model_for 的检查，交给核心的 color_calib_check（tests/test_mobile_ui_calib_wasm.py），
  // 这里不再重写：zip 这一层完整就收下，单元只用来列出来。
  const outdated = await checkPack(files({ ...clone(), schema: 'gxr-studio/calib/1' }));
  assert.equal(outdated.ok, true);
  const odd = await checkPack(files({ ...clone(), units: { other: {}, l08firm: {}, jlaunch: {} } }));
  assert.deepEqual(odd.units.map((u) => u.unit), ['jlaunch', 'l08firm']);
  await bad({ ...clone(), files: [] }, {}, TEXT.badList);
  const escape = clone();
  escape.files['../x'] = { size: 1, sha256: '0'.repeat(64) };
  await bad(escape, {}, TEXT.badList);
  await bad(manifest, { 'manifest.json': new Uint8Array([0xff, 0xfe]) }, TEXT.unreadable);
  await bad(manifest, { 'manifest.json': null }, TEXT.missing);
  // 清单外多出来的文件，不管大小（Sol r1 P2）。
  await bad(manifest, { 'unlisted.bin': new Uint8Array([1, 2, 3]) }, TEXT.badList);
  await bad(manifest, { 'unlisted-large.bin': new Uint8Array(4 << 20) }, TEXT.badList);
  await bad(manifest, { 'constructor': new Uint8Array([1]) }, TEXT.badList);
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

const amount = { name: 'amount', label: 'Amt', min: 0, max: 150, default: 100, cap_lower: false, cap_upper: false, amount_step: 25, levels: [0, 25, 50, 75, 100, 125, 150] };
const knob = (name, def = 0) => ({ name, label: name, min: -9, max: 9, default: def, cap_lower: false, cap_upper: false, amount_step: null, levels: Array.from({ length: 19 }, (_, i) => i - 9) });

/** `color_listing` 的形状（数据是编的）。 */
function listingJson() {
  const unit = (id) => ({
    id,
    recipes: Array.from({ length: 8 }, (_, i) => ({ slot: i + 1, number: String(i + 1).padStart(2, '0'), controls: i === 3 ? [knob('contrast')] : [amount, knob('contrast'), knob('fade')] })),
    looks: [{ kind: 'look', id: 'a'.repeat(64), name: 'Acros', slot: 3, controls: [amount, knob('contrast')] }],
    users: [{ kind: 'camera_user', id: 'b'.repeat(64), name: 'MY POP', slot: 1, controls: [amount, knob('contrast', 2)] }],
  });
  return { units: [unit('jlaunch'), unit('qlaunch'), { ...unit('l08firm'), recipes: [...unit('l08firm').recipes.slice(0, 4), { kind: 'record', id: 'c'.repeat(64), name: 'FILE 05', slot: 2, number: '05', position: 5, controls: [amount] }] }] };
}

test('recipe cards come from color_listing in the desktop order, names and knob rules', () => {
  const list = shelves(listingJson(), 'jlaunch');
  assert.deepEqual(list.builtin.map((c) => c.name), BASE_NAMES);
  assert.deepEqual(list.builtin.map((c) => c.desc).slice(0, 4), ['饱和度高', '饱和度略低', '黑白，对比高', '原厂标准色彩']);
  assert.deepEqual(list.looks.map((c) => [c.id, c.name, c.desc, c.slot]), [[`look:${'a'.repeat(64)}`, 'Acros', '黑白', 3]]);
  assert.deepEqual(list.users.map((c) => [c.name, c.desc]), [['MY POP', '相机里的配方']]);
  assert.equal(shelf(list, 'all').length, 10);
  assert.equal(cardTag(list.builtin[6]), '相机里 · 07');
  assert.equal(cardTag(list.looks[0]), 'Looks');
  const mount = shelves(listingJson(), 'l08firm');
  assert.deepEqual(mount.builtin.map((c) => c.name), ['GR 正片', 'GR 负片', 'GR 高对比黑白', '原厂标准', 'FILE 05']);
  assert.equal(cardTag(mount.builtin[4]), '相机里 · 05');
  // 交给 color_resolve 的选择：桌面 RecipeSel 的 JSON，只带这个配方的旋钮，档位不在可选里就用默认。
  assert.deepEqual(selection(list.builtin[0], 'jlaunch', undefined), { kind: 'builtin', unit: 'jlaunch', slot: 1, levels: { amount: 100, contrast: 0, fade: 0 } });
  assert.deepEqual(selection(list.looks[0], 'jlaunch', { contrast: 3, fade: 2, amount: 7 }), {
    kind: 'look',
    unit: 'jlaunch',
    slot: 3,
    levels: { amount: 100, contrast: 3 },
    id: 'a'.repeat(64),
  });
  assert.deepEqual(defaults(list.users[0]), { amount: 100, contrast: 2 });
  const sel = selection(list.users[0], 'qlaunch', { contrast: -1 });
  assert.equal(selectionKey(sel), selectionKey({ ...sel, levels: { contrast: -1, amount: 100 } }));
  assert.notEqual(selectionKey(sel), selectionKey(selection(list.users[0], 'qlaunch', { contrast: 1 })));
  assert.equal(effective(knob('contrast'), { contrast: 12 }), 0);
  // 旧版相机（第 135 代以前）这四项只认 ±5；强度不受影响。
  assert.deepEqual(choices(knob('contrast'), 127), [-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5]);
  assert.equal(choices(knob('contrast'), 158).length, 19);
  assert.equal(choices(knob('fade'), 127).length, 19);
  assert.deepEqual(choices(amount, null), amount.levels);
  assert.equal(nearestIndex([-5, 0, 5], 4), 2);
  assert.equal(levelText(amount, 100), '100');
  assert.equal(levelText(knob('contrast'), 1), '+1');
  assert.equal(levelText(knob('contrast'), -2), '-2');
  assert.deepEqual(namesOnly().map((c) => c.name), BASE_NAMES);
});
