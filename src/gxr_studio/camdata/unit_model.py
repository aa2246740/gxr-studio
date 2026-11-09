"""逐单元电脑预测：研究 native-C040-v2.0，冻结的 engine 保持原样。"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..engine import knobs
from ..engine.model import Model


def input_codes(raw: np.ndarray, coefficients: np.ndarray | None) -> np.ndarray:
    """与研究 physical_calibration.apply 相同的归约顺序；输入是白平衡后码值。"""
    if coefficients is None:
        return raw
    u = np.asarray(raw) / 4095
    basis = np.c_[np.ones(len(u)), u]
    if len(coefficients) == 10:
        r, g, b = u.T
        basis = np.c_[basis, r * r, g * g, b * b, r * g, r * b, g * b]
    return np.asarray(raw + 4095 * (basis @ coefficients))


class UnitModel(Model):
    def __init__(
        self, stock: np.ndarray, d0: np.ndarray, d1: np.ndarray, ring: dict[str, Any], factory: bytes, coefficients: np.ndarray
    ) -> None:
        if coefficients.shape not in ((4, 3), (10, 3)) or not np.all(np.isfinite(coefficients)):
            raise ValueError("校准输入映射的尺寸或数值不正确")
        super().__init__(stock, d0, d1, ring)
        table = self.engine.table
        if (
            table.nodes.shape != (24,)
            or not np.all(np.diff(table.nodes) > 0)
            or not all(np.all(np.isfinite(v)) for v in (table.nodes, table.gain, table.k, table.s0, table.kappa))
        ):
            raise ValueError("校准色相环的节点或参数不正确")
        self.factory = factory
        self.input_coefficients: np.ndarray | None = coefficients

    def predict(
        self,
        b: bytes | bytearray,
        x: np.ndarray,
        channel_mode: Any = None,
        quantize: bool = False,
        half: int = 0,
        bw_chroma: str = "conditional",
    ) -> np.ndarray:
        pre = self.pre(b, input_codes(x, self.input_coefficients), channel_mode)
        row = self.mm.MatrixRow.from_bytes(b[216 + half * 28 : 244 + half * 28])
        coeff = np.array([[knobs.dec(z, 128) for z in b[i : i + 4]] for i in range(115, 211, 4)])
        if np.any(coeff[:, 0] != coeff[:, 3]) or np.any(coeff[:, 1] != -coeff[:, 2]):
            raise ValueError("frozen symmetric sector model cannot predict asymmetric rows")
        ab = coeff[:, :2]
        z = self.fs.Z_of_words(ab[:, 0], ab[:, 1], self.engine.ctx)
        y, ci = row.apply(pre)
        if b[0] == 3:
            co = ci if bw_chroma == "conditional" else np.zeros_like(ci)
        else:
            cl = self.mm.MatrixRow.from_bytes(self.factory[216 + half * 28 : 244 + half * 28]).apply(pre)[1]
            table = self.engine.table
            hue, magnitude = table.sm.hue_deg(cl), abs(cl)
            co = (ci * table.S_of(hue, magnitude)) * table.z(z, hue, magnitude)
        rgb = np.clip(self.mm.rgb_from_ycc(y, co), 0, 255)
        return np.floor(rgb + 0.5).astype(np.uint8) if quantize else rgb
