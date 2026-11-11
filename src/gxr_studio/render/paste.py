"""把一张照片的编辑粘贴到别的照片，以及把一批粘贴撤销回去。

粘贴不另造一套写法：每张目标照片都走 `render/edits.write`（配方、LUT、调整、曲线、几何、边框、时间戳）和
`library/edits.write_crop`（裁剪，`fit`），所以结果和在界面上手动改成同样的值完全一样。这里只做三件事：

- 分组：配方（配方 + LUT）、调整（调整 + 曲线）、几何、裁剪、边框、时间戳。没勾的组，目标照片原样不动；
- 逐张判断能不能用：配方是按镜头单元的，LUT 可能需要 RAW。能用的用，不能用的跳过并说明原因，不失败；
- 记下每张照片粘贴之前的内容，供「撤销」整批还原；撤销只在照片还停在粘贴后的那一版时才写。

几何组不包括转 90° 和翻转：那是照片自己的方向，不属于「调整」。
"""

from __future__ import annotations

import copy
import math
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from .. import crop as cropmod
from ..crop import CropError
from ..finish.spec import Geometry, parse_frame, parse_geometry, parse_stamp
from ..library import edits as crop_edits
from ..library import sidecar as sc
from ..library.edits import RevisionConflict
from ..library.store import Library
from ..lut.errors import LutError
from ..recipes import RecipeRepository
from .edits import describe as describe_edit
from .edits import read as read_edit
from .edits import validate_recipe
from .edits import write as write_edit
from .params import EditError, parse_adjust, parse_curve, parse_lut, parse_recipe
from .recipes import Menu, unit_of_lens

GROUPS = ("recipe", "adjust", "geometry", "crop", "frame", "stamp")
# 每一组对应的来源键。来源里一个键都没有的组算「复制时没带上」，跳过；键在而值是 None 表示「这一组是空的」，粘贴会清掉目标的这一组
GROUP_KEYS = {
    "recipe": ("recipe", "lut"),
    "adjust": ("adjust", "curve"),
    "geometry": ("geometry",),
    "crop": ("crop",),
    "frame": ("frame",),
    "stamp": ("stamp",),
}
# sidecar 里这些组占用的键（边框在 sidecar 里叫 border）。撤销按这些键整体还原
SIDECAR_KEYS = ("recipe", "lut", "adjust", "curve", "geometry", "border", "stamp", "crop")
MAX_UNDO_BATCHES = 20


@dataclass(frozen=True)
class Env:
    """目标照片能不能用的判断依据（由路由从服务里取，测试可以直接给）。"""

    menu: Menu
    lut_needs_raw: Callable[[str], bool]  # LUT 不在库里时抛 LutError
    recipe_without_raw: bool = False  # 只有 JPG 的照片能不能用配方（近似模式开着才行）
    recipes: RecipeRepository | None = None


@dataclass(frozen=True)
class Skip:
    group: str
    reason: str
    message: str
    part: str | None = None  # 配方组里只有一半不能用时：recipe 或 lut

    def to_json(self) -> dict[str, Any]:
        return {"group": self.group, "reason": self.reason, "message": self.message, **({"part": self.part} if self.part else {})}


@dataclass
class Outcome:
    id: str
    status: str  # applied（要的组都用上了）| partial | skipped（一组都没用上）| error（这张没处理）
    applied: list[str] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)
    rev: int | None = None  # 写过的话，写完后的版本
    code: str | None = None
    message: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "status": self.status, "applied": self.applied, "skipped": [s.to_json() for s in self.skipped]}
        if self.rev is not None:
            out["rev"] = self.rev
        if self.code:
            out.update(code=self.code, message=self.message)
        return out


def _failed(shot_id: str, code: str, message: str) -> Outcome:
    return Outcome(shot_id, "error", code=code, message=message)


# ---- 来源 -------------------------------------------------------------------------


def _crop_source(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    rect = raw.get("rect") if isinstance(raw, dict) else None
    numbers = (
        isinstance(rect, (list, tuple))
        and len(rect) == 4
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in rect)
    )
    if not isinstance(rect, (list, tuple)) or not numbers or rect[2] <= 0 or rect[3] <= 0 or raw.get("preset") not in cropmod.PRESETS:
        raise EditError("bad_crop", "请求无效。请刷新页面后重试。", "crop 要有 preset 和四个数的 rect")
    return {"preset": raw["preset"], "rect": [float(v) for v in rect]}


def prepare_source(source: Any, groups: list[str]) -> dict[str, Any]:
    """检查来源里要用到的块（格式不对就抛 EditError），返回只含这些块的副本。"""
    if not isinstance(source, dict):
        raise EditError("bad_paste", "请求无效。请刷新页面后重试。", "source 要是对象")
    checks: dict[str, Callable[[Any], Any]] = {
        "recipe": parse_recipe,
        "lut": parse_lut,
        "adjust": parse_adjust,
        "curve": parse_curve,
        "geometry": lambda v: None if v is None else parse_geometry(v),
        "frame": lambda v: None if v is None else parse_frame(v),
        "stamp": lambda v: None if v is None else parse_stamp(v),
        "crop": _crop_source,
    }
    out: dict[str, Any] = {}
    for group in groups:
        for key in GROUP_KEYS[group]:
            if key in source:
                checks[key](source[key])
                out[key] = copy.deepcopy(source[key])
    if "crop" in out:
        out["crop"] = _crop_source(out["crop"])
    return out


def source_from_photo(lib: Library, shot: dict[Any, Any], menu: Menu | None = None) -> dict[str, Any]:
    """用某张照片现在存着的编辑当来源（和界面「复制」得到的内容同样的形状）。"""
    edit, side = read_edit(lib, shot, menu)
    described = describe_edit(side, edit)
    recipe = described["recipe"]
    if recipe and described["memory"]:
        recipe = {**recipe, "memory": described["memory"]}
    try:
        crop_json = crop_edits.describe(side) if side.get("crop") else None
    except CropError:
        crop_json = None
    return {
        "recipe": recipe,
        "lut": described["lut"],
        "adjust": described["adjust"],
        "curve": described["curve"],
        "geometry": described["geometry"],
        "frame": described["frame"],
        "stamp": described["stamp"],
        "crop": {"preset": crop_json["preset"], "rect": crop_json["rect"]} if crop_json else None,
    }


# ---- 一张照片 ---------------------------------------------------------------------


def _recipe_changes(shot: dict[Any, Any], source: dict[str, Any], env: Env, skips: list[Skip]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "recipe" in source:
        raw = source["recipe"]
        sel = parse_recipe(raw)
        files = shot.get("files") or {}
        unit = unit_of_lens(shot.get("lens"))
        if sel is None:
            changes["recipe"] = None
        elif unit is not None and sel.unit != unit:
            skips.append(Skip("recipe", "unit_mismatch", "这张照片的镜头和复制来源不是同一个，没有套用配方。", "recipe"))
        elif "dng" not in files and not env.recipe_without_raw:
            skips.append(Skip("recipe", "needs_raw", "这张照片只有 JPG，没有套用配方。", "recipe"))
        else:
            try:
                validate_recipe(sel, shot, env.menu, env.recipes)
            except EditError as exc:
                skips.append(Skip("recipe", "recipe_rejected", f"这张照片用不了这个配方，没有套用。{exc.message}", "recipe"))
            else:
                changes["recipe"] = raw
    if "lut" in source:
        lut = parse_lut(source["lut"])
        if lut is None:
            changes["lut"] = None
        else:
            try:
                needs_raw = env.lut_needs_raw(lut)
            except LutError:
                skips.append(Skip("recipe", "lut_missing", "图库里找不到这个 LUT，没有套用 LUT。", "lut"))
            else:
                if needs_raw and "dng" not in (shot.get("files") or {}):
                    skips.append(Skip("recipe", "needs_raw", "这个 LUT 需要 RAW 文件，这张照片只有 JPG，没有套用 LUT。", "lut"))
                else:
                    changes["lut"] = lut
    return changes


def _edit_changes(
    side: dict[Any, Any], shot: dict[Any, Any], source: dict[str, Any], groups: list[str], env: Env, skips: list[Skip]
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "recipe" in groups:
        before = len(skips)
        part = _recipe_changes(shot, source, env, skips)
        if len(skips) == before:  # 配方 + LUT 是一个整体的「风格」：有一半用不了，这一组整个不套用，不留一半
            changes.update(part)
    if "adjust" in groups:
        changes.update({k: source[k] for k in GROUP_KEYS["adjust"] if k in source})
    if "geometry" in groups and "geometry" in source:
        now = parse_geometry(side["geometry"]) if isinstance(side.get("geometry"), dict) else Geometry()
        wanted = parse_geometry(source["geometry"]) if source["geometry"] is not None else Geometry()
        changes["geometry"] = replace(wanted, rotate90=now.rotate90, flip=now.flip).to_json()  # 转 90° 和翻转是照片自己的方向，不跟着走
    for group in ("frame", "stamp"):
        if group in groups and group in source:
            changes[group] = source[group]
    return changes


def _group_of(change_key: str) -> str:
    return next(g for g, keys in GROUP_KEYS.items() if change_key in keys)


def paste_one(
    lib: Library, shot: dict[Any, Any], source: dict[str, Any], groups: list[str], env: Env, *, expected_rev: int | None = None
) -> tuple[Outcome, dict[str, Any] | None]:
    """粘贴到一张照片。返回结果和粘贴之前的内容（没写过就是 None）。读写失败、版本对不上记成这一张的错，不抛出。"""
    shot_id = str(shot["shot_id"])
    try:
        _edit, side = read_edit(lib, shot, repair=False)
        sc.check_writable(side)
    except sc.SidecarTooNew:
        return _failed(shot_id, "sidecar_too_new", "这张照片的编辑来自更新的版本，没有粘贴。"), None
    except (sc.SidecarCorrupt, EditError):
        return _failed(shot_id, "sidecar_unreadable", "这张照片的编辑记录读不了，没有粘贴。"), None
    rev = int(side.get("rev", 0))
    if expected_rev is not None and expected_rev != rev:
        return _failed(shot_id, "rev_conflict", "这张照片已在别处修改，没有粘贴。"), None
    previous = {key: copy.deepcopy(side[key]) for key in SIDECAR_KEYS if key in side}
    skips: list[Skip] = []
    done: set[str] = set()
    for group in groups:
        if not any(key in source for key in GROUP_KEYS[group]):
            skips.append(Skip(group, "not_copied", "复制时没有带上这一项。"))
    todo = [g for g in groups if g != "crop"]
    changes = _edit_changes(side, shot, source, todo, env, skips)
    wrote = False
    try:
        if changes:
            try:
                _edit2, out = write_edit(lib, shot, changes, env.menu, expected_rev=rev, recipes=env.recipes)
            except EditError as exc:
                for group in {_group_of(k) for k in changes}:
                    skips.append(Skip(group, "rejected", exc.message))
                changes = {}
            else:
                rev, wrote = int(out["rev"]), True
                done.update(_group_of(k) for k in changes)
        if "crop" in groups and "crop" in source:
            box = source["crop"] or {"preset": "original", "rect": [0.0, 0.0, 1.0, 1.0]}
            try:
                done_crop = crop_edits.write_crop(lib, shot, preset=box["preset"], rect=box["rect"], fit=True, expected_rev=rev)
            except CropError as exc:
                skips.append(Skip("crop", exc.code, exc.message))
            else:
                rev, wrote = int(done_crop["rev"]), True
                done.add("crop")
    except RevisionConflict:
        message = "这张照片在粘贴时又被改了，没有粘贴完整。" if wrote else "这张照片已在别处修改，没有粘贴。"
        return _outcome(shot_id, groups, done, skips, rev if wrote else None, "rev_conflict", message), previous if wrote else None
    except sc.SidecarTooNew:
        return _failed(shot_id, "sidecar_too_new", "这张照片的编辑来自更新的版本，没有粘贴。"), None
    except sc.SidecarCorrupt:
        return _failed(shot_id, "sidecar_unreadable", "这张照片的编辑记录读不了，没有粘贴。"), None
    return _outcome(shot_id, groups, done, skips, rev if wrote else None), previous if wrote else None


def _outcome(
    shot_id: str, groups: list[str], done: set[str], skips: list[Skip], rev: int | None, code: str | None = None, message: str | None = None
) -> Outcome:
    applied = [g for g in groups if g in done]
    whole = len(applied) == len(groups) and not skips
    status = "error" if code else "applied" if whole else "partial" if applied else "skipped"
    return Outcome(shot_id, status, applied, skips, rev, code, message)


# ---- 撤销 -------------------------------------------------------------------------


@dataclass
class UndoEntry:
    rev: int  # 粘贴之后的版本：撤销只在照片还是这一版时才写
    previous: dict[str, Any]


class PasteUndo:
    """最近几批粘贴的「粘贴之前」。只在内存里：重启后不能再撤销（界面会如实说）。"""

    def __init__(self, limit: int = MAX_UNDO_BATCHES) -> None:
        self._lock = threading.Lock()
        self._limit = limit
        self._batches: OrderedDict[str, dict[str, UndoEntry]] = OrderedDict()

    def add(self, batch_id: str, shot_id: str, rev: int, previous: dict[str, Any]) -> None:
        with self._lock:
            batch = self._batches.setdefault(batch_id, {})
            self._batches.move_to_end(batch_id)
            kept = batch.get(shot_id)
            batch[shot_id] = UndoEntry(rev, kept.previous if kept else previous)  # 同一批里再次粘贴到同一张：还原到最早的样子
            while len(self._batches) > self._limit:
                self._batches.popitem(last=False)

    def take(self, batch_id: str) -> dict[str, UndoEntry] | None:
        with self._lock:
            return self._batches.pop(batch_id, None)


def restore(lib: Library, shot: dict[Any, Any], entry: UndoEntry) -> int:
    """把这张照片的这几组还原成粘贴之前。照片已经不是粘贴后的那一版就抛 RevisionConflict，什么都不写。"""
    path = lib.sidecar_path_for(shot)
    with lib.lock:
        side = lib.read_sidecar_for_write(shot)
        sc.check_writable(side)
        if int(side.get("rev", 0)) != entry.rev:
            raise RevisionConflict(int(side.get("rev", 0)))
        new = copy.deepcopy(side)
        for key in SIDECAR_KEYS:
            if key in entry.previous:
                new[key] = copy.deepcopy(entry.previous[key])
            else:
                new.pop(key, None)
        out = sc.write_atomic(path, crop_edits.clean_border(new), tmp_dir=lib.tmp_dir)
        lib.set_edit_cache(shot["shot_id"], int(out["rev"]), out.get("crop"))
        return int(out["rev"])


def undo_batch(lib: Library, entries: dict[str, UndoEntry]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for shot_id, entry in entries.items():
        shot = lib.get_shot(shot_id)
        if shot is None:
            results.append({"id": shot_id, "status": "error", "message": "找不到这张照片。"})
            continue
        try:
            results.append({"id": shot_id, "status": "restored", "rev": restore(lib, shot, entry)})
        except RevisionConflict:
            results.append({"id": shot_id, "status": "changed", "message": "粘贴之后又改过，没有撤销。"})
        except (sc.SidecarTooNew, sc.SidecarCorrupt):
            results.append({"id": shot_id, "status": "error", "message": "这张照片的编辑记录读写不了，没有撤销。"})
    return results
