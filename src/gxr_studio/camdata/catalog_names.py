"""封存目录的名称和相机顺序。只含名称，不含配方数据；不依赖用户当前导入哪一代。"""

CAMERA_UNITS = {"28": "qlaunch", "50": "jlaunch", "MT": "l08firm"}
UNIT_LABELS = {"28": "28mm", "50": "50mm", "MT": "MOUNT"}
CATALOG_GENERATIONS = {"d25d2f0a68": 176, "6a3a5a3c02": 178}

# 对照 r5.LIBRARY_SHA256 登记的 g176 / g178 库；前四项是两代保留的原生配方。
_NATIVE = ("GRPos", "GRNeg", "HC BW", "Std")
_G176_NATIVE = _NATIVE + ("K64", "P160", "K25", "Gold")
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

CATALOG_NAMES: dict[str, dict[str, tuple[str, ...]]] = {
    "d25d2f0a68": {
        "50": _G176_NATIVE + _G176_HEAD + ("Vision3 500T",) + _G176_TAIL,
        "28": _G176_NATIVE + _G176_HEAD + _G176_TAIL,
        "MT": _G176_NATIVE + _G176_HEAD + ("Vision3 500T",) + _G176_TAIL,
    },
    "6a3a5a3c02": {code: _G178 for code in CAMERA_UNITS},
}


def recipe_name(identity: str, unit: str, position: int) -> str | None:
    names = CATALOG_NAMES.get(identity, {}).get(unit, ())
    return names[position - 1] if 1 <= position <= len(names) else None
