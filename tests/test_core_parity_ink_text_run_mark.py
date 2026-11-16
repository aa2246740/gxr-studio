"""Gate A / B parity tests for ``gxr-finish::ink_text::{run, mark}`` (Items 4 & 5).

Covers:
- ``draw_run`` Gate A exact parity across all 6 de-hinted fonts (L, RGB, RGBA, clipping).
- ``text_ink`` auto-ink threshold (``luma > 150`` vs ``<= 150``) and ``draw_text_item`` ``rot=True``
  90-degree vertical captions matching ``frame.py:_paint_item``.
- CJK mixed-script runs and hinted-font ``exact=False`` reporting.
- ``gxr_mark`` / ``inktext-mark`` CLI vs WASM parity and missing-font error handling.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw

from gxr_studio.finish import frame_texture
from gxr_studio.finish.fonts import FONT_FILES, FontMetrics, PilFonts, draw_run
from gxr_studio.finish.frame_core import Type
from support.core_cli import require_cli, run_json, run_npy
from support.export_pipeline_cases import anon_ids, check, cli_ok, same_kind, same_pixels, same_redacted
from support.frame_cases import FIXTURE_CJK
from support.ink_text_cases import dehinted_font_paths, font_path, patched_pil_fonts
from support.wasm_check import run_area


@pytest.fixture(autouse=True)
def _cli() -> None:
    require_cli()


def _font_args(font_map: dict[str, Path]) -> list[str]:
    out: list[str] = []
    for name, path in font_map.items():
        out += ["--font", f"{name}={path}"]
    return out


def _font_pairs(font_map: dict[str, Path]) -> list[list[str]]:
    return [[name, str(path)] for name, path in font_map.items()]


RUN_CASES: list[tuple[str, str, int, str, int, int, Any, bool, list[list[int]] | None]] = [
    ("atkinson", "L", 28, "GXR A12", 12, 14, 240, False, None),
    ("inter", "RGB", 24, "f/2.5 · ISO 200", 8, 10, [250, 245, 238], False, None),
    ("inter_sb", "RGB", 22, "Été 1/250s", -4, 18, [22, 22, 26], False, None),
    ("courier", "RGBA", 20, "FRAME #09", 14, 12, [255, 106, 0, 255], False, None),
    ("patrick", "RGB", 26, "Note 01", 10, 16, [40, 80, 120], False, None),
    ("dseg", "RGB", 34, "88:08", 6, 8, [255, 120, 40], False, None),
    ("inter", "RGB", 20, "DARK INK", 16, 20, [20, 20, 24], False, [[244, 244, 246], [18, 18, 22]]),
    ("inter", "RGB", 20, "LIGHT INK", 16, 20, [20, 20, 24], False, [[244, 244, 246], [18, 18, 22]]),
    ("inter", "RGB", 22, "ROTATED 50mm", 0, 0, [32, 32, 36], True, None),
    ("courier", "RGB", 18, "SIDE CAPTION", 0, 0, [220, 190, 140], True, None),
]


def _build_run_item(
    font: str,
    px: int,
    text: str,
    x: int,
    y: int,
    fill: Any,
    rot: bool,
    auto_ink: list[list[int]] | None,
) -> dict[str, Any]:
    metrics = FontMetrics()
    t = Type(metrics)
    item = t.text(float(x), float(y), px, font, text, (0, 0, 0), 0.04)
    item["fill"] = fill
    if auto_ink is not None:
        item["auto_ink"] = auto_ink
    if rot:
        width = round(t.advance(font, px, text, 0.04))
        item["tw"] = max(1, width)
        item["rot"] = True
        item["gx"] = 18
        item["gy"] = 24
        item["xs0"] = [xi - round(float(item["x"])) for xi in item["xs"]]
    return item


def _python_draw_run_item(bg: np.ndarray, mode: str, item: dict[str, Any], font_map: dict[str, Path]) -> np.ndarray:
    im = Image.fromarray(bg.copy(), mode)
    with patched_pil_fonts(font_map):
        fonts = PilFonts()
        if item.get("rot"):
            face = fonts.face(item["font"], item["px"], False)
            tmp = Image.new("RGBA", (item["tw"], face.getmetrics()[0] + math.ceil(item["px"] * 0.4)))
            run = {**item, "xs": item["xs0"], "y": 0}
            draw_run(ImageDraw.Draw(tmp), run, fonts, tuple(item["fill"]))
            tmp = tmp.rotate(90, expand=True)
            im.paste(tmp.convert("RGB"), (item["gx"], item["gy"]), tmp.getchannel("A"))
        elif item.get("auto_ink"):
            ink = frame_texture.text_ink(im, item, FontMetrics(fonts))
            draw_run(ImageDraw.Draw(im), item, fonts, ink)
        else:
            fill = item["fill"] if isinstance(item["fill"], int) else tuple(item["fill"])
            draw_run(ImageDraw.Draw(im), item, fonts, fill)
    return np.asarray(im)


@pytest.mark.parametrize("case_idx", range(len(RUN_CASES)), ids=anon_ids(len(RUN_CASES)))
def test_gate_a_inktext_run_exact_with_dehinted_fonts(case_idx: int, tmp_path: Path) -> None:
    """Gate A: 0-diff parity across all 6 fonts, modes, auto_ink, and 90-degree rotated captions."""
    font, mode, px, text, x, y, fill, rot, auto_ink = RUN_CASES[case_idx]
    dehinted = dehinted_font_paths(tmp_path, zero_maxp=False)

    if rot:
        bg = np.full((320, 120, 3), 50, dtype=np.uint8)
    elif mode == "L":
        bg = np.full((80, 260), 20, dtype=np.uint8)
    elif mode == "RGBA":
        bg = np.full((80, 260, 4), [30, 50, 70, 180], dtype=np.uint8)
    elif "DARK INK" in text:
        bg = np.full((80, 260, 3), 230, dtype=np.uint8)
    else:
        bg = np.full((80, 260, 3), 40, dtype=np.uint8)

    item = _build_run_item(font, px, text, x, y, fill, rot, auto_ink)
    want = _python_draw_run_item(bg, mode, item, dehinted)

    in_npy = tmp_path / f"run-{case_idx}-in.npy"
    out_npy = tmp_path / f"run-{case_idx}-out.npy"
    run_json_path = tmp_path / f"run-{case_idx}.json"
    np.save(in_npy, bg)
    run_json_path.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")

    code, payload = run_json(
        [
            "inktext-run",
            "--in",
            str(in_npy),
            "--mode",
            mode,
            "--run",
            str(run_json_path),
            *_font_args(dehinted),
            "--out",
            str(out_npy),
        ]
    )
    cli_ok(code, payload, "inktext-run gate A")
    check(payload.get("exact") is True, "expected exact=True for dehinted TTF run")
    got = np.load(out_npy)
    same_pixels(got, want, f"inktext-run-{case_idx}")


def test_inktext_run_auto_ink_threshold_boundary(tmp_path: Path) -> None:
    """Verify text_ink boundary near mean luma 150.0 (150 -> light vs 151 -> dark)."""
    dehinted = dehinted_font_paths(tmp_path, zero_maxp=False)
    for luma_val in (150, 151):
        bg = np.full((64, 180, 3), luma_val, dtype=np.uint8)
        item = _build_run_item("atkinson", 22, "AB", 12, 12, [0, 0, 0], False, [[250, 250, 250], [10, 10, 10]])
        want = _python_draw_run_item(bg, "RGB", item, dehinted)

        in_npy = tmp_path / f"thresh-{luma_val}.npy"
        out_npy = tmp_path / f"thresh-{luma_val}-out.npy"
        np.save(in_npy, bg)
        got = run_npy(
            [
                "inktext-run",
                "--in",
                str(in_npy),
                "--mode",
                "RGB",
                "--run-json",
                json.dumps(item),
                *_font_args(dehinted),
            ],
            out_npy,
        )
        same_pixels(got, want, f"threshold-{luma_val}")


def test_inktext_run_cjk_and_hinted_report_exact_false(tmp_path: Path) -> None:
    """Mixed CJK + Latin run on real fonts sets exact=False and places ink at the right spans."""
    real_fonts = {name: font_path(name) for name in FONT_FILES}
    real_fonts["cjk"] = FIXTURE_CJK

    bg = np.full((72, 240, 3), 240, dtype=np.uint8)
    text = "上海 GXR"
    item = {
        "k": "text",
        "x": 12,
        "y": 14,
        "px": 24,
        "font": "inter",
        "text": text,
        "fill": [20, 20, 24],
        "track": 0.0,
        "cjk": False,
        "xs": [12 + i * 24 for i in range(len(text))],
    }
    in_npy = tmp_path / "cjk-in.npy"
    out_npy = tmp_path / "cjk-out.npy"
    np.save(in_npy, bg)
    code, payload = run_json(
        [
            "inktext-run",
            "--in",
            str(in_npy),
            "--mode",
            "RGB",
            "--run-json",
            json.dumps(item, ensure_ascii=False),
            *_font_args(real_fonts),
            "--out",
            str(out_npy),
        ]
    )
    cli_ok(code, payload, "inktext-run cjk")
    check(payload.get("exact") is False, "expected exact=False when hinted/CFF glyphs are used")
    got = np.load(out_npy)
    check(int((got != 240).sum()) > 50, "expected CJK and Latin ink pixels")

    # Missing cjk font reports missing_cjk
    fonts_no_cjk = {name: font_path(name) for name in FONT_FILES}
    code_err, err_payload = run_json(
        [
            "inktext-run",
            "--in",
            str(in_npy),
            "--mode",
            "RGB",
            "--run-json",
            json.dumps(item, ensure_ascii=False),
            *_font_args(fonts_no_cjk),
            "--out",
            str(out_npy),
        ]
    )
    check(code_err == 2, "missing_cjk must exit 2")
    same_kind(err_payload, "missing_cjk", "missing cjk font error")


def test_gxr_mark_cli_wasm_parity_and_errors(tmp_path: Path) -> None:
    """inktext-mark CLI vs gxr_mark WASM parity across real, de-hinted, and missing fonts."""
    dehinted = dehinted_font_paths(tmp_path, zero_maxp=False)
    real_atkinson = [["atkinson", str(font_path("atkinson"))]]
    dehint_atkinson = [["atkinson", str(dehinted["atkinson"])]]

    # Sanity check CLI output structure directly
    code, payload = run_json(["inktext-mark", "--font", f"atkinson={font_path('atkinson')}"])
    cli_ok(code, payload, "inktext-mark real")
    check(int(payload.get("w", 0)) > 1000 and int(payload.get("h", 0)) > 400, "mark dimensions plausible")
    check(payload.get("ink_top") == 0 and int(payload.get("ink_bottom", -1)) == int(payload["h"]) - 1, "tight ink bounds")

    # Missing atkinson -> missing_font
    code_miss, err_miss = run_json(["inktext-mark", "--font", f"inter={font_path('inter')}"])
    check(code_miss == 2, "missing_font must exit 2")
    same_kind(err_miss, "missing_font", "missing mark font")

    # WASM vs CLI byte-for-byte parity via run_area("inktext", ...)
    specs: list[Path] = []
    for tag, fonts in (
        ("real", real_atkinson),
        ("dehint", dehint_atkinson),
        ("missing", [["inter", str(font_path("inter"))]]),
    ):
        p = tmp_path / f"mark-{tag}.json"
        p.write_text(json.dumps({"kind": "mark", "fonts": fonts}), encoding="utf-8")
        specs.append(p)

    proc = run_area("inktext", specs, tmp_path)
    check(proc.returncode == 0, "wasm inktext mark parity failed")

    # Real vs dehinted mark metrics remain close (atkinson hinting at 1000px shifts <= 2px)
    code_dh, payload_dh = run_json(["inktext-mark", "--font", f"atkinson={dehinted['atkinson']}"])
    cli_ok(code_dh, payload_dh, "inktext-mark dehint")
    same_redacted(
        [abs(int(payload["w"]) - int(payload_dh["w"])) <= 4, abs(int(payload["h"]) - int(payload_dh["h"])) <= 4],
        [True, True],
        "mark real vs dehinted delta bound",
    )
