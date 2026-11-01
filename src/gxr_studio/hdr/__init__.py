"""HDR：相机 JPG（或导出的成片）原样做主图，加上从同一张 DNG 算出的增益图（ISO 21496-1 + Ultra HDR）。

`gain` 算增益，`container` 封装，`derived` 在导入后生成图库里的 HDR 版本。导出时的重新计算在 `finish.hdr_export`。
原型和验证见 research/hdr_gainmap/REPORT.md。
"""

from .container import GainMeta, assemble
from .gain import HdrUnsupported, Params, RawLuma, hdr_from_camera_jpeg, raw_luma

__all__ = ["GainMeta", "HdrUnsupported", "Params", "RawLuma", "assemble", "hdr_from_camera_jpeg", "raw_luma"]
