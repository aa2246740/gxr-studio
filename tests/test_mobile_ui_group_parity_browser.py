"""UI-M3-GROUP 第 7 项：同一次复制 / 套用 / 撤销，手机和桌面对拍，不经过导出。

手机：无头 Chrome 里用界面做（`mobile_ui_group_parity_browser.mjs`），每张照片的 sidecar 从 ui-m3 的导出请求
（`buildRequest(...).side`）读。桌面：来源照片的配方（带 memory）由桌面自己的 JS（`preview.js` / `look_cards.js`，
`mobile_ui_group_desktop_js.mjs` 在 vm 里跑）按同样的点卡、动旋钮的顺序算出来，经 `render/edits.write` 写进 sidecar；
再用 `render/paste.py` 的 `source_from_photo`、`paste_one`、`PasteUndo`、`undo_batch` 走同一次套用和撤销。

目标三张：同一个单元的 RAW+JPG、只有 JPG、另一个单元（副本里把镜头名的 50mm 换成 28mm，两边都读这个副本）。
比较的是 `parse_edit`、`recipe.memory`、每张的结果（状态、套用的组、跳过的原因）和桌面 JS 说出来的那句话。
照片只经 support.samples 找；断言用 redacted 帮手，不打印照片数据。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from support.export_pipeline_cases import check, same_redacted
from support.mobile_ui_export import copy_as, real_pairs
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, _drive, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "tests" / "support" / "mobile_ui_group_parity_browser.mjs"
DESKTOP_JS = REPO / "tests" / "support" / "mobile_ui_group_desktop_js.mjs"
UNITS = ("jlaunch", "qlaunch", "l08firm")
ADJUST_KEYS = ("exposure_ev", "contrast", "highlight", "shadow", "black_point", "saturation", "vibrance", "vignette")


def _desktop_js(tmp: Path, job: dict[str, Any]) -> dict[str, Any]:
    src, dst = tmp / "desktop-js-in.json", tmp / "desktop-js-out.json"
    src.write_text(json.dumps(job), encoding="utf-8")
    proc = subprocess.run(["node", str(DESKTOP_JS), str(src), str(dst)], cwd=REPO, check=False, capture_output=True, text=True, timeout=60)
    check(proc.returncode == 0, "desktop JS helper ran")
    out: dict[str, Any] = json.loads(dst.read_text(encoding="utf-8"))
    return out


def _other_unit(src: Path, dest: Path) -> None:
    """同一张照片换一个镜头单元：文件里的「50mm」都换成「28mm」（等长，别的字节不动）。"""
    data = src.read_bytes()
    check(re.search(rb"50mm", data, re.IGNORECASE) is not None, "the sample's lens names 50mm")
    dest.write_bytes(re.sub(rb"50(mm)", rb"28\1", data, flags=re.IGNORECASE))


def _adjust(knobs: dict[str, Any]) -> dict[str, Any]:
    """桌面 preview.js adjustBody（v2）：手机的十个调整写成桌面交给 PUT /edit 的样子。"""
    body: dict[str, Any] = {k: knobs[k] for k in ADJUST_KEYS}
    body["wb"] = {"warm": knobs["warm"], "tint": knobs["tint"]}
    return body


def _pick(catalog: dict[str, Any], unit: str, card: str) -> dict[str, Any]:
    """手机的卡 id → 桌面 recipeTarget 的 target（kind、slot、id）。"""
    kind, ident = card.split(":", 1)
    if kind == "builtin":
        return {"kind": "builtin", "slot": int(ident)}
    row = next(u for u in catalog["units"] if u["id"] == unit)
    info = next(r for r in row.get("looks" if kind == "look" else "users", []) if r["id"] == ident)
    return {"kind": kind, "slot": int(info["slot"]), "id": ident}


def _outcome(row: dict[str, Any]) -> dict[str, Any]:
    """结果里两边都有、和版本号无关的部分。"""
    return {
        "status": row["status"],
        "applied": row["applied"],
        "skipped": row["skipped"],
        "code": row.get("code"),
    }


def _memory(side: dict[str, Any] | None) -> Any:
    recipe = (side or {}).get("recipe") or {}
    return {key: dict(sorted(levels.items())) for key, levels in sorted((recipe.get("memory") or {}).items())}


def _run_parity(tmp_path: Path, *, with_lut: bool) -> None:
    from gxr_studio.render import edits, paste
    from gxr_studio.render.params import parse_edit
    from gxr_studio.render.recipes import unit_of_lens
    from test_mobile_ui_export_browser import Desktop, _local_dat

    pairs = real_pairs(4)
    if len(pairs) < 4:
        from support import samples

        samples.missing("need four DNG+JPG pairs (GXR_STUDIO_TEST_PARENT)")
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")
    dat = _local_dat()

    # 照片：来源和同单元目标是两对真样本；只有 JPG 的是第三对的 JPG 副本；另一个单元是第四对的副本（镜头名改成 28mm）。
    work = tmp_path / "files"
    work.mkdir()
    (s_dng, s_jpg), (t1_dng, t1_jpg) = pairs[0], pairs[1]
    t2_jpg = copy_as(pairs[2][1], work / "R9990002.JPG")
    t3_dng, t3_jpg = work / "R9990003.DNG", work / "R9990003.JPG"
    _other_unit(pairs[3][0], t3_dng)
    _other_unit(pairs[3][1], t3_jpg)
    source, targets = s_dng.stem, [t1_dng.stem, t2_jpg.stem, t3_dng.stem]

    desk = Desktop(tmp_path, dat)
    shots = {
        source: desk.shot(source, s_dng, s_jpg),
        targets[0]: desk.shot(targets[0], t1_dng, t1_jpg),
        targets[1]: desk.shot(targets[1], None, t2_jpg),
        targets[2]: desk.shot(targets[2], t3_dng, t3_jpg),
    }
    unit = unit_of_lens(shots[source]["lens"]) or "jlaunch"
    check(unit == "jlaunch" and unit_of_lens(shots[targets[2]]["lens"]) == "qlaunch", "units as planned")
    check(unit_of_lens(shots[targets[0]]["lens"]) == unit, "same-unit target")

    lut_payload: dict[str, str] | None = None
    cube_id: str | None = None
    if with_lut:
        import numpy as np

        from support.lut_cases import cube_text

        cube_txt = cube_text(
            lambda x: np.clip(x * np.array([1.04, 0.98, 0.92]), 0, 1),
            size=9,
            title="Parity LUT",
            comments="# Input: sRGB\n",
        )
        cube_path = tmp_path / "parity_srgb.cube"
        cube_path.write_text(cube_txt, encoding="utf-8")
        added = desk.render.luts.add(cube_txt.encode("utf-8"), cube_path.name)
        cube_id = str(added["cube_id"])
        lut_payload = {"path": str(cube_path), "cubeId": cube_id}

    # ---- 手机 ----
    report_path = tmp_path / "phone.json"
    manifest = tmp_path / "parity.json"
    files = [str(p) for p in (s_dng, s_jpg, t1_dng, t1_jpg, t2_jpg, t3_dng, t3_jpg)]
    manifest_doc: dict[str, Any] = {
        "pack": str(desk.zip),
        "dat": str(dat),
        "files": files,
        "photos": 4,
        "source": source,
        "targets": targets,
        "out": str(report_path),
    }
    if lut_payload is not None:
        manifest_doc["lut"] = lut_payload
    manifest.write_text(json.dumps(manifest_doc), encoding="utf-8")
    result = _drive(DRIVER, manifest, tmp_path, "", timeout=600)
    check(report_path.is_file(), "driver wrote its report")
    phone: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    check(result.returncode == 0 and phone.get("error") is None, "phone driver finished (first error line is in the report file)")
    check(phone["pageErrors"] == [], "no page errors")
    steps = phone["steps"]

    # ---- 桌面：来源的编辑由桌面 JS 按同样的顺序算出 ----
    catalog = {"units": desk.render.catalog()}
    actions: list[dict[str, Any]] = []
    for act in phone["actions"]:
        if "pick" in act:
            actions.append({"pick": _pick(catalog, unit, act["pick"])})
        else:
            actions.append({"knob": {"name": act["knob"]["name"], "level": act["knob"]["level"]}})
    js = _desktop_js(tmp_path, {"recipe": {"catalog": catalog, "photoUnit": unit, "actions": actions}})
    menu = desk.render.recipe_menu()
    src_edit = steps["source"]["edit"]
    desk_changes: dict[str, Any] = {"recipe": js["body"], "adjust": _adjust(src_edit["knobs"])}
    if cube_id is not None:
        desk_changes["lut"] = cube_id
    edits.write(desk.lib, shots[source], desk_changes, menu)
    _edit, desk_source = edits.read(desk.lib, shots[source])

    # 来源：手机交给导出的 sidecar 和桌面写的一样，memory 也一样（含空的那一项和当前配方的旧档位）。
    phone_source = steps["source"]["side"]
    same_redacted(parse_edit(phone_source).to_json(), parse_edit(desk_source).to_json(), "source parse_edit")
    same_redacted(_memory(phone_source), _memory(desk_source), "source recipe.memory")
    check(f"{unit}/4" in _memory(desk_source) and _memory(desk_source)[f"{unit}/4"] == {}, "desktop memory keeps the empty stock 04 entry")
    check(f"{unit}/2" in _memory(desk_source), "desktop memory keeps the current recipe's old snapshot")

    # ---- 桌面：同一次套用 ----
    def _lut_needs_raw(lut_id: str) -> bool:
        item = desk.render.luts.get(lut_id)
        return item["input_space"] not in ("srgb", "rec709", "gamma24")

    env = paste.Env(menu, _lut_needs_raw, False, desk.render.recipes)
    groups = ["recipe", "adjust"]

    # 手机按网格的顺序（拍摄时间）逐张套用；结果句里各种原因的先后跟着结果的顺序走，所以桌面也按这个顺序。
    by_id = {stem.lower(): stem for stem in targets}
    order = [by_id[row["id"]] for row in steps["paste"]["outcomes"]]
    check(sorted(order) == sorted(targets), "the phone pasted the three targets")

    def run_paste(batch: str, undo: paste.PasteUndo) -> list[dict[str, Any]]:
        src = paste.source_from_photo(desk.lib, shots[source], menu)
        rows = []
        for stem in order:
            outcome, previous = paste.paste_one(desk.lib, shots[stem], src, groups, env)
            if previous is not None and outcome.rev is not None:
                undo.add(batch, stem, outcome.rev, previous)
            rows.append(outcome.to_json())
        return rows

    def undo_batch(entries: dict[str, Any]) -> list[dict[str, Any]]:
        """`paste.undo_batch` 的同一套写法，只是照片从这个测试的 shot 字典里找（临时图库里没登记这些 shot）。"""
        from gxr_studio.library import sidecar as sc
        from gxr_studio.library.edits import RevisionConflict

        rows: list[dict[str, Any]] = []
        for shot_id, entry in entries.items():
            try:
                rows.append({"id": shot_id, "status": "restored", "rev": paste.restore(desk.lib, shots[shot_id], entry)})
            except RevisionConflict:
                rows.append({"id": shot_id, "status": "changed"})
            except (sc.SidecarTooNew, sc.SidecarCorrupt):
                rows.append({"id": shot_id, "status": "error"})
        return rows

    def desk_sides() -> dict[str, dict[str, Any]]:
        return {stem: edits.read(desk.lib, shots[stem])[1] for stem in [source, *targets]}

    undo = paste.PasteUndo()
    desk_results = run_paste("b1", undo)
    after_paste = desk_sides()
    pasted = steps["paste"]
    same_redacted([_outcome(r) for r in pasted["outcomes"]], [_outcome(r) for r in desk_results], "per-target outcomes")
    for stem in [source, *targets]:
        same_redacted(parse_edit(pasted["sides"][stem]).to_json(), parse_edit(after_paste[stem]).to_json(), "parse_edit after paste")
        same_redacted(_memory(pasted["sides"][stem]), _memory(after_paste[stem]), "recipe.memory after paste")
    reasons = {r["id"]: [(s["group"], s["reason"]) for s in r["skipped"]] for r in desk_results}
    planned = {targets[0]: [], targets[1]: [("recipe", "needs_raw")], targets[2]: [("recipe", "unit_mismatch")]}
    check(reasons == planned, "the three targets hit the planned rules")

    undone = undo_batch(undo.take("b1") or {})
    after_undo = desk_sides()
    check(sorted(r["status"] for r in undone) == ["restored"] * 3, "desktop restores all three")
    for stem in [source, *targets]:
        same_redacted(parse_edit(steps["undo"]["sides"][stem]).to_json(), parse_edit(after_undo[stem]).to_json(), "parse_edit after undo")
        same_redacted(_memory(steps["undo"]["sides"][stem]), _memory(after_undo[stem]), "recipe.memory after undo")

    # 再套用一次，改第一张目标（手机上是阴影 20），撤销：那一张算改过。
    undo2 = paste.PasteUndo()
    desk_results2 = run_paste("b2", undo2)
    _e, side_t1 = edits.read(desk.lib, shots[targets[0]])
    knobs = parse_edit(side_t1).adjust.to_json()
    wb = knobs.get("wb", {})
    knobs_flat = {**{k: knobs.get(k, 0) for k in ADJUST_KEYS}, "warm": wb.get("warm", 0), "tint": wb.get("tint", 0)}
    knobs_flat["shadow"] = 20
    edits.write(desk.lib, shots[targets[0]], {"adjust": _adjust(knobs_flat)}, menu)
    undone2 = undo_batch(undo2.take("b2") or {})
    after_changed = desk_sides()
    want_undo2 = {targets[0]: "changed", targets[1]: "restored", targets[2]: "restored"}
    check({r["id"]: r["status"] for r in undone2} == want_undo2, "desktop: the edited target is left alone")
    for stem in [source, *targets]:
        phone_side = steps["changed"]["sides"][stem]
        same_redacted(parse_edit(phone_side).to_json(), parse_edit(after_changed[stem]).to_json(), "parse_edit after edit+undo")
        same_redacted(_memory(steps["changed"]["sides"][stem]), _memory(after_changed[stem]), "recipe.memory after edit+undo")

    # 结果句：桌面 JS 的 pasteMessage / undoMessage 对桌面的结果说的话，和手机底栏上的一字不差。
    def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        return {key: sum(1 for r in rows if r["status"] == key) for key in ("restored", "changed", "error")}

    lines = _desktop_js(tmp_path, {"paste": [desk_results, desk_results2], "undo": [counts(undone), counts(undone2)]})
    same_redacted(
        [pasted["line"], steps["undo"]["line"], steps["changed"]["pasteLine"], steps["changed"]["line"]],
        [lines["pasteLines"][0], lines["undoLines"][0], lines["pasteLines"][1], lines["undoLines"][1]],
        "result lines equal the desktop's",
    )
    parts = lines["pasteLines"][0].split("；")
    expected_parts = sorted(["1 张只有 JPG，没有套用配方", "1 张镜头单元不同，没有套用配方"])
    check(parts[0] == "已套用到 3 张" and sorted(parts[1:]) == expected_parts, "paste line as expected")
    check(lines["undoLines"][1] == "已撤销 2 张；1 张粘贴之后又改过，没有撤销", "changed line as expected")
    # 表格给报告用（只有结构，没有照片数据）。
    table = {
        "targets": {targets[0]: "same-unit RAW+JPG", targets[1]: "JPG only", targets[2]: "other unit"},
        "outcomes": {r["id"]: _outcome(r) for r in desk_results},
        "undo": {r["id"]: r["status"] for r in undone},
        "undo_after_edit": {r["id"]: r["status"] for r in undone2},
        "lines": lines,
        "source_memory_keys": sorted(_memory(desk_source)),
    }
    (tmp_path / "parity-table.json").write_text(json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
    print("group paste parity:", json.dumps({k: table[k] for k in ("outcomes", "undo", "undo_after_edit")}, ensure_ascii=False))


def test_mobile_ui_group_geometry_and_crop_paste_matches_desktop(tmp_path: Path) -> None:
    """UI-M4 第 7 项：拉直 + XPan 来源套用到同方向目标、竖向目标和越界 fit 目标，并核对撤销。"""
    from gxr_studio.library import edits as crop_edits
    from gxr_studio.render import edits, paste
    from gxr_studio.render.params import parse_edit
    from support import mobile_ui_export as mx
    from support import samples
    from test_mobile_ui_export_browser import Desktop, _local_dat

    pairs = real_pairs(4)
    if len(pairs) < 4:
        samples.missing("need four DNG+JPG pairs (GXR_STUDIO_TEST_PARENT)")
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")
    dat = _local_dat()

    work = tmp_path / "files"
    work.mkdir()
    (s_dng, s_jpg), (t1_dng, t1_jpg) = pairs[0], pairs[1]
    t2_jpg = work / "R9990004.JPG"
    check(mx.set_orientation(pairs[2][1], t2_jpg, 6), "portrait target Orientation rewritten to 6")
    t3_dng, t3_jpg = pairs[3]
    source, targets = s_dng.stem, [t1_dng.stem, t2_jpg.stem, t3_dng.stem]

    desk = Desktop(tmp_path, dat)
    try:
        shots = {
            source: desk.shot(source, s_dng, s_jpg),
            targets[0]: desk.shot(targets[0], t1_dng, t1_jpg),
            targets[1]: {**desk.shot(targets[1], None, t2_jpg), "orientation": 6},
            targets[2]: desk.shot(targets[2], t3_dng, t3_jpg),
        }
        check(shots[targets[1]]["width"] < shots[targets[1]]["height"], "t2 is portrait")

        report_path = tmp_path / "phone-crop.json"
        manifest = tmp_path / "parity-crop.json"
        files = [str(p) for p in (s_dng, s_jpg, t1_dng, t1_jpg, t2_jpg, t3_dng, t3_jpg)]
        manifest.write_text(
            json.dumps(
                {
                    "mode": "crop",
                    "pack": str(desk.zip),
                    "dat": str(dat),
                    "files": files,
                    "photos": 4,
                    "source": source,
                    "targets": targets,
                    "out": str(report_path),
                }
            ),
            encoding="utf-8",
        )
        result = _drive(DRIVER, manifest, tmp_path, "", timeout=600)
        check(report_path.is_file(), "crop parity driver wrote report")
        phone: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
        check(result.returncode == 0 and phone.get("error") is None, "crop parity driver finished")
        check(phone["pageErrors"] == [], "no page errors")
        steps = phone["steps"]

        # 复制表：几何和裁剪都不应写「（这张没有设置）」
        by_group = {row["group"]: row["text"] for row in steps["copyOptions"]}
        check("（这张没有设置）" not in by_group["geometry"], "geometry group is marked set")
        check("（这张没有设置）" not in by_group["crop"], "crop group is marked set")

        menu = desk.render.recipe_menu()
        env = paste.Env(menu, lambda _lut: True, False, desk.render.recipes)

        # 桌面同步设置 targets[2] 的 18° 拉直，再设来源的 1.5° + XPan + 曝光
        edits.write(
            desk.lib,
            shots[targets[2]],
            {"geometry": {"straighten": 18.0, "vertical": 0, "horizontal": 0, "distortion": 0, "rotate90": 0, "flip": False}},
            menu,
        )
        edits.write(
            desk.lib,
            shots[source],
            {
                "adjust": _adjust(steps["source"]["edit"]["knobs"]),
                "geometry": {"straighten": 1.5, "vertical": 0, "horizontal": 0, "distortion": 0, "rotate90": 0, "flip": False},
            },
            menu,
        )
        crop_edits.write_crop(desk.lib, shots[source], preset="xpan-65-24", rect=None, fit=False)
        _e, desk_src = edits.read(desk.lib, shots[source])

        def _crop_floats(raw: dict[str, Any] | None) -> dict[str, Any] | None:
            if raw is None:
                return None
            return {k: ([float(v) for v in val] if k == "rect" else (float(val) if k == "angle_deg" else val)) for k, val in raw.items()}

        same_redacted(parse_edit(steps["source"]["side"]).to_json(), parse_edit(desk_src).to_json(), "source parse_edit")
        same_redacted(_crop_floats(steps["source"]["side"].get("crop")), _crop_floats(desk_src.get("crop")), "source crop")
        same_redacted(steps["source"]["side"].get("frame"), desk_src.get("frame"), "source frame")

        def undo_batch(entries: dict[str, Any]) -> list[dict[str, Any]]:
            from gxr_studio.library import sidecar as sc
            from gxr_studio.library.edits import RevisionConflict

            rows: list[dict[str, Any]] = []
            for shot_id, entry in entries.items():
                try:
                    rows.append({"id": shot_id, "status": "restored", "rev": paste.restore(desk.lib, shots[shot_id], entry)})
                except RevisionConflict:
                    rows.append({"id": shot_id, "status": "changed"})
                except (sc.SidecarTooNew, sc.SidecarCorrupt):
                    rows.append({"id": shot_id, "status": "error"})
            return rows

        # 1) 套用 adjust + geometry + crop 到同向 targets[0] 和竖向 targets[1]
        paste1 = steps["paste1"]
        by_id = {stem.lower(): stem for stem in targets}
        order1 = [by_id[row["id"]] for row in paste1["outcomes"]]
        check(sorted(order1) == sorted(targets[:2]), "paste1 hit t1 and t2")
        undo = paste.PasteUndo()
        src1 = paste.source_from_photo(desk.lib, shots[source], menu)
        desk_res1 = []
        for stem in order1:
            out, prev = paste.paste_one(desk.lib, shots[stem], src1, ["adjust", "geometry", "crop"], env)
            if prev is not None and out.rev is not None:
                undo.add("c1", stem, out.rev, prev)
            desk_res1.append(out.to_json())
        same_redacted([_outcome(r) for r in paste1["outcomes"]], [_outcome(r) for r in desk_res1], "paste1 outcomes")
        for stem in [source, *targets[:2]]:
            _e, dside = edits.read(desk.lib, shots[stem])
            same_redacted(parse_edit(paste1["sides"][stem]).to_json(), parse_edit(dside).to_json(), f"paste1 {stem} parse_edit")
            same_redacted(_crop_floats(paste1["sides"][stem].get("crop")), _crop_floats(dside.get("crop")), f"paste1 {stem} crop")
            same_redacted(paste1["sides"][stem].get("frame"), dside.get("frame"), f"paste1 {stem} frame")
        # 确认竖向目标套出来的框仍然是竖图里的合格 XPan 框（宽高比 65:24、横跨 width=1）
        t2_crop = paste1["sides"][targets[1]].get("crop")
        check(t2_crop is not None and t2_crop["preset"] == "xpan-65-24", "t2 kept xpan preset through fit")

        undone1 = undo_batch(undo.take("c1") or {})
        check(sorted(r["status"] for r in undone1) == ["restored", "restored"], "undo1 restored both")
        for stem in targets[:2]:
            _e, dside = edits.read(desk.lib, shots[stem])
            same_redacted(parse_edit(steps["undo1"]["sides"][stem]).to_json(), parse_edit(dside).to_json(), f"undo1 {stem} parse_edit")
            same_redacted(_crop_floats(steps["undo1"]["sides"][stem].get("crop")), _crop_floats(dside.get("crop")), f"undo1 {stem} crop")

        # 2) 只套用 crop 到已拉直 18° 的 targets[2]（fit=true 把越界的 XPan 框收进 18° 安全边界）
        paste2 = steps["paste2"]
        src2 = paste.source_from_photo(desk.lib, shots[source], menu)
        out2, prev2 = paste.paste_one(desk.lib, shots[targets[2]], src2, ["crop"], env)
        check(prev2 is not None and out2.rev is not None, "paste2 wrote t3")
        undo.add("c2", targets[2], out2.rev, prev2)
        same_redacted([_outcome(r) for r in paste2["outcomes"]], [_outcome(out2.to_json())], "paste2 outcome")
        _e, dside_t3 = edits.read(desk.lib, shots[targets[2]])
        same_redacted(parse_edit(paste2["sides"][targets[2]]).to_json(), parse_edit(dside_t3).to_json(), "paste2 t3 parse_edit")
        same_redacted(_crop_floats(paste2["sides"][targets[2]].get("crop")), _crop_floats(dside_t3.get("crop")), "paste2 t3 crop")
        same_redacted(paste2["sides"][targets[2]].get("frame"), dside_t3.get("frame"), "paste2 t3 frame")

        undone2 = undo_batch(undo.take("c2") or {})
        check([r["status"] for r in undone2] == ["restored"], "undo2 restored t3")
        _e, dside_t3_u = edits.read(desk.lib, shots[targets[2]])
        same_redacted(parse_edit(steps["undo2"]["sides"][targets[2]]).to_json(), parse_edit(dside_t3_u).to_json(), "undo2 t3 parse_edit")
        same_redacted(_crop_floats(steps["undo2"]["sides"][targets[2]].get("crop")), _crop_floats(dside_t3_u.get("crop")), "undo2 t3 crop")

        def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
            return {key: sum(1 for r in rows if r["status"] == key) for key in ("restored", "changed", "error")}

        lines = _desktop_js(tmp_path, {"paste": [desk_res1, [out2.to_json()]], "undo": [counts(undone1), counts(undone2)]})
        same_redacted(
            [paste1["line"], steps["undo1"]["line"], paste2["line"], steps["undo2"]["line"]],
            [lines["pasteLines"][0], lines["undoLines"][0], lines["pasteLines"][1], lines["undoLines"][1]],
            "crop paste/undo lines equal the desktop's",
        )
    finally:
        desk.lib.close()


def test_mobile_ui_group_paste_matches_the_desktop(tmp_path: Path) -> None:
    _run_parity(tmp_path, with_lut=False)


def test_mobile_ui_group_paste_recipe_with_lut_matches_the_desktop(tmp_path: Path) -> None:
    """UI-M4-LUT item 6: extend group paste desktop parity with a recipe + LUT source."""
    _run_parity(tmp_path, with_lut=True)


def test_mobile_ui_group_frame_and_stamp_paste_matches_desktop(tmp_path: Path) -> None:
    """UI-M4-FRAME 第 7 项：边框 + 时间戳来源套用到横向目标与竖向目标，并核对撤销。"""
    from gxr_studio.render import edits, paste
    from gxr_studio.render.params import parse_edit
    from support import mobile_ui_export as mx
    from support import samples
    from test_mobile_ui_export_browser import Desktop, _local_dat

    pairs = real_pairs(3)
    if len(pairs) < 3:
        samples.missing("need three DNG+JPG pairs (GXR_STUDIO_TEST_PARENT)")
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")
    dat = _local_dat()

    work = tmp_path / "files"
    work.mkdir()
    (s_dng, s_jpg), (t1_dng, t1_jpg) = pairs[0], pairs[1]
    t2_jpg = work / "R9990005.JPG"
    check(mx.set_orientation(pairs[2][1], t2_jpg, 6), "portrait target Orientation rewritten to 6")
    source, targets = s_dng.stem, [t1_dng.stem, t2_jpg.stem]

    desk = Desktop(tmp_path, dat)
    try:
        shots = {
            source: desk.shot(source, s_dng, s_jpg),
            targets[0]: desk.shot(targets[0], t1_dng, t1_jpg),
            targets[1]: {**desk.shot(targets[1], None, t2_jpg), "orientation": 6},
        }
        check(shots[targets[0]]["width"] > shots[targets[0]]["height"], "t1 is landscape")
        check(shots[targets[1]]["width"] < shots[targets[1]]["height"], "t2 is portrait")

        report_path = tmp_path / "phone-frame.json"
        manifest = tmp_path / "parity-frame.json"
        files = [str(p) for p in (s_dng, s_jpg, t1_dng, t1_jpg, t2_jpg)]
        manifest.write_text(
            json.dumps(
                {
                    "mode": "frame",
                    "pack": str(desk.zip),
                    "dat": str(dat),
                    "files": files,
                    "photos": 3,
                    "source": source,
                    "targets": targets,
                    "out": str(report_path),
                }
            ),
            encoding="utf-8",
        )
        result = _drive(DRIVER, manifest, tmp_path, "", timeout=600)
        check(report_path.is_file(), "frame parity driver wrote report")
        phone: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
        check(result.returncode == 0 and phone.get("error") is None, "frame parity driver finished")
        check(phone["pageErrors"] == [], "no page errors")
        steps = phone["steps"]

        by_group = {row["group"]: row["text"] for row in steps["copyOptions"]}
        check("（这张没有设置）" not in by_group["frame"], "frame group is marked set")
        check("（这张没有设置）" not in by_group["stamp"], "stamp group is marked set")
        check("徕卡水印，时间戳" in steps["whereLead"], "copied-where summary lists frame style and stamp")

        menu = desk.render.recipe_menu()
        env = paste.Env(menu, lambda _lut: True, False, desk.render.recipes)

        edits.write(
            desk.lib,
            shots[source],
            {
                "frame": {
                    "version": 2,
                    "style": "leica",
                    "color": "cream",
                    "weight": "medium",
                    "show_logo": True,
                    "show_params": True,
                    "show_date": True,
                    "text": "",
                    "output_ratio": "3:4",
                    "fmt": "135",
                    "table": "light",
                    "logo": "ricoh",
                    "logo_mode": "auto",
                    "logo_size": "medium",
                    "model": True,
                },
                "stamp": {
                    "enabled": True,
                    "content": "date+text",
                    "date_format": "'yy mm dd",
                    "show_time": False,
                    "date_override": None,
                    "text": "GXR 50",
                    "font": "typewriter",
                    "color": "yellow",
                    "size": "medium",
                    "corner": "bl",
                    "glow": True,
                    "placement": "photo",
                },
            },
            menu,
        )
        _e, desk_src = edits.read(desk.lib, shots[source])
        phone_src = steps["source"]["side"]
        same_redacted(parse_edit(phone_src).to_json(), parse_edit(desk_src).to_json(), "frame source parse_edit")
        same_redacted(phone_src.get("border"), desk_src.get("border"), "frame source border")
        same_redacted(phone_src.get("stamp"), desk_src.get("stamp"), "frame source stamp")
        same_redacted(phone_src.get("frame"), desk_src.get("frame"), "frame source size frame")

        def undo_batch(entries: dict[str, Any]) -> list[dict[str, Any]]:
            from gxr_studio.library import sidecar as sc
            from gxr_studio.library.edits import RevisionConflict

            rows: list[dict[str, Any]] = []
            for shot_id, entry in entries.items():
                try:
                    rows.append({"id": shot_id, "status": "restored", "rev": paste.restore(desk.lib, shots[shot_id], entry)})
                except RevisionConflict:
                    rows.append({"id": shot_id, "status": "changed"})
                except (sc.SidecarTooNew, sc.SidecarCorrupt):
                    rows.append({"id": shot_id, "status": "error"})
            return rows

        pasted = steps["paste"]
        by_id = {stem.lower(): stem for stem in targets}
        order = [by_id[row["id"]] for row in pasted["outcomes"]]
        check(sorted(order) == sorted(targets), "frame paste hit landscape and portrait targets")

        undo = paste.PasteUndo()
        src_clip = paste.source_from_photo(desk.lib, shots[source], menu)
        desk_res = []
        for stem in order:
            out_row, prev = paste.paste_one(desk.lib, shots[stem], src_clip, ["frame", "stamp"], env)
            if prev is not None and out_row.rev is not None:
                undo.add("f1", stem, out_row.rev, prev)
            desk_res.append(out_row.to_json())

        same_redacted([_outcome(r) for r in pasted["outcomes"]], [_outcome(r) for r in desk_res], "frame paste outcomes")
        for stem in [source, *targets]:
            _e, dside = edits.read(desk.lib, shots[stem])
            pside = pasted["sides"][stem]
            same_redacted(parse_edit(pside).to_json(), parse_edit(dside).to_json(), f"frame paste {stem} parse_edit")
            same_redacted(pside.get("border"), dside.get("border"), f"frame paste {stem} border")
            same_redacted(pside.get("stamp"), dside.get("stamp"), f"frame paste {stem} stamp")
            same_redacted(pside.get("frame"), dside.get("frame"), f"frame paste {stem} size frame")

        undone = undo_batch(undo.take("f1") or {})
        check(sorted(r["status"] for r in undone) == ["restored", "restored"], "frame undo restored both targets")
        for stem in [source, *targets]:
            _e, dside_u = edits.read(desk.lib, shots[stem])
            pside_u = steps["undo"]["sides"][stem]
            same_redacted(parse_edit(pside_u).to_json(), parse_edit(dside_u).to_json(), f"frame undo {stem} parse_edit")
            same_redacted(pside_u.get("border"), dside_u.get("border"), f"frame undo {stem} border")
            same_redacted(pside_u.get("stamp"), dside_u.get("stamp"), f"frame undo {stem} stamp")

        def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
            return {key: sum(1 for r in rows if r["status"] == key) for key in ("restored", "changed", "error")}

        lines = _desktop_js(tmp_path, {"paste": [desk_res], "undo": [counts(undone)]})
        same_redacted(
            [pasted["line"], steps["undo"]["line"]],
            [lines["pasteLines"][0], lines["undoLines"][0]],
            "frame paste/undo lines equal the desktop's",
        )
    finally:
        desk.lib.close()
