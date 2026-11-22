// 设备名称判定（iPad / iPhone）：纯函数，便于 Node 单测传参验证，浏览器运行时自动读取 navigator。
// iPadOS Safari 默认将 UA/platform 报成 Macintosh，需结合 maxTouchPoints > 1 识别；横屏手机保持 iPhone。

export interface DeviceEnv {
  userAgent?: string;
  platform?: string;
  maxTouchPoints?: number;
}

export type DeviceName = 'iPad' | 'iPhone';

function readEnv(): DeviceEnv {
  const nav = typeof navigator === 'object' && navigator ? navigator : null;
  if (!nav) return {};
  return {
    userAgent: typeof nav.userAgent === 'string' ? nav.userAgent : '',
    platform: typeof nav.platform === 'string' ? nav.platform : '',
    maxTouchPoints: typeof nav.maxTouchPoints === 'number' ? nav.maxTouchPoints : 0,
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

/** 界面文案使用的设备名：iPadOS 返回「iPad」，其余（含横屏手机与非触控测试环境）返回「iPhone」。 */
export function deviceName(env: DeviceEnv = readEnv()): DeviceName {
  return isIpadDevice(env) ? 'iPad' : 'iPhone';
}
