"""七份历史授权的固定信任根。新拟合入口不能构造历史质量状态。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .types import RecipeError, digest

AUTHORIZATION_SHA256 = "fb28d32605023401ae9bf61da7680affd6774c2437fb29b5a7611964aceaff69"
EQUIVALENCE_SHA256 = "f056bbe52c88b82de516cd82751a118a6ced0a4bc17fb852c48a586e6934d96a"
REGISTRY_SHA256 = "06e68edad93c5775a11653a154a1636fc7e84fba20afb363ff97a62c365bc26f"
HISTORICAL_IDS = frozenset(
    {
        "g178_07_mount-a12",
        "g178_08_a12-50",
        "g178_16_a12-28",
        "g178_16_a12-50",
        "g178_17_a12-50",
        "g178_18_a12-28",
        "g178_18_a12-50",
    }
)
UNITS = {"jlaunch": "a12-50", "qlaunch": "a12-28", "l08firm": "mount-a12"}


@dataclass(frozen=True)
class HistoricalQuality:
    registry_id: str
    unit: str
    position: int
    record_sha256: str
    admission_basis: str
    original_admission_unpassed: tuple[str, ...]
    acceptance_note: str

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": "gxr-studio/historical-quality/1",
            "registry_id": self.registry_id,
            "unit": self.unit,
            "position": self.position,
            "record_sha256": self.record_sha256,
            "admission_basis": self.admission_basis,
            "original_admission_unpassed": list(self.original_admission_unpassed),
            "acceptance_note": self.acceptance_note,
            "authorization_sha256": AUTHORIZATION_SHA256,
            "equivalence_sha256": EQUIVALENCE_SHA256,
            "registry_sha256": REGISTRY_SHA256,
        }

    def summary(self) -> dict[str, Any]:
        return {**self.to_json(), "state": "historical_exception", "label": "历史授权收录，原准入未通过"}

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> HistoricalQuality:
        if value.get("registry_id") not in HISTORICAL_IDS or (
            value.get("authorization_sha256"),
            value.get("equivalence_sha256"),
            value.get("registry_sha256"),
        ) != (AUTHORIZATION_SHA256, EQUIVALENCE_SHA256, REGISTRY_SHA256):
            raise RecipeError("historical_authorization_mismatch", "历史例外的固定证据身份不符。")
        return cls(
            value["registry_id"],
            value["unit"],
            value["position"],
            value["record_sha256"],
            value["admission_basis"],
            tuple(value["original_admission_unpassed"]),
            value["acceptance_note"],
        )


def verify_historical(
    entry: bytes,
    unit: str,
    position: int,
    registry_id: str,
    *,
    authorization: bytes,
    equivalence: bytes,
    registry: bytes,
) -> HistoricalQuality:
    if (digest(authorization), digest(equivalence), digest(registry)) != (AUTHORIZATION_SHA256, EQUIVALENCE_SHA256, REGISTRY_SHA256):
        raise RecipeError("historical_authorization_mismatch", "历史例外只接受正式交接的固定授权和证据。")
    auth, table = json.loads(authorization), json.loads(registry)
    if registry_id not in HISTORICAL_IDS:
        raise RecipeError("historical_identity_mismatch", "这份配方不在七份历史授权中。")
    approved = next(row for row in auth["records"] if row["registry_id"] == registry_id)
    rows = table["records"] if isinstance(table, dict) else table
    record = next(row for row in rows if row["id"] == registry_id)
    if (approved["unit"], approved["position"], approved["materialized_sha256"]) != (UNITS.get(unit), position, digest(entry)):
        raise RecipeError("historical_identity_mismatch", "历史例外的单元、位置或记录摘要不符。")
    if record["record_sha256"] != digest(entry) or not record.get("original_admission_unpassed"):
        raise RecipeError("historical_identity_mismatch", "历史登记与记录或原准入状态不一致。")
    return HistoricalQuality(
        registry_id,
        unit,
        position,
        digest(entry),
        record["admission_basis"],
        tuple(record["original_admission_unpassed"]),
        record["acceptance_note"],
    )
