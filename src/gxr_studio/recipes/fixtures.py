"""冻结D65固定梯度、可达色相环和训练期色相罚项。输入域为mapped。"""

from __future__ import annotations

import numpy as np

from ..camdata.unit_model import UnitModel
from ..engine import colour
from ..engine import matrix_model as mm
from .numerics import FullProblem, curve_x, predict_mapped


def fixtures(model: UnitModel) -> tuple[dict[str, np.ndarray], tuple[dict[str, np.ndarray], ...]]:
    stock = model.factory
    curve = mm.Curve(curve_x(stock), list(stock[82:115]))
    row = mm.MatrixRow.from_bytes(stock[216:244])
    t = np.linspace(0, 1, 2049)[:, None]
    colors = {
        "gray": (np.zeros(3), np.full(3, 255.0)),
        "sky": (np.array([70.0, 115.0, 180.0]), np.array([195.0, 224.0, 248.0])),
        "skin": (np.array([70.0, 42.0, 30.0]), np.array([236.0, 191.0, 157.0])),
    }
    gradients = {key: curve.inverse(a + (b - a) * t) for key, (a, b) in colors.items()}
    rings = []
    hue = np.linspace(0, 360, 721)
    for y in (64.0, 128.0, 192.0):
        for radius in (5.0, 20.0, 40.0):
            chroma = radius * np.exp(1j * np.deg2rad(hue))
            pre = row.invert(np.full(len(hue), y), chroma)
            valid = np.all((pre > curve.ys[0]) & (pre < curve.ys[-1]), axis=1)
            rings.append({"x": curve.inverse(pre), "valid": valid})
    return gradients, tuple(rings)


def _ring_masks(model: UnitModel, entry: bytes, rings: tuple[dict[str, np.ndarray], ...]) -> list[np.ndarray]:
    result = []
    for ring in rings:
        rgb = predict_mapped(model, entry, ring["x"])
        chroma = mm.ycbcr(rgb)[1]
        angle = np.unwrap(np.angle(chroma)) * 180 / np.pi
        eligible = ring["valid"][:-1] & ring["valid"][1:]
        defined = eligible & (abs(chroma[:-1]) >= 2) & (abs(chroma[1:]) >= 2)
        result.append(defined & (np.diff(angle) / 0.5 < -0.001))
    return result


def guard(model: UnitModel, candidate: bytes, original: bytes) -> dict[str, object]:
    gradients, rings = fixtures(model)
    maximum = 0.0
    for x in gradients.values():
        lab = colour.lab_from_codes(np.rint(predict_mapped(model, candidate, x)))
        maximum = max(maximum, float(colour.delta_e00(lab[:-1], lab[1:]).max()))
    before, after = _ring_masks(model, original, rings), _ring_masks(model, candidate, rings)
    new = [int((a & ~b).sum()) for a, b in zip(after, before)]
    return {
        "passes": sum(new) == 0 and maximum <= 2,
        "new_reverse_intervals_by_ring": new,
        "new_reverse_hue_intervals": sum(new),
        "max_integer_gradient_step_de00": maximum,
        "input_domain": "mapped",
    }


class GuardedProblem:
    def __init__(self, primary: FullProblem) -> None:
        self.primary = primary
        _gradients, rings = fixtures(primary.model)
        x = np.concatenate([r["x"] for r in rings])
        target = predict_mapped(primary.model, primary.training.original, x)
        self.fixture = FullProblem(primary.training, primary.axis, x=x, want=target, sw=np.ones(len(target)))
        left: list[int] = []
        right: list[int] = []
        eligible: list[bool] = []
        offset = 0
        for ring in rings:
            n = len(ring["x"])
            left.extend(range(offset, offset + n - 1))
            right.extend(range(offset + 1, offset + n))
            eligible.extend(ring["valid"][:-1] & ring["valid"][1:])
            offset += n
        self.left, self.right = np.array(left), np.array(right)
        chroma = mm.ycbcr(target)[1]
        delta = np.angle(chroma[self.right] / np.where(abs(chroma[self.left]) > 1e-12, chroma[self.left], 1)) * 180 / np.pi
        reverse = (abs(chroma[self.left]) >= 2) & (abs(chroma[self.right]) >= 2) & (delta / 0.5 < -0.001)
        self.protect = np.asarray(eligible, bool) & ~reverse

    def evaluate(self, theta: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        residual, jacobian = self.primary.evaluate(theta)
        self.fixture.evaluate(theta)
        if self.fixture.raw_state is None:
            raise ArithmeticError("missing fixed-fixture forward state")
        _point, raw, jac = self.fixture.raw_state
        rgb = np.clip(raw, 0, 255)
        jac = jac * ((raw > 0) & (raw < 255))[:, :, None]
        chroma = mm.ycbcr(rgb)[1]
        chroma_derivative = np.array([-0.168736 + 0.5j, -0.331264 - 0.418688j, 0.5 - 0.081312j])
        dchroma = np.einsum("c,ncp->np", chroma_derivative, jac)
        a, b = self.left, self.right
        delta = np.angle(chroma[b] / np.where(abs(chroma[a]) > 1e-12, chroma[a], 1)) * 180 / np.pi
        active = self.protect & (abs(chroma[a]) >= 2) & (abs(chroma[b]) >= 2) & (delta < 0)
        penalty = 40.0 * np.minimum(delta, 0) * active
        direction = np.zeros_like(dchroma)
        np.divide(dchroma, chroma[:, None], out=direction, where=abs(chroma[:, None]) > 1e-12)
        derivative = 40.0 * (direction[b] - direction[a]).imag * 180 / np.pi * active[:, None]
        return np.r_[residual, penalty], np.vstack([jacobian, derivative])
