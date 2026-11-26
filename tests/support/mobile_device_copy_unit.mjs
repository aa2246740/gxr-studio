// desktop-copy 单元测试（Node --experimental-strip-types，不碰 DOM）：lib/device.ts 的设备名、「这台…」短语、
// 帮助标题，以及 hdr_text / lut 用它拼出来的句子。手机和 iPad 的原话逐字不变，电脑上不出现 iPhone。

import test from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';

registerHooks({
  resolve(spec, ctx, next) {
    try {
      return next(spec, ctx);
    } catch (err) {
      if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(`${spec}.ts`, ctx);
      throw err;
    }
  },
});

const IPHONE = {
  userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1',
  platform: 'iPhone',
  maxTouchPoints: 5,
  finePointer: false,
};
const IPAD_MAC_UA = {
  userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15',
  platform: 'MacIntel',
  maxTouchPoints: 5,
  finePointer: false,
};
const MAC = {
  userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15',
  platform: 'MacIntel',
  maxTouchPoints: 0,
  finePointer: true,
};
const WINDOWS = {
  userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
  platform: 'Win32',
  maxTouchPoints: 0,
  finePointer: true,
};
// iPad 配妙控键盘：指针精确，但有触摸点，仍是 iPad。
const IPAD_TRACKPAD = { ...IPAD_MAC_UA, finePointer: true };

test('deviceName: iPhone, iPadOS (Mac UA with 5 touch points) and computers', async () => {
  const { deviceName, isComputerDevice, isIpadDevice } = await import('../../mobile/src/lib/device.ts');
  assert.equal(deviceName(IPHONE), 'iPhone');
  assert.equal(deviceName(IPAD_MAC_UA), 'iPad');
  assert.equal(deviceName(IPAD_TRACKPAD), 'iPad');
  assert.equal(deviceName(MAC), '电脑');
  assert.equal(deviceName(WINDOWS), '电脑');
  assert.equal(isComputerDevice(MAC), true);
  assert.equal(isComputerDevice(WINDOWS), true);
  assert.equal(isComputerDevice(IPHONE), false);
  assert.equal(isComputerDevice(IPAD_TRACKPAD), false);
  assert.equal(isIpadDevice(MAC), false);
  // 和 isComputer() 一样：没触摸点但指针不精确、或者读不到触摸点数，都不算电脑。
  assert.equal(deviceName({ ...MAC, finePointer: false }), 'iPhone');
  assert.equal(deviceName({ ...MAC, maxTouchPoints: undefined }), 'iPhone');
  assert.equal(deviceName({}), 'iPhone');
});

test('thisDevice phrase: a space before the Latin word only, none before CJK on a computer', async () => {
  const { thisDevice } = await import('../../mobile/src/lib/device.ts');
  assert.equal(thisDevice('', IPHONE), '这台 iPhone');
  assert.equal(thisDevice('', IPAD_MAC_UA), '这台 iPad');
  assert.equal(thisDevice('', MAC), '这台电脑');
  assert.equal(thisDevice('', WINDOWS), '这台电脑');
  assert.equal(thisDevice('上', IPHONE), '这台 iPhone 上');
  assert.equal(thisDevice('上', MAC), '这台电脑上');
  assert.equal(thisDevice('的浏览器里', IPAD_MAC_UA), '这台 iPad 的浏览器里');
  assert.equal(thisDevice('的浏览器里', MAC), '这台电脑的浏览器里');
  assert.equal(thisDevice('，不会上传。', IPHONE), '这台 iPhone，不会上传。');
  assert.equal(thisDevice('。', MAC), '这台电脑。');
  assert.equal(thisDevice('：这次能用', IPHONE), '这台 iPhone：这次能用');
});

test('cardHelpTitle: phones and iPads unchanged, computers get the card-to-computer wording', async () => {
  const { cardHelpTitle } = await import('../../mobile/src/lib/device.ts');
  assert.equal(cardHelpTitle(IPHONE), '怎么把卡插到 iPhone');
  assert.equal(cardHelpTitle(IPAD_MAC_UA), '怎么把卡插到 iPad');
  assert.equal(cardHelpTitle(MAC), '怎么把存储卡接到电脑');
  assert.equal(cardHelpTitle(WINDOWS), '怎么把存储卡接到电脑');
});

test('hdrLine and lutFootnote: byte-identical on phone and iPad, no iPhone on a computer', async () => {
  const { hdrLine } = await import('../../mobile/src/lib/hdr_text.ts');
  const { lutFootnote } = await import('../../mobile/src/lib/lut.ts');
  assert.equal(hdrLine(IPHONE), '在 iPhone 上看，亮处会更亮，更接近你当时看到的；别的设备照常显示。');
  assert.equal(hdrLine(IPAD_MAC_UA), '在 iPad 上看，亮处会更亮，更接近你当时看到的；别的设备照常显示。');
  assert.equal(hdrLine(MAC), '在支持 HDR 的屏幕上看，亮处会更亮，更接近你当时看到的；别的屏幕照常显示。');
  assert.equal(lutFootnote(IPHONE), 'LUT 只存在这台 iPhone 上，直接套在照片上，不写入相机。');
  assert.equal(lutFootnote(IPAD_MAC_UA), 'LUT 只存在这台 iPad 上，直接套在照片上，不写入相机。');
  assert.equal(lutFootnote(WINDOWS), 'LUT 只存在这台电脑上，直接套在照片上，不写入相机。');
});

test('HDR note blames the browser when there is no GPU preview, the screen otherwise', async () => {
  const { HdrPreviewController, HDR_SCREEN_NOTE } = await import('../../mobile/src/core/hdr_preview.ts');
  const note = (engine) => {
    const ctl = new HdrPreviewController({
      photo: () => ({ id: 'p', version: 1, rev: 0, dng: {}, jpg: null }),
      session: () => 1,
      dragging: () => false,
      holding: () => false,
      exportOn: () => true,
      display: () => false,
      engine: () => engine,
      readDng: async () => null,
      fit: async () => ({ ok: false }),
      clock: { set: () => 1, clear: () => undefined },
    });
    const host = { nodes: [], querySelector() { return this.nodes[0] ?? null; }, append(node) { this.nodes = [node]; } };
    const real = globalThis.document;
    globalThis.document = { createElement() { return { dataset: {}, className: '', textContent: '', remove() { host.nodes = []; } }; } };
    try {
      ctl.sync();
      ctl.paintScreenNote(host);
      return host.nodes[0]?.textContent ?? null;
    } finally {
      globalThis.document = real;
      ctl.close();
    }
  };
  const engine = { setOutput() {}, async readback() { return { w: 1, h: 1, data: new Uint8Array(4) }; }, hdrGainCount: () => 0 };
  assert.equal(note(engine), HDR_SCREEN_NOTE);
  assert.equal(note(engine), '这块屏幕显示不了 HDR，导出的照片仍带 HDR');
  assert.equal(note(null), '这个浏览器显示不了 HDR 预览，导出的照片仍带 HDR');
});

test('cubeErrorText: the parser English becomes Chinese with the line number kept', async () => {
  const { cubeErrorText, LUT_ERRORS } = await import('../../mobile/src/lib/lut.ts');
  assert.equal(cubeErrorText('line 4: unsupported directive not'), '第 4 行读不懂，不是有效的 LUT');
  assert.equal(cubeErrorText('line 2: duplicate or late header LUT_3D_SIZE'), '第 2 行的文件头重复或位置不对，不是有效的 LUT');
  assert.equal(cubeErrorText('line 1: TITLE needs one quoted string'), '第 1 行的标题要用引号括起来');
  assert.equal(cubeErrorText('line 3: invalid LUT size'), '第 3 行的 LUT 尺寸不对');
  assert.equal(cubeErrorText('line 3: LUT size out of range'), '第 3 行的 LUT 尺寸不对');
  assert.equal(cubeErrorText('line 5: invalid DOMAIN_MIN'), '第 5 行的范围数值不对');
  assert.equal(cubeErrorText('line 5: non-finite header'), '第 5 行的范围数值不对');
  assert.equal(cubeErrorText('line 9: expected three finite numbers'), '第 9 行应该是三个数字');
  assert.equal(cubeErrorText('line 40: unexpected data row'), '第 40 行多出了一行数据');
  assert.equal(cubeErrorText('line 7: something new'), '第 7 行读不懂，不是有效的 LUT');
  assert.equal(cubeErrorText('No closing quotation'), '引号没有配对，不是有效的 LUT');
  assert.equal(cubeErrorText("could not convert string to float: \"x\""), '有读不懂的数字，不是有效的 LUT');
  assert.equal(cubeErrorText("invalid literal for int() with base 10: 'x'"), '有读不懂的数字，不是有效的 LUT');
  assert.equal(cubeErrorText("invalid literal for int() with base 10: '三'"), '有读不懂的数字，不是有效的 LUT');
  assert.equal(cubeErrorText('Unknown 中文 token'), LUT_ERRORS.cube_unreadable);
  assert.equal(cubeErrorText('LUT 数据行数与声明不符'), 'LUT 数据行数与声明不符');
  assert.equal(cubeErrorText('Something else'), LUT_ERRORS.cube_unreadable);
});

test('LutBook import notice shows the Chinese parse error, not the parser English', async () => {
  const { LutBook } = await import('../../mobile/src/app/lut_book.ts');
  const book = new LutBook({
    importLut: async () => ({ ok: false, reason: 'parse', message: 'line 4: unsupported directive not' }),
    restore: async () => ({ records: [], order: [], totalBytes: 0, watermark: 0 }),
    patch: async () => ({ ok: false, reason: 'not_found' }),
    remove: async () => true,
    readBlob: async () => null,
  });
  const out = await book.importFiles([new File(['TITLE "x"\n'], 'synth-broken.cube')]);
  assert.deepEqual(out.errors, ['synth-broken.cube：第 4 行读不懂，不是有效的 LUT']);
  assert.ok(!/[A-Za-z]{4,}/.test(out.message.replace('synth-broken.cube', '')), out.message);
});
