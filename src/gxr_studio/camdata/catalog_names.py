"""封存目录的名称和相机顺序。只含名称，不含配方数据；不依赖用户当前导入哪一代。"""

from collections.abc import Sequence
from dataclasses import dataclass

CAMERA_UNITS = {"28": "qlaunch", "50": "jlaunch", "MT": "l08firm"}
UNIT_LABELS = {"28": "28mm", "50": "50mm", "MT": "MOUNT"}
UNIT_BITS = {"50": 1, "28": 2, "MT": 4}
# 库代数；g178/g181/g182/g185/g250 共用 178 目录
CATALOG_GENERATIONS = {"d25d2f0a68": 176, "6a3a5a3c02": 178, "6adf847602": 178, "0ccc86c19b": 178}

# 对照 r5.LIBRARY_SHA256 与 r6 profile 登记的库；前四项是两代保留的原生配方。
_NATIVE = ("GRPos", "GRNeg", "HC BW", "Std")
_G176_NATIVE = _NATIVE + ("K64", "P160", "K25", "Gold")
BASE_NAMES = _G176_NATIVE  # 库 RV 列表 1..8 基底槽
_G176_HEAD = (
    "Acros",
    "ARRI 709",
    "M9 STD",
    "instax mini",
    "HNCS HiCon",
    "HNCS LowSat",
    "Tri-X 400",
    "Ektachrm 100",
    "Provia 100F",
    "Portra 800",
)
_G176_TAIL = ("F-CC", "F-CN", "Leica F1", "Teal Orange")
_G178 = _NATIVE + (
    "F-CC",
    "F-CN",
    "Provia100F",
    "M9 STD",
    "L-Classic",
    "L-Contemp",
    "Leica F1",
    "K64",
    "K25",
    "Portra160",
    "Gold200",
    "Ektachrm100",
    "HNCS HiCon",
    "HNCS LowSat",
    "Teal Orange",
    "Y2K Plady",
    "Agfa LeBox",
    "Acros",
    "Tri-X 400",
)
_KUBIK_LOOKS = ("K-0815", "K-PACIFIC", "K-KINO")
_KUBIK = _G178 + _KUBIK_LOOKS

# 共享描述符区的全局顺序（pos = effect + 4，effect 为 1-based 下标；各风格按单元掩码 1/2/4 排列）。
_G176_DESCRIPTORS: tuple[tuple[str, int], ...] = (
    ("Acros", 1),
    ("Acros", 2),
    ("Acros", 4),
    ("ARRI 709", 7),
    ("M9 STD", 1),
    ("M9 STD", 2),
    ("M9 STD", 4),
    ("instax mini", 7),
    ("HNCS HiCon", 1),
    ("HNCS HiCon", 2),
    ("HNCS HiCon", 4),
    ("HNCS LowSat", 3),
    ("HNCS LowSat", 4),
    ("Tri-X 400", 7),
    ("Ektachrm 100", 1),
    ("Ektachrm 100", 2),
    ("Ektachrm 100", 4),
    ("Provia 100F", 3),
    ("Provia 100F", 4),
    ("Portra 800", 3),
    ("Portra 800", 4),
    ("Vision3 500T", 1),
    ("Vision3 500T", 4),
    ("F-CC", 5),
    ("F-CC", 2),
    ("F-CN", 3),
    ("F-CN", 4),
    ("Leica F1", 7),
    ("Teal Orange", 7),
)
_G178_DESCRIPTORS: tuple[tuple[str, int], ...] = tuple((name, mask) for name in _G178[4:] for mask in (1, 2, 4))
_KUBIK_DESCRIPTORS: tuple[tuple[str, int], ...] = _G178_DESCRIPTORS + tuple((name, mask) for name in _KUBIK_LOOKS for mask in (1, 2, 4))

CATALOG_DESCRIPTORS: dict[str, tuple[tuple[str, int], ...]] = {
    "d25d2f0a68": _G176_DESCRIPTORS,
    "6a3a5a3c02": _G178_DESCRIPTORS,
    "6adf847602": _G178_DESCRIPTORS,
    "0ccc86c19b": _KUBIK_DESCRIPTORS,
}

CATALOG_NAMES: dict[str, dict[str, tuple[str, ...]]] = {
    "d25d2f0a68": {
        "50": _G176_NATIVE + _G176_HEAD + ("Vision3 500T",) + _G176_TAIL,
        "28": _G176_NATIVE + _G176_HEAD + _G176_TAIL,
        "MT": _G176_NATIVE + _G176_HEAD + ("Vision3 500T",) + _G176_TAIL,
    },
    "6a3a5a3c02": {code: _G178 for code in CAMERA_UNITS},
    "6adf847602": {code: _G178 for code in CAMERA_UNITS},
    "0ccc86c19b": {code: _KUBIK for code in CAMERA_UNITS},
}


@dataclass(frozen=True)
class CatalogTarget:
    kind: str  # "builtin" | "record"
    slot: int | None
    effect: int | None
    name: str


def resolve_descriptors(descriptors: Sequence[tuple[str, int]], unit: str, position: int) -> CatalogTarget | None:
    """相机引擎规则 `pos = effect ? effect + 4 : slot`：1–4 查基底槽；≥5 先试 effect=pos−4 且单元匹配；否则 5–8 回退基底槽；其余 None。"""
    unit_bit = UNIT_BITS.get(unit)
    if unit_bit is None or position < 1:
        return None
    if position <= 4:
        return CatalogTarget("builtin", position, None, BASE_NAMES[position - 1])
    effect = position - 4
    if 1 <= effect <= len(descriptors):
        name, mask = descriptors[effect - 1]
        if mask & unit_bit:
            return CatalogTarget("record", None, effect, name)
    if position <= 8:
        return CatalogTarget("builtin", position, None, BASE_NAMES[position - 1])
    return None


def resolve_catalog_position(identity: str, unit: str, position: int) -> CatalogTarget | None:
    descriptors = CATALOG_DESCRIPTORS.get(identity)
    if descriptors is None:
        return None
    return resolve_descriptors(descriptors, unit, position)


def recipe_name(identity: str, unit: str, position: int) -> str | None:
    target = resolve_catalog_position(identity, unit, position)
    return None if target is None else target.name
