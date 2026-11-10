"""可移植native-C040-v2.0连续问题。只优化曲线、C100和对称扇区。

参数化/正则与冻结film_look_routes、ceiling_model及matrix_classes相同；
原厂色度查表和曲线雅可比额外项来自v2patch。没有研究运行时或私有kernel。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..camdata.unit_model import UnitModel
from ..engine import colour, knobs
from ..engine import matrix_model as mm
from ..fit_contexts.types import TrainingInput
from .record import check_record


def weights(x: np.ndarray, nodes: np.ndarray) -> np.ndarray:
    x = np.clip(np.asarray(x), nodes[0], nodes[-1])
    indices = np.clip(np.searchsorted(nodes, x, side="right") - 1, 0, len(nodes) - 2)
    fraction = (x - nodes[indices]) / (nodes[indices + 1] - nodes[indices])
    result = np.zeros((*x.shape, len(nodes)))
    np.put_along_axis(result, indices[..., None], (1 - fraction)[..., None], axis=-1)
    np.put_along_axis(result, (indices + 1)[..., None], fraction[..., None], axis=-1)
    return result


def curve_x(entry: bytes) -> np.ndarray:
    return np.array([0, *struct.unpack_from(">31H", entry, 20), 4095], dtype=int)


def predict_mapped(model: UnitModel, entry: bytes, mapped: np.ndarray) -> np.ndarray:
    """冻结评分顺序。mapped已核输入域，这里绝不再做input_codes。"""
    pre = model.pre(entry, mapped)
    row = mm.MatrixRow.from_bytes(entry[216:244])
    words = np.array([[knobs.dec(z, 128) for z in entry[i : i + 4]] for i in range(115, 211, 4)])
    if not (np.array_equal(words[:, 0], words[:, 3]) and np.array_equal(words[:, 1], -words[:, 2])):
        raise ValueError("完整拟合不支持非对称扇区")
    z = colour.Z_of_words(words[:, 0], words[:, 1], model.engine.ctx)
    y, ci = row.apply(pre)
    cl = mm.MatrixRow.from_bytes(model.factory[216:244]).apply(pre)[1]
    table = model.engine.table
    hue, magnitude = table.sm.hue_deg(cl), abs(cl)
    co = (ci * table.S_of(hue, magnitude)) * table.z(z, hue, magnitude)
    return np.clip(mm.rgb_from_ycc(y, co), 0, 255)


def literal_mapped(model: UnitModel, entry: bytes, mapped: np.ndarray) -> np.ndarray:
    """独立展开的v2前向，用于<1e-8的回放核对，不取代冻结评分归约。"""
    nodes = curve_x(entry)
    curves = (entry[379:412], entry[82:115], entry[412:445]) if entry[1] & 4 else (entry[82:115],) * 3
    pre = np.column_stack([np.interp(np.clip(mapped[:, c], 0, 4095), nodes, list(curves[c])) for c in range(3)])
    row = mm.MatrixRow.from_bytes(entry[216:244])
    ycc = pre @ row.effective(100.0).T + row.offsets
    y, ci = ycc[:, 0], ycc[:, 1] - 128 + 1j * (ycc[:, 2] - 128)
    factory = mm.MatrixRow.from_bytes(model.factory[216:244])
    lookup = pre @ factory.effective(100.0).T + factory.offsets
    cl = lookup[:, 1] - 128 + 1j * (lookup[:, 2] - 128)
    words = np.array([[knobs.dec(z, 128) for z in entry[i : i + 4]] for i in range(115, 211, 4)])
    if not (np.array_equal(words[:, 0], words[:, 3]) and np.array_equal(words[:, 1], -words[:, 2])):
        raise ValueError("完整拟合不支持非对称扇区")
    context, table = model.engine.ctx, model.engine.table
    ordinary = colour.Z_of_words(words[:, 0], words[:, 1], context)
    effective = abs(ordinary) * np.exp(1j * table.k * np.angle(ordinary))
    basis = table.kernel_weights(np.angle(cl, deg=True) % 360, abs(cl))
    multiplier = basis @ context.S_eff + (basis * table.gain) @ (context.S_eff * (effective - 1))
    co = ci * multiplier
    return np.clip(mm.rgb_from_ycc(y, co), 0, 255)


def integer_objective(model: UnitModel, entry: bytes, mapped: np.ndarray, want: np.ndarray, sw: np.ndarray) -> float:
    prediction = np.rint(predict_mapped(model, entry, mapped))
    error = colour.delta_e00(colour.lab_from_codes(prediction), colour.lab_from_codes(want))
    return float(np.average(error * error, weights=sw))


def rgb_jacobian(value: np.ndarray) -> np.ndarray:
    return np.stack([1.402 * value.imag, -0.344136 * value.real - 0.714136 * value.imag, 1.772 * value.real], axis=1)


@dataclass(frozen=True)
class State:
    matrix: np.ndarray
    curves: np.ndarray
    z: np.ndarray
    sector_a: np.ndarray
    sector_b: np.ndarray


class FullProblem:
    def __init__(
        self,
        training: TrainingInput,
        axis: np.ndarray,
        *,
        x: np.ndarray | None = None,
        want: np.ndarray | None = None,
        sw: np.ndarray | None = None,
    ) -> None:
        self.training = training
        self.model = training.environment.model_for(200)[0]
        self.table, self.context = self.model.engine.table, self.model.engine.ctx
        self.axis = np.asarray(axis, dtype=float)
        self.bw = training.family == "bw"
        self.nc = 1 if self.bw else 3
        self.x = training.samples.x if x is None else x
        self.want = training.samples.want if want is None else want
        sample_weights = training.samples.sw if sw is None else sw
        self.sqrtw: np.ndarray = np.sqrt(sample_weights / np.mean(sample_weights) / len(sample_weights))[:, None]
        self.target_lab = colour.lab_from_codes(self.want)
        self.row = mm.MatrixRow.from_bytes(training.original[216:244])
        self.factory = mm.MatrixRow.from_bytes(self.model.factory[216:244])
        self.stock_curve = np.array(list(self.model.factory[82:115]), float)
        self.qstart = 2 if self.bw else 50
        self.qend = self.qstart + self.nc * 33
        self.matrix_start = self.qend
        self.size = self.qend + (1 if self.bw else 7)
        self.theta0 = np.zeros(self.size)
        if not self.bw:
            original = colour.Z_of_words(self.context.stock[:, 0], self.context.stock[:, 1], self.context)
            self.theta0[2:26], self.theta0[26:50] = np.log(abs(original)), np.angle(original)
            self.theta0[abs(self.theta0) < 1e-12] = 0
        self.lower, self.upper = np.full(self.size, -255.0), np.full(self.size, 255.0)
        self.lower[:2], self.upper[:2] = -0.8, 0.8
        self.lower[self.matrix_start :], self.upper[self.matrix_start :] = -0.8, 0.8
        if not self.bw:
            self.lower[2:26], self.upper[2:26] = np.log(0.25), np.log(2.3)
            self.lower[26:50], self.upper[26:50] = self.theta0[26:50] - 0.75, self.theta0[26:50] + 0.75
            for j, base in enumerate(self.row.coef[1:].ravel()):
                self.lower[self.matrix_start + 1 + j] = max(-0.8, (-1279 - float(base)) / 1024)
                self.upper[self.matrix_start + 1 + j] = min(0.8, (1279 - float(base)) / 1024)
        self.regularizer = self._regularizer()
        self.basis = weights(self.x, self.axis)
        self.cache: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        self.raw_state: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None

    def _regularizer(self) -> np.ndarray:
        eye = np.eye(self.size)
        rows: list[np.ndarray] = []
        rows.extend(eye[j] * 0.02 for j in range(2))
        if not self.bw:
            for offset, scale in ((2, 0.12), (26, 0.10)):
                for j in range(24):
                    rows.extend((eye[offset + j] * 0.02 / scale, (eye[offset + j] - eye[offset + (j + 1) % 24]) * 0.06 / scale))
        for channel in range(self.nc):
            start = self.qstart + channel * 33
            rows.extend(eye[start + j] * 0.0008 for j in range(33))
            rows.extend((eye[start + j] - 2 * eye[start + j + 1] + eye[start + j + 2]) * 0.012 for j in range(31))
        rows.extend(eye[j] * 0.025 for j in range(self.matrix_start, self.size))
        return np.asarray(rows)

    def state(self, theta: np.ndarray, *, quantized: bool = False) -> State:
        matrix = self.row.coef.astype(float).copy()
        matrix[0] += 1024 * np.array([theta[0], -theta[0] - theta[1], theta[1]])
        matrix[0, 1] += theta[self.matrix_start] * 1024
        if not self.bw:
            matrix[1:] += theta[self.matrix_start + 1 :].reshape(2, 3) * 1024
        else:
            matrix[1:] = 0
        curves = self.stock_curve + theta[self.qstart : self.qend].reshape(self.nc, 33)
        a, b = self.context.stock[:, 0].copy(), self.context.stock[:, 1].copy()
        z = np.zeros(24, complex) if self.bw else np.exp(theta[2:26] + 1j * self.table.k * theta[26:50])
        if quantized:
            matrix = np.rint(np.clip(matrix, -2047, 2047)).astype(int)
            curves = np.maximum.accumulate(np.rint(np.clip(curves, 0, 255)).astype(int), axis=1)
            if not self.bw:
                physical = np.exp(theta[2:26] + 1j * theta[26:50]) * self.context.S_eff * 32
                a = np.clip(np.rint(physical.real - self.context.d0), -127, 127).astype(int)
                b = np.clip(np.rint(-physical.imag - self.context.d1), -127, 127).astype(int)
                actual = colour.Z_of_words(a, b, self.context)
                z = abs(actual) * np.exp(1j * self.table.k * np.angle(actual))
        return State(matrix, curves, z, a, b)

    def seed(self, entry: bytes) -> np.ndarray:
        theta = self.theta0.copy()
        row = mm.MatrixRow.from_bytes(entry[216:244])
        theta[:2] = (row.coef[0, [0, 2]] - self.row.coef[0, [0, 2]]) / 1024
        curves = np.array([list(entry[379:412]), list(entry[82:115]), list(entry[412:445])], float)
        if not entry[1] & 4:
            curves[:] = list(entry[82:115])
        theta[self.qstart : self.qend] = ((curves[1:2] if self.bw else curves) - self.stock_curve).ravel()
        extra = [float((row.coef[0].sum() - self.row.coef[0].sum()) / 1024)]
        if not self.bw:
            extra.extend(((row.coef[1:] - self.row.coef[1:]) / 1024).ravel())
            words = np.array([[knobs.dec(v, 128) for v in entry[i : i + 4]] for i in range(115, 211, 4)])
            z = colour.Z_of_words(words[:, 0], words[:, 1], self.context)
            # 相同整数扇区的物理倍率严格为1。复数除法的微小虚部会把
            # 原本为零的起点变成~1e-17，使TRF初始信赖域退化为零步长。
            z[np.all(words[:, :2] == self.context.stock, axis=1)] = 1
            theta[2:26], theta[26:50] = np.log(np.maximum(abs(z), 1e-6)), np.angle(z)
        theta[self.matrix_start :] = extra
        return np.clip(theta, self.lower + 1e-9, self.upper - 1e-9)

    def _core(self, state: State, y: np.ndarray, ci: np.ndarray, cl: np.ndarray) -> np.ndarray:
        if self.bw:
            return np.repeat(y[:, None], 3, axis=1)
        basis = self.table.kernel_weights(mm.hue_deg(cl), abs(cl))
        multiplier = basis @ self.context.S_eff + (basis * self.table.gain) @ (self.context.S_eff * (state.z - 1))
        return mm.rgb_from_ycc(y, ci * multiplier)

    def _forward(self, state: State, pre: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        ycc = pre @ (state.matrix / 1024).T + self.row.offsets
        y, ci = ycc[:, 0], ycc[:, 1] - 128 + 1j * (ycc[:, 2] - 128)
        lookup = pre @ self.factory.effective(100.0).T + self.factory.offsets
        cl = lookup[:, 1] - 128 + 1j * (lookup[:, 2] - 128)
        return self._core(state, y, ci, cl), y, ci, cl

    def predict(self, theta: np.ndarray, x: np.ndarray, *, quantized: bool = False) -> np.ndarray:
        state = self.state(theta, quantized=quantized)
        curves = np.repeat(state.curves, 3, axis=0) if self.nc == 1 else state.curves
        pre = np.einsum("ncj,cj->nc", weights(x, self.axis), curves)
        return np.asarray(np.clip(self._forward(state, pre)[0], 0, 255))

    def constraints(self, theta: np.ndarray) -> np.ndarray:
        state = self.state(theta)
        q = state.curves
        result = [
            2 * np.minimum(np.diff(q, axis=1), 0).ravel(),
            2 * np.minimum(q, 0).ravel(),
            2 * np.maximum(q - 255, 0).ravel(),
            2 * np.minimum(q[:, -1] - 128, 0),
        ]
        if not self.bw:
            physical = self.context.S_eff * np.exp(theta[2:26] + 1j * theta[26:50]) * 32
            a, b = physical.real - self.context.d0, -physical.imag - self.context.d1
            result.append(2 * np.maximum(abs(np.r_[a, b]) - 126.8, 0))
        result.append(5 * np.maximum(abs(state.matrix.ravel()) - 2047, 0) / 1024)
        return np.concatenate(result)

    def _loss(self, rgb: np.ndarray) -> np.ndarray:
        lab = colour.lab_from_codes(np.clip(rgb, 0, 255))
        if self.bw:
            mean = (self.target_lab[:, 0] + lab[:, 0]) / 2
            scale = 1 + 0.015 * (mean - 50) ** 2 / np.sqrt(20 + (mean - 50) ** 2)
            result = np.zeros_like(lab)
            result[:, 0] = (lab[:, 0] - self.target_lab[:, 0]) / scale
            return np.asarray(result * self.sqrtw)
        return np.asarray(colour.de00_components(self.target_lab, lab) * self.sqrtw)

    def evaluate(self, theta: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.cache is not None and np.array_equal(self.cache[0], theta):
            return self.cache[1], self.cache[2]
        state = self.state(theta)
        curves = np.repeat(state.curves, 3, axis=0) if self.nc == 1 else state.curves
        pre = np.einsum("ncj,cj->nc", self.basis, curves)
        raw, y, ci, cl = self._forward(state, pre)
        eps = 1e-3
        loss_jac = np.empty((len(raw), 3, 3))
        for channel in range(3):
            delta = np.zeros_like(raw)
            delta[:, channel] = eps
            loss_jac[:, :, channel] = (self._loss(raw + delta) - self._loss(raw - delta)) / (2 * eps)
        core_jac = np.empty((len(raw), 3, 3))
        core_jac[:, :, 0] = (self._core(state, y + eps, ci, cl) - self._core(state, y - eps, ci, cl)) / (2 * eps)
        core_jac[:, :, 1] = (self._core(state, y, ci + eps, cl) - self._core(state, y, ci - eps, cl)) / (2 * eps)
        core_jac[:, :, 2] = (self._core(state, y, ci + 1j * eps, cl) - self._core(state, y, ci - 1j * eps, cl)) / (2 * eps)
        jac = np.zeros((len(raw), 3, self.size))
        jac[:, :, 0] = core_jac[:, :, 0] * (pre[:, 0] - pre[:, 1])[:, None]
        jac[:, :, 1] = core_jac[:, :, 0] * (pre[:, 2] - pre[:, 1])[:, None]
        dpre = np.einsum("nrc,cd->nrd", core_jac, state.matrix / 1024)
        if not self.bw:
            basis = self.table.kernel_weights(mm.hue_deg(cl), abs(cl))
            dc = ci[:, None] * (basis * self.table.gain) * (self.context.S_eff * state.z)[None, :]
            jac[:, :, 2:26], jac[:, :, 26:50] = rgb_jacobian(dc), rgb_jacobian(1j * self.table.k * dc)
            lookup_jac = np.empty((len(raw), 3, 2))
            lookup_jac[:, :, 0] = (self._core(state, y, ci, cl + eps) - self._core(state, y, ci, cl - eps)) / (2 * eps)
            lookup_jac[:, :, 1] = (self._core(state, y, ci, cl + 1j * eps) - self._core(state, y, ci, cl - 1j * eps)) / (2 * eps)
            dpre += np.einsum("nrc,cd->nrd", lookup_jac, self.factory.effective(100.0)[1:])
        for channel in range(3):
            start = self.qstart + (channel * 33 if self.nc == 3 else 0)
            jac[:, :, start : start + 33] += dpre[:, :, channel, None] * self.basis[:, channel, None, :]
        jac[:, :, self.matrix_start] = core_jac[:, :, 0] * pre[:, 1, None]
        if not self.bw:
            for row in range(2):
                for channel in range(3):
                    jac[:, :, self.matrix_start + 1 + row * 3 + channel] = core_jac[:, :, row + 1] * pre[:, channel, None]
        data_jac = np.einsum("nrc,ncp->nrp", loss_jac, jac).reshape(-1, self.size)
        constraints = self.constraints(theta)
        constraint_jac = np.empty((len(constraints), self.size))
        for j in range(self.size):
            delta = np.zeros(self.size)
            delta[j] = 1e-5
            constraint_jac[:, j] = (self.constraints(theta + delta) - self.constraints(theta - delta)) / 2e-5
        residual = np.r_[self._loss(raw).ravel(), self.regularizer @ (theta - self.theta0), constraints]
        derivative = np.vstack([data_jac, self.regularizer, constraint_jac])
        self.raw_state = theta.copy(), raw.copy(), jac.copy()
        self.cache = theta.copy(), residual, derivative
        return residual, derivative

    def encode(self, theta: np.ndarray) -> bytes:
        state = self.state(theta, quantized=True)
        entry = bytearray(self.training.template)
        if self.bw:
            entry[0] = 3
        entry[1] = 1 if self.bw else 7
        entry[445:447] = struct.pack(">H", 2 if self.bw else 0)
        entry[20:82] = struct.pack(">31H", *self.axis[1:-1].astype(int))
        curves = np.repeat(state.curves, 3, axis=0) if self.bw else state.curves
        entry[82:115] = bytes(curves[1].tolist())
        if not self.bw:
            entry[379:412], entry[412:445] = bytes(curves[0].tolist()), bytes(curves[2].tolist())
        entry[456:518], entry[518:551] = entry[20:82], entry[82:115]
        entry[216:244] = mm.MatrixRow(state.matrix, self.row.tail).to_bytes()
        if self.bw:
            second = mm.MatrixRow.from_bytes(entry[244:272])
            coefficients = second.coef.copy()
            coefficients[1:] = 0
            entry[244:272] = mm.MatrixRow(coefficients, second.tail).to_bytes()
            entry[115:211] = bytes(96)
        else:
            entry[115:211] = b"".join(colour.encode_word(int(a), int(b)) for a, b in zip(state.sector_a, state.sector_b))
        knobs.checksum(entry)
        result = bytes(entry)
        check_record(result)
        return result

    def parameters(self, theta: np.ndarray) -> dict[str, Any]:
        state = self.state(theta, quantized=True)
        return {
            "curve_x_u12": self.axis.astype(int).tolist(),
            "curve_y_u8": state.curves.tolist(),
            "matrix_q10": state.matrix.tolist(),
            "sector_a": state.sector_a.tolist(),
            "sector_b": state.sector_b.tolist(),
        }
