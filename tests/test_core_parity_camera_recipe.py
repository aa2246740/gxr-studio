"""Core parity for camera shooting recipe resolution: native gxr-cli == gxr-wasm == desktop photo_edit."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

from gxr_studio import meta
from gxr_studio.camdata import store
from gxr_studio.camdata.catalog_names import CATALOG_GENERATIONS
from gxr_studio.camdata.recipe_tags import parse_recipe_tag
from gxr_studio.render.camera_recipe import photo_edit
from gxr_studio.render.params import EditError
from gxr_studio.render.recipes import CameraData, Menu
from support import samples
from support.core_cli import require_cli, run_json
from support.export_pipeline_cases import check, same_redacted
from support.wasm_check import PKG
from test_core_parity_camdata import R5_BUNDLES, SINGLE_DATS

REPO = Path(__file__).resolve().parents[1]
WASM_RUNNER = REPO / "tests" / "support" / "camera_recipe_wasm_parity.mjs"
PKG_WASM = PKG.with_name("gxr_wasm_bg.wasm")

IDENTITY_BUNDLE_ORDER: dict[str, tuple[str, ...]] = {
    "d25d2f0a68": ("g176",),
    "6a3a5a3c02": ("g185", "g182", "g181", "g178", "g250"),
    "6adf847602": ("g250",),
    "0ccc86c19b": ("g250_kubik",),
}


def _run_wasm_cases(tmp_path: Path, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not PKG.is_file() or not PKG_WASM.is_file():
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.fail("wasm package is missing; run make core-wasm")
        pytest.skip("wasm package is missing; run make core-wasm")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    manifest = tmp_path / "wasm-cases.json"
    out = tmp_path / "wasm-out.json"
    manifest.write_text(json.dumps(cases), encoding="utf-8")
    proc = subprocess.run(
        [node, str(WASM_RUNNER), str(PKG), str(PKG_WASM), str(manifest), str(out)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0 or not out.is_file():
        pytest.fail(f"wasm parity runner failed: {proc.stderr[:300]}", pytrace=False)
    loaded: list[dict[str, Any]] = json.loads(out.read_text(encoding="utf-8"))
    return loaded


def _desktop_expected(tag_text: str, cam: Menu) -> tuple[bool, dict[str, Any]]:
    tag = parse_recipe_tag(tag_text)
    shot = {"files": {"dng": {"relpath": "x.dng"}}, "recipe_tag": tag}
    try:
        edit = photo_edit({}, shot, cam)
    except EditError as exc:
        return False, {"error": {"kind": exc.code, "message": exc.message}}
    if edit.recipe is None:
        return True, {"recipe": None}
    return True, {"recipe": edit.recipe.to_json(), "levels_unknown": tag is not None and tag["levels"] is None}


def _cli_actual(tag_text: str, dat_path: Path | None, lib_path: Path | None) -> tuple[bool, dict[str, Any]]:
    args = ["camdata-camera-recipe", "--tag-hex", tag_text.encode("utf-8").hex()]
    if dat_path is not None:
        args.extend(["--dat", str(dat_path)])
    if lib_path is not None:
        args.extend(["--library", str(lib_path)])
    code, payload = run_json(args)
    if code == 0:
        return True, payload
    if code == 2 and isinstance(payload.get("error"), dict):
        return False, {"error": {"kind": payload["error"]["kind"], "message": payload["error"]["message"]}}
    pytest.fail(f"gxr-cli camdata-camera-recipe unexpected exit {code}", pytrace=False)
    raise AssertionError("unreachable")


def _normalize_wasm(res: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    if res["ok"]:
        return True, res["data"]
    err = res["data"].get("error", {})
    return False, {"error": {"kind": err.get("kind"), "message": err.get("message")}}


def test_camera_recipe_synthetic_cli_and_wasm_parity(tmp_path: Path) -> None:
    """Synthetic CLI == WASM == desktop checks for every CATALOG_GENERATIONS identity without DAT files."""
    require_cli()
    embedded = Menu()
    synthetic_tags = [
        "",
        "Copyright 2026 Plain Text",
        "GXR1 deadbeef00 50 01",
        "GXR1 6adf847602 50 99",
        *[f"GXR1 {identity} 50 01" for identity in CATALOG_GENERATIONS],
        *[f"GXR1 {identity} 28 04 A99999999-" for identity in CATALOG_GENERATIONS],
    ]
    wasm_results = _run_wasm_cases(tmp_path, [{"tag": t, "dat": None, "library": None} for t in synthetic_tags])
    for idx, (tag_text, wasm_res) in enumerate(zip(synthetic_tags, wasm_results, strict=True)):
        desk = _desktop_expected(tag_text, embedded)
        cli = _cli_actual(tag_text, None, None)
        wasm = _normalize_wasm(wasm_res)
        same_redacted(cli, desk, f"synthetic case #{idx} cli vs desktop")
        same_redacted(wasm, desk, f"synthetic case #{idx} wasm vs desktop")


def _bundle_map() -> dict[str, tuple[Path, Path]]:
    return {label: (dat_path, fx_path) for label, dat_path, fx_path in R5_BUNDLES}


def _pick_bundle_for_identity(identity: str, bundles: dict[str, tuple[Path, Path]]) -> tuple[str, Path, Path] | None:
    for label in IDENTITY_BUNDLE_ORDER.get(identity, ()):
        if label in bundles:
            dat_path, fx_path = bundles[label]
            return label, dat_path, fx_path
    return None


def _recipe_name_map(cam: CameraData) -> dict[tuple[str, str, str | None, int], str]:
    out: dict[tuple[str, str, str | None, int], str] = {}
    for unit_row in cam.listing():
        for rec in unit_row["recipes"]:
            slot = int(rec["slot"])
            kind = str(rec.get("kind", "builtin"))
            rid = rec.get("id")
            name = str(rec.get("name") or ("HC BW" if slot == 3 else f"slot:{slot}"))
            out[(str(unit_row["id"]), kind, str(rid) if rid else None, slot)] = name
    return out


def test_camera_recipe_real_captures_cli_wasm_desktop_parity(tmp_path: Path) -> None:
    """Acceptance #1: native CLI == WASM == desktop photo_edit on stable_sample + all card250 + all kubik_r6 + errors."""
    require_cli()
    captures_root = samples.captures()
    if not captures_root.is_dir():
        samples.missing("captures/ is not on this machine (GXR_STUDIO_TEST_PARENT)")
    bundles = _bundle_map()
    if "g250" not in bundles or "g250_kubik" not in bundles:
        samples.missing("g250 / g250_kubik local packages are not on this machine")

    # Build desktop CameraData once per bundle.
    desktop_cams: dict[str, CameraData] = {}
    for label, (dat_path, fx_path) in bundles.items():
        home = tmp_path / f"home-{label}"
        store.import_from_dat(dat_path.read_bytes(), home, library=fx_path.read_bytes())
        desktop_cams[label] = CameraData(home)
    wrong_gen_dat: Path | None = SINGLE_DATS[0][1] if SINGLE_DATS else None
    wrong_cam: CameraData | None = None
    if wrong_gen_dat is not None:
        home_wrong = tmp_path / "home-wrong"
        store.import_from_dat(wrong_gen_dat.read_bytes(), home_wrong)
        wrong_cam = CameraData(home_wrong)

    # Collect target DNGs: stable_sample of captures/ + ALL of card250 + ALL of kubik_r6.
    all_dngs = sorted(p for p in captures_root.rglob("*") if p.is_file() and p.suffix.upper() == ".DNG")
    sampled = samples.stable_sample(all_dngs, captures_root, 120)
    card250_dir = captures_root / "r6-field-20261118" / "card250"
    kubik_dir = captures_root / "r6-field-20261118" / "kubik_r6"
    if not card250_dir.is_dir() or not kubik_dir.is_dir():
        samples.missing("card250 or kubik_r6 folder is not on this machine")
    card250_dngs = sorted(p for p in card250_dir.iterdir() if p.is_file() and p.suffix.upper() == ".DNG")
    kubik_dngs = sorted(p for p in kubik_dir.iterdir() if p.is_file() and p.suffix.upper() == ".DNG")

    seen: set[Path] = set()
    ordered_dngs: list[Path] = []
    for path in [*card250_dngs, *kubik_dngs, *sampled]:
        if path not in seen:
            seen.add(path)
            ordered_dngs.append(path)

    # Gather tagged cases without ever exposing photo names or raw tags in assertions/reports.
    cases: list[dict[str, Any]] = []
    folder_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"raws": 0, "tagged": 0, "matched": 0, "mismatched": 0})
    card250_names_by_index: dict[int, str | None] = {}
    kubik_names_by_index: dict[int, str | None] = {}

    g250_names = _recipe_name_map(desktop_cams["g250"])
    kubik_names = _recipe_name_map(desktop_cams["g250_kubik"])

    for dng in ordered_dngs:
        folder_label = dng.parent.relative_to(captures_root).as_posix()
        folder_stats[folder_label]["raws"] += 1
        dm = meta.read_dng_meta(dng)
        tag_text = dm.copyright if dm.error is None and dm.copyright else None
        if not tag_text:
            jpg = dng.with_suffix(".JPG")
            if jpg.is_file():
                jm = meta.read_jpeg_meta(jpg)
                tag_text = jm.copyright if jm.error is None and jm.copyright else None
        tag = parse_recipe_tag(tag_text) if tag_text else None
        if tag is None or tag_text is None:
            continue
        folder_stats[folder_label]["tagged"] += 1
        picked = _pick_bundle_for_identity(tag["catalog_identity"], bundles)
        if picked is None:
            bundle_label = "g250"
            dat_path, fx_path = bundles["g250"]
        else:
            bundle_label, dat_path, fx_path = picked
        cases.append(
            {
                "kind": "real",
                "folder": folder_label,
                "index": folder_stats[folder_label]["raws"],
                "card250_idx": card250_dngs.index(dng) + 1 if dng in card250_dngs else None,
                "kubik_idx": kubik_dngs.index(dng) + 1 if dng in kubik_dngs else None,
                "bundle": bundle_label,
                "tag": tag_text,
                "dat": str(dat_path),
                "library": str(fx_path),
                "cam": desktop_cams[bundle_label],
            }
        )

    # Error parity cases on real g250 + wrong-generation packages (synthetic tags, never real tag text).
    g250_dat, g250_fx = bundles["g250"]
    error_cases: list[dict[str, Any]] = [
        # Missing catalog: g176 tag against g250 (lib_gen 178)
        {
            "label": "wrong_gen_176_on_250",
            "tag": "GXR1 d25d2f0a68 50 01",
            "dat": str(g250_dat),
            "library": str(g250_fx),
            "cam": desktop_cams["g250"],
        },
        # Unknown amount code ('Z' isn't valid, 'I' = 18 -> 180 not in amount_levels)
        {
            "label": "amount_unknown",
            "tag": "GXR1 6adf847602 50 47 I99999999-",
            "dat": str(g250_dat),
            "library": str(g250_fx),
            "cam": desktop_cams["g250"],
        },
        # Missing required knob ('-' in contrast slot) -> camera_knobs_mismatch
        {
            "label": "knobs_mismatch",
            "tag": "GXR1 6adf847602 50 47 A-9999999-",
            "dat": str(g250_dat),
            "library": str(g250_fx),
            "cam": desktop_cams["g250"],
        },
        # Non-neutral filter knob on color recipe -> knob_not_available
        {
            "label": "knob_not_available",
            "tag": "GXR1 6adf847602 50 47 A99999999A",
            "dat": str(g250_dat),
            "library": str(g250_fx),
            "cam": desktop_cams["g250"],
        },
        # Legacy tag without knob_code -> levels_unknown=True
        {
            "label": "levels_unknown",
            "tag": "GXR1 6adf847602 50 47",
            "dat": str(g250_dat),
            "library": str(g250_fx),
            "cam": desktop_cams["g250"],
        },
    ]
    if wrong_gen_dat is not None and wrong_cam is not None:
        error_cases.append(
            {
                "label": "wrong_gen_single_dat",
                "tag": "GXR1 6adf847602 50 47 A99999999-",
                "dat": str(wrong_gen_dat),
                "library": None,
                "cam": wrong_cam,
            }
        )

    all_wasm_inputs = [{"tag": c["tag"], "dat": c["dat"], "library": c["library"]} for c in [*cases, *error_cases]]
    wasm_outputs = _run_wasm_cases(tmp_path, all_wasm_inputs)

    for case, wasm_raw in zip(cases, wasm_outputs[: len(cases)], strict=True):
        desk = _desktop_expected(case["tag"], case["cam"])
        cli = _cli_actual(case["tag"], Path(case["dat"]), Path(case["library"]))
        wasm = _normalize_wasm(wasm_raw)
        label = f"{case['folder']} #{case['index']}"
        if cli == desk and wasm == desk:
            folder_stats[case["folder"]]["matched"] += 1
        else:
            folder_stats[case["folder"]]["mismatched"] += 1
            same_redacted(cli, desk, f"{label} cli vs desktop")
            same_redacted(wasm, desk, f"{label} wasm vs desktop")
        if desk[0] and desk[1].get("recipe"):
            rec = desk[1]["recipe"]
            key = (str(rec["unit"]), str(rec["kind"]), rec.get("id"), int(rec["slot"]))
            if case["card250_idx"] is not None:
                card250_names_by_index[case["card250_idx"]] = g250_names.get(key)
            if case["kubik_idx"] is not None:
                kubik_names_by_index[case["kubik_idx"]] = kubik_names.get(key)

    for err_case, wasm_raw in zip(error_cases, wasm_outputs[len(cases) :], strict=True):
        desk = _desktop_expected(err_case["tag"], err_case["cam"])
        cli = _cli_actual(
            err_case["tag"],
            Path(err_case["dat"]) if err_case["dat"] else None,
            Path(err_case["library"]) if err_case["library"] else None,
        )
        wasm = _normalize_wasm(wasm_raw)
        same_redacted(cli, desk, f"error case {err_case['label']} cli vs desktop")
        same_redacted(wasm, desk, f"error case {err_case['label']} wasm vs desktop")

    # Named checks (Acceptance #1):
    # - card250 files 1 and 3-10 -> Teal Orange
    for idx in (1, 3, 4, 5, 6, 7, 8, 9, 10):
        check(
            card250_names_by_index.get(idx) == "Teal Orange",
            f"card250 #{idx} expected Teal Orange, got {card250_names_by_index.get(idx)!r}",
        )
    # - card250 file 11 (XPan) -> HC BW (builtin slot 3)
    check(
        card250_names_by_index.get(11) == "HC BW",
        f"card250 #11 expected HC BW, got {card250_names_by_index.get(11)!r}",
    )
    # - KUBIK r6 photos -> K-0815 / K-PACIFIC / K-KINO, exact match with desktop
    check(len(kubik_names_by_index) == len(kubik_dngs) and len(kubik_dngs) > 0, "all kubik_r6 photos resolved")
    expected_kubik_set = {"K-0815", "K-PACIFIC", "K-KINO"}
    check(
        set(kubik_names_by_index.values()) == expected_kubik_set,
        f"kubik_r6 recipes expected {sorted(expected_kubik_set)}, got {sorted(set(kubik_names_by_index.values()))}",
    )

    # Optionally emit privacy-safe parity table counts for REPORT.md.
    report_out = os.environ.get("GXR_CAMERA_RECIPE_PARITY_OUT")
    if report_out:
        summary = {
            "folders": dict(sorted(folder_stats.items())),
            "error_cases": len(error_cases),
            "card250_checked": len(card250_names_by_index),
            "kubik_r6_checked": len(kubik_names_by_index),
        }
        Path(report_out).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
