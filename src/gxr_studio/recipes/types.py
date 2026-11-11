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
class ProofBinding:
    record_sha256: str
    environment_sha256: str
    context_sha256: str
    target_sha256: str
    plan_sha256: str
    receipt_sha256: str
    references_sha256: str

    def __post_init__(self) -> None:
        for item in (
            self.record_sha256,
            self.environment_sha256,
            self.context_sha256,
            self.target_sha256,
            self.plan_sha256,
            self.receipt_sha256,
            self.references_sha256,
        ):
            require_digest(item)

    def to_json(self) -> dict[str, str]:
        return {
            "record_sha256": self.record_sha256,
            "environment_sha256": self.environment_sha256,
            "context_sha256": self.context_sha256,
            "target_sha256": self.target_sha256,
            "plan_sha256": self.plan_sha256,
            "receipt_sha256": self.receipt_sha256,
            "references_sha256": self.references_sha256,
        }

    @classmethod
    def from_json(cls, value: Any) -> ProofBinding:
        expected_keys = {
            "record_sha256",
            "environment_sha256",
            "context_sha256",
            "target_sha256",
            "plan_sha256",
            "receipt_sha256",
            "references_sha256",
        }
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise RecipeError("bad_proof", "质量回执的身份绑定无效。")
        return cls(
            value["record_sha256"],
            value["environment_sha256"],
            value["context_sha256"],
            value["target_sha256"],
            value["plan_sha256"],
            value["receipt_sha256"],
            value["references_sha256"],
        )


@dataclass(frozen=True)
class Quality:
    required: tuple[str, ...]
    checks: tuple[GateResult, ...]
    partial: bool = False
    binding: ProofBinding | None = None

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
        if self.binding is not None and not isinstance(self.binding, ProofBinding):
            raise RecipeError("bad_proof", "质量回执的身份绑定无效。")

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
        payload: dict[str, Any] = {
            "schema": "gxr-studio/full-quality/2" if self.binding is not None else "gxr-studio/full-quality/1",
            "required": list(self.required),
            "checks": [c.to_json() for c in self.checks],
            "partial": self.partial,
        }
        if self.binding is not None:
            payload["binding"] = self.binding.to_json()
        return payload

    def summary(self) -> dict[str, Any]:
        return {"state": self.state, "failures": list(self.failures), "missing": list(self.missing), "label": "电脑预测"}

    @classmethod
    def from_json(cls, value: Any) -> Quality:
        if not isinstance(value, dict):
            raise RecipeError("bad_proof", "质量回执格式无效。")
        schema = value.get("schema")
        if schema == "gxr-studio/full-quality/1":
            if "binding" in value:
                raise RecipeError("bad_proof", "旧版质量回执不能携带未知绑定字段。")
            binding = None
        elif schema == "gxr-studio/full-quality/2":
            binding = ProofBinding.from_json(value.get("binding"))
        else:
            raise RecipeError("bad_proof", "质量回执版本无法识别。")
        required = value.get("required")
        checks = value.get("checks")
        if not isinstance(required, list) or not all(isinstance(k, str) for k in required) or not isinstance(checks, list):
            raise RecipeError("bad_proof", "质量回执缺少检查项列表。")
        try:
            parsed = tuple(GateResult(c["key"], c["state"], canonical(c["detail"])) for c in checks)
        except (KeyError, TypeError, ValueError) as exc:
            raise RecipeError("bad_proof", "质量回执的检查项无效。") from exc
        return cls(
            tuple(required),
            parsed,
            value.get("partial", False),
            binding,
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
