"""Bounded .cube reader: RGB-fast ordering, per-channel domains, 1D shaper + 3D."""

from __future__ import annotations

import hashlib
import math
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import numpy as np

MAX_BYTES = 64 * 1024 * 1024
MAX_1D = 65536
MAX_3D = 129


@dataclass
class Cube:
    table1: np.ndarray | None
    table3: np.ndarray | None
    domain_min: np.ndarray
    domain_max: np.ndarray
    range1: tuple[Any, ...] = (0.0, 1.0)
    range3: tuple[Any, ...] = (0.0, 1.0)
    title: str = ""
    source_sha256: str = ""

    @classmethod
    def read(cls, path: str | Path) -> Cube:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > MAX_BYTES:
            raise ValueError("cube 文件不存在或超过 64 MiB")
        raw = p.read_bytes()
        return cls.parse(raw.decode("utf-8-sig"), hashlib.sha256(raw).hexdigest())

    @classmethod
    def parse(cls, text: str, digest: str = "") -> Cube:
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise ValueError("cube 超过 64 MiB")
        headers: dict[str, Any] = {}
        data: list[list[float]] = []
        counts = {"LUT_1D_SIZE": (2, MAX_1D), "LUT_3D_SIZE": (2, MAX_3D)}
        known = {*counts, "DOMAIN_MIN", "DOMAIN_MAX", "TITLE", "LUT_1D_INPUT_RANGE", "LUT_3D_INPUT_RANGE"}
        for no, line in enumerate(text.splitlines(), 1):
            words = shlex.split(line, comments=True, posix=True) if line.lstrip().startswith("TITLE") else line.split("#", 1)[0].split()
            if not words:
                continue
            key = words[0]
            if key in known:
                if data or key in headers:
                    raise ValueError(f"line {no}: duplicate or late header {key}")
                values = words[1:]
                if key == "TITLE":
                    if len(values) != 1:
                        raise ValueError(f"line {no}: TITLE needs one quoted string")
                    headers[key] = values[0]
                elif key in counts:
                    if len(values) != 1:
                        raise ValueError(f"line {no}: invalid LUT size")
                    n = int(values[0])
                    if not counts[key][0] <= n <= counts[key][1]:
                        raise ValueError(f"line {no}: LUT size out of range")
                    headers[key] = n
                else:
                    size = 3 if key.startswith("DOMAIN") else 2
                    if len(values) != size:
                        raise ValueError(f"line {no}: invalid {key}")
                    v = np.array([float(s) for s in values])
                    if not np.isfinite(v).all():
                        raise ValueError(f"line {no}: non-finite header")
                    headers[key] = v
                continue
            try:
                row = [float(s) for s in words]
            except ValueError as exc:
                raise ValueError(f"line {no}: unsupported directive {key}") from exc
            if len(row) != 3 or not all(math.isfinite(v) for v in row):
                raise ValueError(f"line {no}: expected three finite numbers")
            n1, n3 = headers.get("LUT_1D_SIZE", 0), headers.get("LUT_3D_SIZE", 0)
            if not n1 and not n3 or len(data) >= n1 + n3**3:
                raise ValueError(f"line {no}: unexpected data row")
            data.append(row)
        n1, n3 = headers.get("LUT_1D_SIZE", 0), headers.get("LUT_3D_SIZE", 0)
        if not (n1 or n3) or len(data) != n1 + n3**3:
            raise ValueError("LUT 数据行数与声明不符")
        lo, hi = (headers.get("DOMAIN_" + k, np.full(3, default)) for k, default in (("MIN", 0.0), ("MAX", 1.0)))
        if np.any(hi <= lo):
            raise ValueError("DOMAIN_MAX 必须大于 DOMAIN_MIN")
        r1 = tuple(headers.get("LUT_1D_INPUT_RANGE", [0.0, 1.0]))
        r3 = tuple(headers.get("LUT_3D_INPUT_RANGE", [0.0, 1.0]))
        if r1[1] <= r1[0] or r3[1] <= r3[0]:
            raise ValueError("INPUT_RANGE 必须递增")
        if ("DOMAIN_MIN" in headers or "DOMAIN_MAX" in headers) and ("LUT_1D_INPUT_RANGE" in headers or "LUT_3D_INPUT_RANGE" in headers):
            raise ValueError("DOMAIN 与 INPUT_RANGE 同时声明时含义不明确，拒绝猜测")
        rows = np.asarray(data)
        return cls(
            rows[:n1] if n1 else None,
            rows[n1:].reshape(n3, n3, n3, 3) if n3 else None,
            lo,
            hi,
            r1,
            r3,
            headers.get("TITLE", ""),
            digest,
        )

    def apply(self, rgb: Any, return_info: bool = False) -> Any:
        x = np.asarray(rgb, float)
        if x.shape[-1:] != (3,) or not np.isfinite(x).all():
            raise ValueError("LUT 输入必须为有限 RGB")
        shape = x.shape
        x = x.reshape(-1, 3)
        x = (x - self.domain_min) / (self.domain_max - self.domain_min)
        clipped = np.zeros(len(x), bool)
        if self.table1 is not None:
            x = (x - self.range1[0]) / (self.range1[1] - self.range1[0])
            clipped |= np.any((x < 0) | (x > 1), axis=1)
            nodes = np.linspace(0, 1, len(self.table1))
            x = np.stack([np.interp(x[:, c], nodes, self.table1[:, c]) for c in range(3)], axis=-1)
        if self.table3 is not None:
            x = (x - self.range3[0]) / (self.range3[1] - self.range3[0])
            clipped |= np.any((x < 0) | (x > 1), axis=1)
            x = self.tetrahedral(np.clip(x, 0, 1))
        y = x.reshape(shape)
        return (y, {"input_clipped_share": float(clipped.mean())}) if return_info else y

    def tetrahedral(self, x: np.ndarray) -> np.ndarray:
        table3 = self.table3
        if table3 is None:
            raise ValueError("LUT 没有 3D 表")
        n = len(table3)
        pos = x * (n - 1)
        base = np.minimum(pos.astype(int), n - 2)
        f = pos - base
        order = np.argsort(-f, axis=1, kind="stable")
        idx = np.arange(len(x))
        b = base.copy()

        def at(coords: Any) -> Any:
            # .cube writes R fastest, then G, then B.
            return table3[coords[:, 2], coords[:, 1], coords[:, 0]]

        v0 = at(b)
        b[idx, order[:, 0]] += 1
        v1 = at(b)
        b[idx, order[:, 1]] += 1
        v2 = at(b)
        b[idx, order[:, 2]] += 1
        v3 = at(b)
        u = np.take_along_axis(f, order, axis=1)
        return cast(np.ndarray, v0 + u[:, 0, None] * (v1 - v0) + u[:, 1, None] * (v2 - v1) + u[:, 2, None] * (v3 - v2))

    def description(self) -> dict[Any, Any]:
        return {
            "title": self.title,
            "sha256": self.source_sha256,
            "size_1d": 0 if self.table1 is None else len(self.table1),
            "size_3d": 0 if self.table3 is None else len(self.table3),
            "domain_min": self.domain_min,
            "domain_max": self.domain_max,
            "range_1d": self.range1,
            "range_3d": self.range3,
            "interpolation": "tetrahedral",
            "out_of_domain": "clamp at each stage, record share; preserve finite LUT output until display conversion",
        }


def write_cube(path: Any, fun: Any, size: int = 33, title: str = "synthetic") -> None:
    """Test fixture writer."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    a = np.linspace(0, 1, size)
    b, g, r = np.meshgrid(a, a, a, indexing="ij")
    y = fun(np.stack([r, g, b], axis=-1).reshape(-1, 3))
    with p.open("w") as f:
        f.write(f'TITLE "{title}"\nLUT_3D_SIZE {size}\n')
        np.savetxt(f, y, fmt="%.10f")
