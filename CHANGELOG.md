# Changelog

## Unreleased

- 导出只编码一次（画质更好）；HDR 增益改在编码前的像素上拟合；PNG 导出不再先过一遍 JPEG。
- 调整的 sRGB 换算改用双精度幂，导出在不同电脑上结果一致
- 只有 RAW、没有相机数据的照片导出时，曝光、白平衡、对比度等调整会被丢掉 → 已修复
- 导出接口选「中性转换」时，有校准数据也会画成原厂 04 → 已修复，现在一律是中性转换

- 3f64665 Add the checks and local operator tools the studio was missing.
- e4fe1be Import the GXR Studio tree as its own local repository.
