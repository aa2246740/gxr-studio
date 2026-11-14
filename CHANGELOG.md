# Changelog

## Unreleased

- 导出只编码一次（画质更好）；HDR 增益改在编码前的像素上拟合；PNG 导出不再先过一遍 JPEG。
- 调整的 sRGB 换算改用双精度幂，导出在不同电脑上结果一致

- 3f64665 Add the checks and local operator tools the studio was missing.
- e4fe1be Import the GXR Studio tree as its own local repository.
