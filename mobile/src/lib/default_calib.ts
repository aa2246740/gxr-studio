// 内置的默认校准（机主 2026-11-24 决定公开）：作者三支单元（50mm、28mm、MOUNT）实拍拟合出来的参数。
// 包里只有 manifest.json、ctx_*.npz（d0、d1 和输入系数）和 ring_*.json（色相分区模型），没有固件字节，也没有
// 理光的原厂表；原厂 04 照样从用户自己的相机配方文件里取。zip 原样放在 mobile/public/calib/。
// 加载时先核对整个文件的 SHA-256 是这里记的这一份，再走和用户导入一样的核对（lib/zip.ts、lib/calib.ts、核心的
// color_calib_pack）。scripts/private_bytes.py 只放行这个路径上的这一份字节。

/** 相对网站根（Vite 的 base）的路径。 */
export const DEFAULT_CALIB_PATH = 'calib/gxr-default-calib.zip';
export const DEFAULT_CALIB_SHA256 = '0bac0fe18613794fa39715e80c0067701e5718a985beb568af23c1848764f0da';

export const DEFAULT_LABEL = '默认校准（基于样机）';
export const DEFAULT_NOTE = '用作者的三支单元实拍拟合；想更准可以换成自己的校准。';

/** 默认校准用不了的原因（「未校准」后面那一句）。 */
export const DEFAULT_TEXT = {
  fetch: '默认校准没有下载下来，可能没联网。',
  bad: '默认校准文件没有通过核对。',
} as const;

/** 下载地址带上 SHA-256 的前 12 位：换了新的一份，浏览器缓存里的旧文件不会被拿来核对。 */
export function defaultCalibUrl(base: string, page: string): string {
  return new URL(`${base}${DEFAULT_CALIB_PATH}?v=${DEFAULT_CALIB_SHA256.slice(0, 12)}`, page).href;
}
