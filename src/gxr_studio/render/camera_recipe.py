"""拍摄标签提供初始配方；只认同一封存目录，用户的显式选择（含关闭）优先。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..camdata.catalog_names import CAMERA_UNITS, CATALOG_GENERATIONS, recipe_name
from ..camdata.recipe_tags import RecipeTag
from .params import EditError, EditState, RecipeSel, parse_edit
from .recipes import CameraData, Menu


def recorded_recipe(tag: RecipeTag | None, menu: Menu) -> RecipeSel | None:
    if tag is None or recipe_name(tag["catalog_identity"], tag["unit"], tag["position"]) is None:
        return None
    if not isinstance(menu, CameraData) or CATALOG_GENERATIONS.get(tag["catalog_identity"]) != menu.manifest["generation"]:
        raise EditError("camera_recipe_catalog_missing", "请先导入拍摄时使用的相机配方文件。")
    unit = CAMERA_UNITS[tag["unit"]]
    recipes = next(row["recipes"] for row in menu.listing() if row["id"] == unit)
    position = tag["position"]
    if not 1 <= position <= len(recipes):
        return None
    info = recipes[position - 1]
    stored_levels = tag["levels"]
    levels = {name: value for name, value in (stored_levels or {}).items() if value is not None}
    if stored_levels is not None and any(stored_levels.get(control["name"]) is None for control in info["controls"]):
        raise EditError("camera_knobs_mismatch", "相机记录的旋钮与配方目录不匹配。", "缺少可调旋钮的值")
    selection = RecipeSel(unit, info["slot"], tuple(sorted(levels.items())), info.get("kind", "builtin"), info.get("id"))
    menu.request_for(selection)  # 不钳位，也不把目录不支持的非零旋钮吞掉。
    return selection


def photo_edit(side: dict[Any, Any], shot: dict[Any, Any], menu: Menu) -> EditState:
    edit = parse_edit(side)
    # JPEG 已含机内配方颜色；只有 RAW 能重算拍摄旋钮。显式用户选择仍优先。
    if "recipe" in side or "dng" not in shot.get("files", {}):
        return edit
    selection = recorded_recipe(shot.get("recipe_tag"), menu)
    return replace(edit, recipe=selection)


def editor_edit(side: dict[Any, Any], shot: dict[Any, Any], menu: Menu) -> tuple[EditState, EditError | None]:
    """存储状态必须有效；拍摄配方不可预测时，电脑编辑仍可读写。"""
    stored = parse_edit(side)
    try:
        return photo_edit(side, shot, menu), None
    except EditError as problem:
        return stored, problem
