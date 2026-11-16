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


def test_mobile_ui_group_paste_matches_the_desktop(tmp_path: Path) -> None:
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

    # ---- 手机 ----
    report_path = tmp_path / "phone.json"
    manifest = tmp_path / "parity.json"
    files = [str(p) for p in (s_dng, s_jpg, t1_dng, t1_jpg, t2_jpg, t3_dng, t3_jpg)]
    manifest.write_text(
        json.dumps(
            {"pack": str(desk.zip), "dat": str(dat), "files": files, "photos": 4, "source": source, "targets": targets, "out": str(report_path)}
        ),
        encoding="utf-8",
    )
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
    edits.write(desk.lib, shots[source], {"recipe": js["body"], "adjust": _adjust(src_edit["knobs"])}, menu)
    _edit, desk_source = edits.read(desk.lib, shots[source])

    # 来源：手机交给导出的 sidecar 和桌面写的一样，memory 也一样（含空的那一项和当前配方的旧档位）。
    phone_source = steps["source"]["side"]
    same_redacted(parse_edit(phone_source).to_json(), parse_edit(desk_source).to_json(), "source parse_edit")
    same_redacted(_memory(phone_source), _memory(desk_source), "source recipe.memory")
    check(f"{unit}/4" in _memory(desk_source) and _memory(desk_source)[f"{unit}/4"] == {}, "desktop memory keeps the empty stock 04 entry")
    check(f"{unit}/2" in _memory(desk_source), "desktop memory keeps the current recipe's old snapshot")

    # ---- 桌面：同一次套用 ----
    env = paste.Env(menu, lambda _lut: True, False, desk.render.recipes)
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
