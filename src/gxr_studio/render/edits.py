"""sidecar 里配方、旋钮、调整和曲线的读写（裁切的读写在 `library/edits.py`）。

- 相机侧（会跟着配方进相机）：`recipe`（底子 + 旋钮档位）；
- 电脑侧（只在电脑里生效）：`adjust`（曝光、色温色调、画面对比/高光/阴影）和 `curve`；
- 其他键（`crop`、`import`…）原样保留；写入走 sidecar 的原子写，`rev` 加一，带 `expected_rev` 时做乐观并发检查。
"""

from __future__ import annotations

from ..library import sidecar as sc
from ..library.edits import RevisionConflict
from ..library.store import Library
from .params import EditError, EditState, parse_adjust, parse_curve, parse_edit, parse_recipe
from .recipes import Menu, unit_of_lens

_UNSET = object()
SCOPE = {"camera": ["recipe"], "computer_only": ["adjust", "curve", "crop"]}


def read(lib: Library, shot: dict) -> tuple[EditState, dict]:
    with lib.lock:
        side, _ = lib.read_sidecar_or_default(shot)  # 编辑记录坏了/不见了就当没有编辑
    return parse_edit(side), side


def describe(side: dict, edit: EditState) -> dict:
    recipe = side.get("recipe")
    return {
        "rev": int(side.get("rev", 0)),
        "recipe": edit.recipe.to_json() if edit.recipe else None,
        "memory": (recipe or {}).get("memory") or {},
        "adjust": edit.adjust.to_json(),
        "curve": {"master": [list(p) for p in edit.master]} if edit.master else None,
        "scope": SCOPE,
    }


def _memory(raw) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise EditError("bad_recipe", "memory 要是对象")
    out = {}
    for key, levels in raw.items():
        if (
            not isinstance(key, str)
            or "/" not in key
            or not isinstance(levels, dict)
            or any(not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, int) for k, v in levels.items())
        ):
            raise EditError("bad_recipe", 'memory 要是 {"单元/槽位": {旋钮: 档位}}')
        out[key] = dict(levels)
    return out


def write(lib: Library, shot: dict, changes: dict, menu: Menu, *, expected_rev: int | None = None) -> tuple[EditState, dict]:
    """`changes` 里出现的键才会被改：值为 None 表示还原（删掉这一块）。"""
    path = lib.sidecar_path_for(shot)
    with lib.lock:
        side = lib.read_sidecar_for_write(shot)
        sc.check_writable(side)
        if expected_rev is not None and int(side.get("rev", 0)) != expected_rev:
            raise RevisionConflict(int(side.get("rev", 0)))
        new = dict(side)
        if "recipe" in changes:
            raw = changes["recipe"]
            sel = parse_recipe(raw)
            if sel is None:
                new.pop("recipe", None)
            else:
                photo_unit = unit_of_lens(shot.get("lens"))
                if photo_unit is not None and sel.unit != photo_unit:
                    raise EditError("unit_mismatch", f"这张照片是 {photo_unit} 单元拍的，只能用同一个单元的配方")
                menu.request_for(sel)  # 档位不合法（含到顶标记）就拒绝，不钳位
                memory = _memory(raw.get("memory")) if "memory" in raw else (side.get("recipe") or {}).get("memory") or {}
                new["recipe"] = {**sel.to_json(), **({"memory": memory} if memory else {})}
        if "adjust" in changes:
            adjust = parse_adjust(changes["adjust"])
            if adjust.is_identity:
                new.pop("adjust", None)
            else:
                new["adjust"] = adjust.to_json()
        if "curve" in changes:
            master = parse_curve(changes["curve"])
            if master is None:
                new.pop("curve", None)
            else:
                new["curve"] = {"master": [list(p) for p in master]}
        out = sc.write_atomic(path, new, tmp_dir=lib.tmp_dir)
        lib.set_edit_cache(shot["shot_id"], int(out["rev"]), out.get("crop"))
        return parse_edit(out), out
