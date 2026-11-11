"""把一张照片的编辑粘贴到别的照片（批量），以及撤销一批粘贴。逻辑在 `render/paste.py`。

`POST /api/edits/paste`：来源（`source` 是复制时拿到的内容，或 `source_id` 指一张照片）、要粘贴的组、目标照片。
每张目标照片各有结果：用上了哪些组、跳过了哪些组和原因、写完后的版本。能用的用，不能用的跳过并说明，不整体失败。
`POST /api/edits/paste/undo`：按 `batch_id` 把这一批的每张照片还原成粘贴之前；粘贴后又改过的照片不动，结果里说明。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..crop import CropError
from ..library import edits as crop_edits
from ..library import sidecar as sc
from ..lut.spaces import DISPLAY
from ..render import edits as render_edits
from ..render import paste
from ..render.params import EditError
from .errors import ApiError
from .render_routes import _edit_error, _payload
from .routes import _shot_or_404, ctx

router = APIRouter()

MAX_TARGETS = 200


class PasteTarget(BaseModel):
    id: str
    expected_rev: int | None = None  # 给了就要求照片还是这一版，不是就不粘贴这一张


class PasteRequest(BaseModel):
    groups: list[str] = Field(min_length=1)
    targets: list[PasteTarget] = Field(min_length=1, max_length=MAX_TARGETS)
    source: dict[str, Any] | None = None
    source_id: str | None = None
    batch_id: str | None = Field(default=None, min_length=1, max_length=80)  # 同一个 batch_id 的多次请求合成一批撤销
    include_state: bool = False  # 带上每张写过的照片现在的编辑和裁剪（编辑器粘贴到当前这张时用）


class UndoRequest(BaseModel):
    batch_id: str = Field(min_length=1, max_length=80)


def _bad(detail: str) -> ApiError:
    return ApiError(422, "bad_paste", "请求无效。请刷新页面后重试。", {"detail": detail})


def _env(c: Any) -> paste.Env:
    from ..finish.approx import ENABLED

    def lut_needs_raw(lut_id: str) -> bool:
        target, _item = c.render.luts.target(lut_id)
        return bool(target.input_space not in DISPLAY)

    return paste.Env(c.render.recipe_menu(), lut_needs_raw, bool(ENABLED and c.render.available()), c.render.recipes)


def _validate(body: PasteRequest) -> None:
    unknown = [g for g in body.groups if g not in paste.GROUPS]
    if unknown or len(set(body.groups)) != len(body.groups):
        raise _bad("groups 里有不认识或重复的项")
    ids = [t.id for t in body.targets]
    if len(set(ids)) != len(ids):
        raise _bad("targets 里有重复的照片")
    if (body.source is None) == (body.source_id is None):
        raise _bad("source 和 source_id 要给一个，且只给一个")


def _state(c: Any, shot: dict[Any, Any], out: dict[str, Any]) -> None:
    _edit, side = render_edits.read(c.lib, shot)
    out["edit"] = _payload(c, shot, side)
    try:
        out["crop"] = crop_edits.describe(side)
    except CropError:
        out["crop"] = None


@router.post("/api/edits/paste", response_model=None)
def paste_edits(body: PasteRequest, request: Request) -> Any:
    """把复制的编辑粘贴到一批照片。每张照片各有结果（用上的组、跳过的组和原因、新的版本）；能用的用，不能用的跳过。"""
    c = ctx(request)
    _validate(body)
    try:
        if body.source_id is not None:
            menu = c.render.recipe_menu() if "recipe" in body.groups else None
            source = paste.source_from_photo(c.lib, _shot_or_404(c, body.source_id), menu)
        else:
            source = body.source or {}
        source = paste.prepare_source(source, body.groups)
    except EditError as exc:
        raise _edit_error(exc) from exc
    except (sc.SidecarCorrupt, sc.SidecarTooNew) as exc:
        raise ApiError(422, "sidecar_unreadable", "来源照片的编辑记录读不了。") from exc
    batch_id = body.batch_id or uuid.uuid4().hex
    env = _env(c)
    results: list[dict[str, Any]] = []
    for target in body.targets:
        shot = c.lib.get_shot(target.id)
        if shot is None:
            results.append(paste.Outcome(target.id, "error", code="photo_not_found", message="找不到这张照片。").to_json())
            continue
        outcome, previous = paste.paste_one(c.lib, shot, source, body.groups, env, expected_rev=target.expected_rev)
        item = outcome.to_json()
        if previous is not None and outcome.rev is not None:
            c.paste_undo.add(batch_id, target.id, outcome.rev, previous)
            if body.include_state:
                _state(c, shot, item)
        results.append(item)
    counts = {k: sum(1 for r in results if r["status"] == k) for k in ("applied", "partial", "skipped", "error")}
    return {"batch_id": batch_id, "results": results, "counts": counts}


@router.post("/api/edits/paste/undo", response_model=None)
def undo_paste(body: UndoRequest, request: Request) -> Any:
    """把一批粘贴还原成粘贴之前；粘贴之后又改过的照片不动，结果里说明。"""
    c = ctx(request)
    entries = c.paste_undo.take(body.batch_id)
    if entries is None:
        raise ApiError(404, "paste_undo_expired", "这次粘贴已经不能撤销了。")
    results = paste.undo_batch(c.lib, entries)
    counts = {k: sum(1 for r in results if r["status"] == k) for k in ("restored", "changed", "error")}
    return {"results": results, "counts": counts}
