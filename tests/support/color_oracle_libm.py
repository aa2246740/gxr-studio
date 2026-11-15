"""Pinned `gxr_math`/libm stream client and Cephes `ndtr`/`erfc` sequential math for `color_oracle`."""

from __future__ import annotations

import atexit
import math
import os
import struct
import subprocess  # nosec B404
import threading
from typing import Any

import numpy as np

# Cephes ndtr polynomials matching core/crates/gxr-color/src/sector.rs
_T = (
    9.60497373987051638749e0,
    9.00260197203842689217e1,
    2.23200534594684319226e3,
    7.00332514112805075473e3,
    5.55923013010394962768e4,
)
_U = (
    3.35617141647503099647e1,
    5.21357949780152679795e2,
    4.59432382970980127987e3,
    2.26290000613890934246e4,
    4.92673942608635921086e4,
)
_P = (
    2.46196981473530512524e-10,
    5.64189564831068821977e-1,
    7.46321056442269912687e0,
    4.86371970985681366614e1,
    1.96520832956077098242e2,
    5.26445194995477358631e2,
    9.34528527171957607540e2,
    1.02755188689515710272e3,
    5.57535335369399327526e2,
)
_Q = (
    1.32281951154744992508e1,
    8.67072140885989742329e1,
    3.54937778887819891062e2,
    9.75708501743205489753e2,
    1.82390916687909736289e3,
    2.24633760818710981792e3,
    1.65666309194161350182e3,
    5.57535340817727675546e2,
)
_R = (
    5.64189583547755073984e-1,
    1.27536670759978104416e0,
    5.01905042251180477414e0,
    6.16021097993053585195e0,
    7.40974269950448939160e0,
    2.97886665372100240670e0,
)
_S = (
    2.26052863220117276590e0,
    9.39603524938001434673e0,
    1.20489539808096656605e1,
    1.70814450747565897222e1,
    9.60896809063285878198e0,
    3.36907645100081516050e0,
)
_MAXLOG = 7.09782712893383996843e2
_SQRTH = math.sqrt(0.5)  # std::f64::consts::FRAC_1_SQRT_2
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)

_OP_EXP = 4
_OP_SIN = 5
_OP_COS = 6
_OP_ATAN2 = 7
_OP_HYPOT = 8


class _ColorLibmStream:
    """Probes `gxr-cli` (`color-libm` / `lut-libm --stream`) for gxr_math exp/sin/cos/atan2/hypot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._subcmd: str | None = None
        self._probed = False
        self._supported = False

    def _spawn(self, subcmd: str) -> subprocess.Popen[bytes]:
        from support.core_cli import CLI

        if not CLI.is_file() or not os.access(CLI, os.X_OK):
            raise RuntimeError(f"gxr-cli binary not built at {CLI}")
        return subprocess.Popen(  # noqa: S603  # nosec B603
            [str(CLI), subcmd, "--stream"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _raw_call(self, proc: subprocess.Popen[bytes], op_tag: int, values: np.ndarray, out_len: int) -> np.ndarray | None:
        payload = struct.pack("<BdQ", op_tag, 0.0, values.size) + values.tobytes()
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except OSError:
            return None
        need = out_len * 8
        chunks = []
        rem = need
        while rem > 0:
            chunk = proc.stdout.read(rem)
            if not chunk:
                return None
            chunks.append(chunk)
            rem -= len(chunk)
        return np.frombuffer(b"".join(chunks), dtype="<f8").copy()

    def probe(self) -> bool:
        with self._lock:
            if self._probed:
                return self._supported
            self._probed = True
            for subcmd in ("color-libm", "lut-libm"):
                proc = None
                try:
                    proc = self._spawn(subcmd)
                    one = self._raw_call(proc, _OP_EXP, np.array([0.0], dtype="<f8"), 1)
                    if one is None or len(one) != 1 or one[0] != 1.0:
                        self._kill_proc(proc)
                        continue
                    two = self._raw_call(proc, _OP_ATAN2, np.array([1.0, 0.0], dtype="<f8"), 1)
                    if two is None or len(two) != 1 or not math.isclose(float(two[0]), math.pi / 2.0, rel_tol=1e-15):
                        self._kill_proc(proc)
                        continue
                    for tag in (_OP_SIN, _OP_COS):
                        chk = self._raw_call(proc, tag, np.array([0.0], dtype="<f8"), 1)
                        if chk is None or len(chk) != 1:
                            self._kill_proc(proc)
                            proc = None
                            break
                    if proc is None:
                        continue
                    chk_h = self._raw_call(proc, _OP_HYPOT, np.array([3.0, 4.0], dtype="<f8"), 1)
                    if chk_h is None or len(chk_h) != 1 or chk_h[0] != 5.0:
                        self._kill_proc(proc)
                        continue
                    self._proc = proc
                    self._subcmd = subcmd
                    self._supported = True
                    return True
                except Exception:
                    if proc is not None:
                        self._kill_proc(proc)
            return False

    @staticmethod
    def _kill_proc(proc: subprocess.Popen[bytes]) -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

    def _call_unary(self, tag: int, x: np.ndarray) -> np.ndarray:
        arr = np.ascontiguousarray(x, dtype="<f8")
        if arr.size == 0:
            return np.empty(arr.shape, dtype=np.float64)
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                assert self._subcmd is not None
                self._proc = self._spawn(self._subcmd)
            out = self._raw_call(self._proc, tag, arr, arr.size)
            if out is None:
                raise RuntimeError("gxr-cli libm stream exited unexpectedly")
            return out.reshape(arr.shape)

    def _call_binary(self, tag: int, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        a1, b1 = np.broadcast_arrays(np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64))
        if a1.size == 0:
            return np.empty(a1.shape, dtype=np.float64)
        pairs = np.empty((a1.size, 2), dtype="<f8")
        pairs[:, 0] = a1.reshape(-1)
        pairs[:, 1] = b1.reshape(-1)
        flat = np.ascontiguousarray(pairs.reshape(-1), dtype="<f8")
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                assert self._subcmd is not None
                self._proc = self._spawn(self._subcmd)
            out = self._raw_call(self._proc, tag, flat, a1.size)
            if out is None:
                raise RuntimeError("gxr-cli libm stream exited unexpectedly")
            return out.reshape(a1.shape)

    def close(self) -> None:
        with self._lock:
            if self._proc is not None:
                self._kill_proc(self._proc)
                self._proc = None


_COLOR_LIBM = _ColorLibmStream()
atexit.register(_COLOR_LIBM.close)


def color_libm_available() -> bool:
    """True when `gxr-cli` (`color-libm` or `lut-libm --stream`) exposes exp/sin/cos/atan2/hypot."""
    return _COLOR_LIBM.probe()


def libm_backend() -> str:
    """Name of the active elementary-function backend when `use_libm=True`."""
    if _COLOR_LIBM.probe():
        assert _COLOR_LIBM._subcmd is not None
        return _COLOR_LIBM._subcmd
    return "host-math-fallback"


def _elem_exp(x: Any, *, use_libm: bool) -> np.ndarray:
    arr = np.asarray(x, dtype=np.float64)
    if not use_libm:
        return np.exp(arr)
    if _COLOR_LIBM.probe():
        return _COLOR_LIBM._call_unary(_OP_EXP, arr)
    return np.fromiter((math.exp(float(v)) for v in arr.reshape(-1)), dtype=np.float64, count=arr.size).reshape(arr.shape)


def _elem_cos(x: Any, *, use_libm: bool) -> np.ndarray:
    arr = np.asarray(x, dtype=np.float64)
    if not use_libm:
        return np.cos(arr)
    if _COLOR_LIBM.probe():
        return _COLOR_LIBM._call_unary(_OP_COS, arr)
    return np.fromiter((math.cos(float(v)) for v in arr.reshape(-1)), dtype=np.float64, count=arr.size).reshape(arr.shape)


def _elem_sin(x: Any, *, use_libm: bool) -> np.ndarray:
    arr = np.asarray(x, dtype=np.float64)
    if not use_libm:
        return np.sin(arr)
    if _COLOR_LIBM.probe():
        return _COLOR_LIBM._call_unary(_OP_SIN, arr)
    return np.fromiter((math.sin(float(v)) for v in arr.reshape(-1)), dtype=np.float64, count=arr.size).reshape(arr.shape)


def _elem_atan2(y: Any, x: Any, *, use_libm: bool) -> np.ndarray:
    y1, x1 = np.broadcast_arrays(np.asarray(y, dtype=np.float64), np.asarray(x, dtype=np.float64))
    if not use_libm:
        return np.arctan2(y1, x1)
    if _COLOR_LIBM.probe():
        return _COLOR_LIBM._call_binary(_OP_ATAN2, y1, x1)
    return np.fromiter(
        (math.atan2(float(yy), float(xx)) for yy, xx in zip(y1.reshape(-1), x1.reshape(-1))),
        dtype=np.float64,
        count=y1.size,
    ).reshape(y1.shape)


def _elem_hypot(x: Any, y: Any, *, use_libm: bool) -> np.ndarray:
    x1, y1 = np.broadcast_arrays(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
    if not use_libm:
        return np.hypot(x1, y1)
    if _COLOR_LIBM.probe():
        return _COLOR_LIBM._call_binary(_OP_HYPOT, x1, y1)
    return np.fromiter(
        (math.hypot(float(xx), float(yy)) for xx, yy in zip(x1.reshape(-1), y1.reshape(-1))),
        dtype=np.float64,
        count=x1.size,
    ).reshape(x1.shape)


def _polevl(x: np.ndarray, coef: tuple[float, ...]) -> np.ndarray:
    ans = np.full(x.shape, coef[0], dtype=np.float64)
    for c in coef[1:]:
        ans = ans * x + c
    return ans


def _p1evl(x: np.ndarray, coef: tuple[float, ...]) -> np.ndarray:
    ans = x + coef[0]
    for c in coef[1:]:
        ans = ans * x + c
    return ans


def _erfc_vec(a: np.ndarray, *, use_libm: bool) -> np.ndarray:
    x = np.abs(a)
    z = (-a) * a
    out = np.empty(a.shape, dtype=np.float64)
    under = z < -_MAXLOG
    if np.any(under):
        out[under] = np.where(a[under] < 0.0, 2.0, 0.0)
    ok = ~under
    if np.any(ok):
        x_ok = x[ok]
        a_ok = a[ok]
        z_ok = _elem_exp(z[ok], use_libm=use_libm)
        p = np.empty(x_ok.shape, dtype=np.float64)
        q = np.empty(x_ok.shape, dtype=np.float64)
        lt8 = x_ok < 8.0
        if np.any(lt8):
            p[lt8] = _polevl(x_ok[lt8], _P)
            q[lt8] = _p1evl(x_ok[lt8], _Q)
        ge8 = ~lt8
        if np.any(ge8):
            p[ge8] = _polevl(x_ok[ge8], _R)
            q[ge8] = _p1evl(x_ok[ge8], _S)
        y = (z_ok * p) / q
        neg = a_ok < 0.0
        y = np.where(neg, 2.0 - y, y)
        y = np.where(y == 0.0, np.where(neg, 2.0, 0.0), y)
        out[ok] = y
    return out


def _ndtr_vec(a: np.ndarray, *, use_libm: bool) -> np.ndarray:
    x = a * _SQRTH
    z = np.abs(x)
    out = np.empty(a.shape, dtype=np.float64)
    is_nan = np.isnan(a)
    out[is_nan] = np.nan
    mid = (~is_nan) & (z < _SQRTH)
    if np.any(mid):
        xm = x[mid]
        zm = xm * xm
        out[mid] = 0.5 + 0.5 * (xm * _polevl(zm, _T) / _p1evl(zm, _U))
    tail = (~is_nan) & (~mid)
    if np.any(tail):
        zt = z[tail]
        xt = x[tail]
        y = np.empty(zt.shape, dtype=np.float64)
        sub1 = zt < 1.0
        if np.any(sub1):
            xs = zt[sub1]
            z2 = xs * xs
            y[sub1] = 0.5 * (1.0 - xs * _polevl(z2, _T) / _p1evl(z2, _U))
        ge1 = ~sub1
        if np.any(ge1):
            y[ge1] = 0.5 * _erfc_vec(zt[ge1], use_libm=use_libm)
        out[tail] = np.where(xt > 0.0, 1.0 - y, y)
    return out


def _phi_vec(t: np.ndarray, *, use_libm: bool) -> np.ndarray:
    return _elem_exp((-0.5 * t) * t, use_libm=use_libm) * _INV_SQRT_2PI


def _ramp_vec(d: np.ndarray, sigma: np.ndarray, *, use_libm: bool) -> np.ndarray:
    t = d / sigma
    return d * _ndtr_vec(t, use_libm=use_libm) + sigma * _phi_vec(t, use_libm=use_libm)
