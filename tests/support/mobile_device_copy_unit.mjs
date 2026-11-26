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
