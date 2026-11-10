"""完整记录、冻结依赖和证据。名称不参与内容身份。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ..camdata.unit_model import UnitModel
from ..render.params import RecipeSel

if TYPE_CHECKING:
    from .history import HistoricalQuality

GateState = Literal["pass", "fail", "missing"]


class RecipeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require_digest(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RecipeError("bad_digest", "完整配方的内容身份无效。")
    return value


@dataclass(frozen=True)
class GateResult:
    key: str
    state: GateState
    detail: bytes = b"{}"

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "state": self.state, "detail": json.loads(self.detail)}


@dataclass(frozen=True)
class Quality:
    required: tuple[str, ...]
    checks: tuple[GateResult, ...]
    partial: bool = False

    def __post_init__(self) -> None:
        keys = tuple(c.key for c in self.checks)
        if not self.required or len(set(self.required)) != len(self.required) or len(set(keys)) != len(keys):
            raise RecipeError("bad_proof", "质量回执缺少检查项或包含重复项。")
        if set(keys) - set(self.required) or any(c.state not in ("pass", "fail", "missing") for c in self.checks):
            raise RecipeError("bad_proof", "质量回执的检查项无效。")
        for check in self.checks:
            canonical(json.loads(check.detail))
        if not isinstance(self.partial, bool):
            raise RecipeError("bad_proof", "质量回执的完成状态无效。")

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(c.key for c in self.checks if c.state == "fail")

    @property
    def missing(self) -> tuple[str, ...]:
        actual = {c.key: c for c in self.checks}
        return tuple(k for k in self.required if k not in actual or actual[k].state == "missing") + (
            ("evaluation_incomplete",) if self.partial else ()
        )

    @property
    def state(self) -> str:
        return "rejected" if self.failures else "needs_evidence" if self.missing else "passed"

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": "gxr-studio/full-quality/1",
            "required": list(self.required),
            "checks": [c.to_json() for c in self.checks],
            "partial": self.partial,
        }

    def summary(self) -> dict[str, Any]:
        return {"state": self.state, "failures": list(self.failures), "missing": list(self.missing), "label": "电脑预测"}

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> Quality:
        if value.get("schema") != "gxr-studio/full-quality/1":
            raise RecipeError("bad_proof", "质量回执版本无法识别。")
        return cls(
            tuple(value["required"]),
            tuple(GateResult(c["key"], c["state"], canonical(c["detail"])) for c in value["checks"]),
            value.get("partial", False),
        )


@dataclass(frozen=True)
class FitProduct:
    unit: str
    slot: int
    entry: bytes
    environment: bytes
    context: str
    source: bytes
    quality: Quality
    receipt: bytes
    style: str | None = None
    replacement: str | None = None


@dataclass(frozen=True)
class ResolvedRecipe:
    selection: RecipeSel
    entry: bytes
    record_sha256: str
    entry_sha256: str
    binding_sha256: str
    levels: dict[str, int]
    model: UnitModel
    model_iso: int
    calibrated: bool
    quality: Quality | HistoricalQuality
    assessed_entry: bool
