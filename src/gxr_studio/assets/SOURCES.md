# 素材来源

这些文件是下载来的数据，放在 `incoming/`，没有执行过里面的任何东西。字体只交给 Pillow 读取字形。SVG 用本机的 `rsvg-convert` 栅格化成 `logo/ricoh.png`，原型画的是这张 PNG。

商标：RICOH 字样是理光的注册商标。这里只给个人照片的装饰边框用，不表示理光认可这个工作室。

总大小约 839 KB，在 3 MB 以内。SHA-256 是文件当前字节。

## 标志

| 文件 | 来源 | 许可证 | 字节 | SHA-256 |
| --- | --- | --- | --- | --- |
| `incoming/logo/Ricoh_logo_2005.svg` | <https://commons.wikimedia.org/wiki/File:Ricoh_logo_2005.svg>，原始文件 <https://upload.wikimedia.org/wikipedia/commons/2/2e/Ricoh_logo_2005.svg> | PD-textlogo（公共领域文字标志）。页面上的 LicenseShortName 是 Public domain | 4108 | `8508bb3b741c70b535606af079758cd64471fce481f4ae3d67f134fc8798981b` |
| `logo/ricoh.png` | 上面那张 SVG 栅格化的 2400×433 RGBA，不是另一次下载 | 同上 | 45747 | `d2a143158f299d10d9722e0d13d4c354742068485b3731ca586411fe159b7b02` |

`incoming/logo/commons.json` 是下载时的 Wikimedia API 记录，不参与画图。

## 字体

都是 SIL Open Font License 1.1。许可证全文在同目录的 txt 里。GXR 三个字母用 Atkinson Hyperlegible。边框上的镜头名和参数用 Inter，因为 Atkinson 的零带斜杠。

| 文件 | 用途 | 来源 | 字节 | SHA-256 |
| --- | --- | --- | --- | --- |
| `incoming/fonts/DSEG7Classic-Regular.ttf` | 时间戳默认的七段数码管 | <https://github.com/keshikan/DSEG> 发行包 v0.46 <https://github.com/keshikan/DSEG/releases/download/v0.46/fonts-DSEG_v046.zip> 里的 `DSEG7-Classic/DSEG7Classic-Regular.ttf`。只解出这一款和许可证，zip 没有放进仓库 | 23272 | `e3270928ced68082f32d3b62877a6741426116335ac0832919326341a8b5cfaf` |
| `incoming/fonts/DSEG-LICENSE.txt` | DSEG 的 OFL。保留字体名 “DSEG” | 同一个 zip 里的 `DSEG-LICENSE.txt` | 4487 | `c6ee0567859cb7cc8d992149bbd96ad359ef65fb7355f7feae0547555ffacd23` |
| `incoming/fonts/CourierPrime-Regular.ttf` | 打字机 | <https://github.com/google/fonts/tree/main/ofl/courierprime> | 71188 | `72f793376f8e2841656bf21d77a5de010f2929bd6956a22ee848ad0c7eb978af` |
| `incoming/fonts/CourierPrime-OFL.txt` | Courier Prime 的 OFL | 同目录 `OFL.txt` | 4403 | `9a755af092b494944c99f471be6fddd19b006a448fefdc4717e4ee0aa09a97b0` |
| `incoming/fonts/AtkinsonHyperlegible-Regular.ttf` | 简洁无衬线，也用来排 “GXR” | <https://github.com/google/fonts/tree/main/ofl/atkinsonhyperlegible> | 54348 | `7fb917c89019896d0b52ee84b7cbb3304c18cb90b19a62f5e32712bd23e97669` |
| `incoming/fonts/AtkinsonHyperlegible-OFL.txt` | Atkinson 的 OFL。版权 2020 Braille Institute of America, Inc. | 同目录 `OFL.txt` | 4352 | `f32d22b3908fcad2c86a74000614ec22e6a7f66ea7e867e616026a27aebdc143` |
| `incoming/fonts/PatrickHand-Regular.ttf` | 拍立得下面的手写 | <https://github.com/google/fonts/tree/main/ofl/patrickhand> | 214772 | `0f173b3e6cb6d1af25babf7f0057c5ac4ee11f9992b0469bb817e967ef4ad0fc` |
| `incoming/fonts/PatrickHand-OFL.txt` | Patrick Hand 的 OFL | 同目录 `OFL.txt` | 4376 | `377f4f9c19e935228552478eb68cc2ed82910988a60ba60e2ac73b09f32d02d1` |
| `fonts/Inter-Regular.ttf` | 边框上的镜头名和参数。零是普通椭圆，没有斜杠也没有圆点 | <https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip> 里的 `extras/ttf/Inter-Regular.ttf`。zip 没有放进仓库 | 411640 | `40d692fce188e4471e2b3cba937be967878f631ad3ebbbdcd587687c7ebe0c82` |
| `fonts/Inter-OFL.txt` | Inter 的 OFL。版权 2016 The Inter Project Authors | 同一个 zip 里的 `LICENSE.txt` | 4380 | `262481e844521b326f5ecd053e59b98c8b2da78c8ee1bdbb6e8174305e54935a` |

## 测试夹具

产品画中文仍然只找系统字体。下面这份只给测试注入，不进运行时的回退列表。用 fontTools 从 Noto Sans CJK SC Regular 裁出「上海北京」四个字。

| 文件 | 用途 | 来源 | 字节 | SHA-256 |
| --- | --- | --- | --- | --- |
| `tests/fixtures/NotoSansSC-Shanghai.otf` | 可移植测试里的「上海北京」 | <https://github.com/notofonts/noto-cjk> `Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf`，fontTools 只留 U+4E0A U+6D77 U+5317 U+4EAC | 5580 | `e86f156b1ca5989ea95837ef2d72833d68b71974171728ce6baa7e478b55a1af` |
| `tests/fixtures/NotoSansSC-OFL.txt` | 这份裁剪字体的 OFL。保留字体名 Noto | 同一个仓库的 `Sans/LICENSE` | 4301 | `6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2` |
