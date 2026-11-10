"""优化器只获得TrainingInput，其中没有路径或留出加载能力。"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..camdata.unit_model import input_codes
from ..recipes import RecipeError, RenderEnvironment
from ..recipes.types import canonical, digest

GROUPS = ("train", "synthetic", "photo_iso200", "photo_other_iso", "new7_RAW_target")
MAX_ARRAY_BYTES = 64 << 20


def immutable(array: np.ndarray) -> np.ndarray:
    return np.frombuffer(array.tobytes(order="C"), dtype=array.dtype).reshape(array.shape)


@dataclass(frozen=True)
class SampleGroup:
    raw_wb: np.ndarray
    x: np.ndarray
    xyz: np.ndarray
    want: np.ndarray
    base_new: np.ndarray
    sw: np.ndarray
    photo_id: np.ndarray
    iso: np.ndarray

    @classmethod
    def from_samples(
        cls,
        environment: RenderEnvironment,
        *,
        raw_wb: np.ndarray,
        xyz: np.ndarray,
        sw: np.ndarray,
        photo_id: np.ndarray,
        iso: np.ndarray,
    ) -> SampleGroup:
        """封存数值采样转为可重放语料；尚未对任何新LUT声称质量通过。"""
        model = environment.model_for(200)[0]
        mapped = input_codes(np.asarray(raw_wb, dtype="<f8"), model.input_coefficients)
        base = model.predict(environment.stock, raw_wb)
        result = cls(raw_wb, mapped, xyz, base, base, sw, photo_id, iso)
        return cls.from_bytes(result.to_bytes())

    @classmethod
    def from_bytes(cls, data: bytes) -> SampleGroup:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(i.file_size for i in archive.infolist()) > MAX_ARRAY_BYTES or len(archive.infolist()) != 8:
                raise RecipeError("bad_quality_arrays", "质量输入数组的大小或成员数量无效。")
        with np.load(io.BytesIO(data), allow_pickle=False) as loaded:
            names = ("raw_wb", "x", "xyz", "want", "base_new", "sw", "photo_id", "iso")
            if set(loaded.files) != set(names):
                raise RecipeError("bad_quality_arrays", "质量输入缺少必需数组。")
            arrays = []
            for key in names:
                array = loaded[key]
                if array.dtype.str != ("<i8" if key in ("photo_id", "iso") else "<f8"):
                    raise RecipeError("bad_quality_arrays", "质量输入需要固定的小端float64和int64数组。")
                arrays.append(immutable(array))
        result = cls(*arrays)
        n = len(result.raw_wb)
        if not n or any(a.shape != (n, 3) for a in arrays[:5]) or any(a.shape != (n,) for a in arrays[5:]):
            raise RecipeError("bad_quality_arrays", "质量输入的样本维度不一致或为空。")
        if not all(np.isfinite(a).all() for a in arrays) or np.any(result.sw < 0) or not np.any(result.sw > 0):
            raise RecipeError("bad_quality_arrays", "质量输入含非有限值或无效权重。")
        if np.any((result.raw_wb < 0) | (result.raw_wb > 4095)) or np.any((result.want < 0) | (result.want > 255)):
            raise RecipeError("bad_quality_arrays", "质量输入的原码值或目标超出定义域。")
        return result

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        values = {name: getattr(self, name) for name in self.__dataclass_fields__}
        arrays = {k: np.asarray(v, dtype="<i8" if k in ("photo_id", "iso") else "<f8") for k, v in values.items()}
        np.savez(
            buf,
            raw_wb=arrays["raw_wb"],
            x=arrays["x"],
            xyz=arrays["xyz"],
            want=arrays["want"],
            base_new=arrays["base_new"],
            sw=arrays["sw"],
            photo_id=arrays["photo_id"],
            iso=arrays["iso"],
            allow_pickle=False,
        )
        return buf.getvalue()

    def verify_model(self, environment: RenderEnvironment) -> None:
        model = environment.model_for(200)[0]
        if not np.array_equal(self.x, input_codes(self.raw_wb, model.input_coefficients)):
            raise RecipeError("input_domain_mismatch", "映射后码值与原白平衡码值不符，不能重复映射。")
        expected = model.predict(environment.stock, self.raw_wb)
        if np.max(abs(expected - self.base_new)) >= 1e-8:
            raise RecipeError("quality_model_mismatch", "质量输入的原厂04参照与冻结模型不一致。")


@dataclass(frozen=True)
class TrainingInput:
    unit: str
    slot: int
    family: str
    samples: SampleGroup
    environment: RenderEnvironment
    template: bytes
    original: bytes
    seeds: tuple[bytes, ...]
    plan: bytes

    @property
    def identity(self) -> str:
        return digest(
            canonical(
                {
                    "unit": self.unit,
                    "slot": self.slot,
                    "family": self.family,
                    "train": digest(self.samples.to_bytes()),
                    "environment": self.environment.identity,
                    "template": digest(self.template),
                    "original": digest(self.original),
                    "seeds": [digest(s) for s in self.seeds],
                    "plan": json.loads(self.plan),
                }
            )
        )


@dataclass(frozen=True)
class FrozenContext:
    identity: str
    manifest: bytes
    training: TrainingInput

    @property
    def document(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.manifest)
        return value
