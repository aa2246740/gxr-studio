"""同一配方只看条目本身：两个槽位解析出同一份条目时，EXIF 与 /edit 判定一致。"""

from __future__ import annotations

import hashlib

from gxr_studio import halfframe_compose as hfc
from gxr_studio.api.render_routes import _same_plan_recipe
from gxr_studio.raw import Renderer
from gxr_studio.render.params import same_recipe_entries
from gxr_studio.render.service import Plan


def test_same_entry_different_slots_agree_in_exif_and_edit() -> None:
    entry = b"same-recipe-entry"
    digest = hashlib.sha256(entry).hexdigest()
    other = hashlib.sha256(b"other-recipe-entry").hexdigest()

    def plan(slot: int, sha: str, selection: str) -> Plan:
        return Plan("l08firm", slot, False, {}, entry, sha, 200, True, (), "电脑预测", None, "r" * 64, "b" * 64, selection)

    left, right = plan(1, digest, "local:1"), plan(6, digest, "local:6")
    assert same_recipe_entries("", "") is False
    assert same_recipe_entries(digest, "") is False
    assert same_recipe_entries(left.entry_sha256, right.entry_sha256)
    assert _same_plan_recipe(left, right)
    assert not _same_plan_recipe(left, plan(6, other, "local:6"))

    pfx = "GXR Studio computer prediction (not camera-measured): "

    def renderer(slot: int, sha: str, selection: str) -> Renderer:
        binding = {
            "record_sha256": f"record-{slot}",
            "entry_sha256": sha,
            "binding_sha256": f"binding-{slot}",
            "selection_id": selection,
            "quality_state": "passed",
            "assessed_entry": "1",
        }
        return Renderer("recipe-prediction", "电脑预测", "", True, lambda x: x, f"{pfx}slot {slot}.", binding, sha)

    labels = hfc._labels_from_renderers(renderer(1, digest, "local:1"), renderer(6, digest, "local:6"), neutral=False)
    assert "left:" not in labels["description"] and labels["entry_sha256"] == digest
    mixed = hfc._labels_from_renderers(renderer(1, digest, "local:1"), renderer(6, other, "local:6"), neutral=False)
    assert mixed["description"].startswith(f"{pfx}left:") and "entry_sha256" not in mixed
