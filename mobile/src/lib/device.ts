// 设备名称判定（iPad / iPhone / 电脑）：纯函数，便于 Node 单测传参验证，浏览器运行时自动读取 navigator。
// iPadOS Safari 默认将 UA/platform 报成 Macintosh，需结合 maxTouchPoints > 1 识别；横屏手机保持 iPhone。
// 电脑和 lib/save_mode.ts 的 isComputer() 同一条规则：没有触摸点（maxTouchPoints 恰好是 0）且指针精确。

export { saveWord } from './save_mode.ts';

export interface DeviceEnv {
  userAgent?: string;
  platform?: string;
  maxTouchPoints?: number;
  /** `(pointer: fine)` 是否成立。 */
  finePointer?: boolean;
}

export type DeviceName = 'iPad' | 'iPhone' | '电脑';

function readEnv(): DeviceEnv {
  const nav = typeof navigator === 'object' && navigator ? navigator : null;
  if (!nav) return {};
  return {
    userAgent: typeof nav.userAgent === 'string' ? nav.userAgent : '',
    platform: typeof nav.platform === 'string' ? nav.platform : '',
    maxTouchPoints: typeof nav.maxTouchPoints === 'number' ? nav.maxTouchPoints : undefined,
    finePointer: typeof globalThis.matchMedia === 'function' && globalThis.matchMedia('(pointer: fine)').matches,
  };
}

/** 判定当前环境是否为 iPad：需同时满足多点触控（maxTouchPoints > 1）与 Mac/iPad 平台或 UA，不靠视口尺寸。 */
export function isIpadDevice(env: DeviceEnv = readEnv()): boolean {
  const ua = env.userAgent ?? '';
  const platform = env.platform ?? '';
  const touch = env.maxTouchPoints ?? 0;
  if (touch <= 1) return false;
  return /\b(iPad|Macintosh|MacIntel)\b/i.test(ua) || /\biPad\b/i.test(platform) || /Mac/i.test(platform);
}

/** 电脑浏览器：没有触摸点且指针精确（和 isComputer() 一样）。 */
export function isComputerDevice(env: DeviceEnv = readEnv()): boolean {
  return env.maxTouchPoints === 0 && env.finePointer === true;
}

/** 界面文案使用的设备名：iPadOS 返回「iPad」，电脑返回「电脑」，其余（含横屏手机与非触控测试环境）返回「iPhone」。 */
export function deviceName(env: DeviceEnv = readEnv()): DeviceName {
  if (isIpadDevice(env)) return 'iPad';
  return isComputerDevice(env) ? '电脑' : 'iPhone';
}

/**
 * 「这台 iPhone」「这台 iPad」「这台电脑」，可带上后面的话。中文和拉丁字之间留一个空格，中文和中文之间不留：
 * `thisDevice('上')` 是「这台 iPhone 上」或「这台电脑上」；标点前不留空格。
 */
export function thisDevice(tail = '', env: DeviceEnv = readEnv()): string {
  const name = deviceName(env);
  const phrase = name === '电脑' ? '这台电脑' : `这台 ${name}`;
  return /[A-Za-z]$/.test(phrase) && /^[一-鿿]/.test(tail) ? `${phrase} ${tail}` : phrase + tail;
}

/** 起始页的帮助链接、帮助面板标题和网格「更多」那一行：手机「怎么把卡插到 iPhone」，电脑「怎么把存储卡接到电脑」。 */
export function cardHelpTitle(env: DeviceEnv = readEnv()): string {
  return isComputerDevice(env) ? '怎么把存储卡接到电脑' : `怎么把卡插到 ${deviceName(env)}`;
}
