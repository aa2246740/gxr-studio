"""What the desktop plans for `source-selection`, read off `finish.serve.color_image`.

The hint is the recipe `color_image` passes to `RenderService.plan`, or null when it never
calls `plan`. `neutral` always renders NEUTRAL (`serve.py`, `neutral=True`) and does not
plan, with or without calibration, camera data, or an edit. `auto` on a RAW-only photo
still plans the unit's stock 04.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gxr_studio.finish import serve
from gxr_studio.render import service
from gxr_studio.render.params import Adjust, EditState, RecipeSel
from gxr_studio.render.recipes import unit_of_lens
from support import export_source_cases as xs
from support.core_cli import run_json

HOMES = ("all", "nocal", "nodat")
LENS_50 = "GR LENS A12 50mm F2.5 MACRO"
LENS_28 = "RICOH LENS A12 28mm F2.5"


@dataclass(frozen=True)
class Case:
    name: str
    shot_key: str
    edit_key: str
    base: str
    expect: str
    unit: str = "jlaunch"
    slot: int = 4
    homes: tuple[str, ...] = HOMES


def shots(files: dict[str, Path]) -> dict[str, dict[str, Any]]:
    dng, jpg = files["ok"], files["jpg"]
    return {
        "raw-50": xs.shot_for(dng, None, lens=LENS_50, iso=200),
        "raw-28": xs.shot_for(dng, None, lens=LENS_28, iso=200),
        "raw-none": xs.shot_for(dng, None, lens=None, iso=200),
        "pair-50": xs.shot_for(dng, jpg, lens=LENS_50, iso=200),
        # Sol's probe: the DNG path does not have to exist for the hint itself.
        "review": {"files": {"dng": {"relpath": "R0000001.DNG"}}, "lens": LENS_50, "iso": 200},
    }


def edits() -> dict[str, EditState | str]:
    return {
        "empty": EditState(),
        "braces": "{}",
        "recipe": EditState(recipe=RecipeSel("qlaunch", 2)),
        "adjust": EditState(adjust=Adjust(exposure_ev=0.7)),
        "both": EditState(recipe=RecipeSel("jlaunch", 6), adjust=Adjust(exposure_ev=-0.3)),
    }


def matrix() -> list[Case]:
    """Neutral across the homes, plus the neighbours that must not become null."""
    cases = [
        Case(f"neutral/{shot}/{edit}", shot, edit, "neutral", "neutral")
        for shot in ("raw-50", "raw-28", "raw-none", "pair-50")
        for edit in ("empty", "braces", "recipe", "adjust", "both")
    ]
    cases.append(Case("neutral/review/braces", "review", "braces", "neutral", "null"))
    cases.extend(
        [
            Case("auto/raw-50/empty", "raw-50", "empty", "auto", "stock"),
            Case("auto/raw-28/empty", "raw-28", "empty", "auto", "stock", unit="qlaunch"),
            Case("auto/raw-none/empty", "raw-none", "empty", "auto", "stock"),
            Case("auto/raw-50/adjust", "raw-50", "adjust", "auto", "stock"),
            Case("auto/raw-50/recipe", "raw-50", "recipe", "auto", "recipe", unit="qlaunch", slot=2),
            Case("auto/raw-50/both", "raw-50", "both", "auto", "recipe", slot=6),
            Case("auto/pair-50/empty", "pair-50", "empty", "auto", "camera-jpg"),
            Case("standard/raw-50/empty", "raw-50", "empty", "standard", "stock"),
            Case("standard/raw-50/recipe", "raw-50", "recipe", "standard", "stock"),
            Case("standard/raw-50/both", "raw-50", "both", "standard", "stock"),
            Case("camera-jpg/pair-50/empty", "pair-50", "empty", "camera-jpg", "camera-jpg"),
            Case("camera-jpg/raw-50/empty", "raw-50", "empty", "camera-jpg", "null"),
            Case("recipe/raw-50/recipe", "raw-50", "recipe", "recipe", "recipe", unit="qlaunch", slot=2),
            Case("recipe/raw-50/empty", "raw-50", "empty", "recipe", "stock"),
        ]
    )
    return cases


def desktop_hint(render: Any, lib: Any, shot: dict[str, Any], edit: EditState, base: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The recipe `color_image` plans, or None when `plan` is not called, plus the outcome.

    A recipe render would bake for about a second. The bake is refused after `plan` returns,
    so the selection is still observed. `neutral` never bakes, and its labels are the real ones.
    """
    seen: list[EditState] = []
    real_plan = render.plan
    real_bake = service.chain.bake_chroma

    def spy(shot_arg: dict[str, Any], edit_arg: EditState) -> Any:
        seen.append(edit_arg)
        return real_plan(shot_arg, edit_arg)

    def refuse_bake(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("bake skipped")

    render.plan = spy
    service.chain.bake_chroma = refuse_bake
    try:
        try:
            _rgb, from_raw, labels = serve.color_image(render, shot, edit, max_edge=None, quality=95, lib=lib, base=base, full=True)
            outcome: dict[str, Any] = {
                "from_raw": from_raw,
                "id": labels.get("id"),
                "text": labels.get("text"),
                "calibrated": labels.get("calibrated"),
                "description": labels.get("description") or "",
            }
        except Exception as exc:  # noqa: BLE001  the hint is defined even when the picture cannot be opened
            outcome = {"error": type(exc).__name__}
    finally:
        del render.plan
        service.chain.bake_chroma = real_bake
    if not seen:
        return None, outcome
    used = seen[0]
    sel = used.recipe or RecipeSel(unit_of_lens(shot.get("lens")) or "jlaunch", 4)
    return {"kind": sel.kind, "unit": sel.unit, "slot": sel.slot, "defaulted": used.recipe is None}, outcome


def cli_selection(shot: dict[str, Any], edit: EditState | str, base: str, tmp: Path, space: Path | None = None) -> Any:
    shot_path = xs.write_json(tmp / "shot.json", shot)
    if isinstance(edit, str):
        edit_path = tmp / "edit.json"
        edit_path.write_text(edit, encoding="utf-8")
    else:
        edit_path = xs.write_json(tmp / "edit.json", xs.edit_json(edit))
    args = ["source-selection", "--shot", str(shot_path), "--edit", str(edit_path), "--base", base]
    if space is not None:
        args += ["--space", str(space)]
    code, payload = run_json(args)
    assert code == 0, payload
    return payload["selection"]


def _expect(case: Case, desktop: dict[str, Any] | None, outcome: dict[str, Any]) -> None:
    if case.expect == "neutral":
        assert desktop is None, (case.name, desktop)
        assert "error" not in outcome, (case.name, outcome.get("error"))
        assert outcome["id"] == "neutral-preview" and outcome["text"] == "中性转换", (case.name, outcome["id"], outcome["text"])
        assert outcome["from_raw"] is False and outcome["calibrated"] == "0", (case.name, outcome["from_raw"], outcome["calibrated"])
        assert outcome["description"].startswith("GXR Studio neutral-preview"), case.name
        assert "stock 04" not in outcome["description"], case.name
        return
    if case.expect == "stock":
        assert desktop == {"kind": "builtin", "unit": case.unit, "slot": case.slot, "defaulted": True}, (case.name, desktop)
        return
    if case.expect == "recipe":
        assert desktop == {"kind": "builtin", "unit": case.unit, "slot": case.slot, "defaulted": False}, (case.name, desktop)
        return
    if case.expect == "camera-jpg":
        assert desktop is None, (case.name, desktop)
        assert outcome.get("id") == "camera-jpg" and outcome.get("from_raw") is False, (case.name, outcome.get("id"))
        return
    assert case.expect == "null", case.expect
    assert desktop is None, (case.name, desktop)


def run_matrix(homes: dict[str, Any], files: dict[str, Path], tmp: Path) -> int:
    """Compare the CLI hint with the desktop for every case and every home. Returns the count."""
    built, edit_map = shots(files), edits()
    n = 0
    for case in matrix():
        shot = built[case.shot_key]
        edit = edit_map[case.edit_key]
        desktop_edit = EditState() if isinstance(edit, str) else edit
        for home in case.homes:
            render = homes[home].render
            desktop, outcome = desktop_hint(render, homes[home].lib, shot, desktop_edit, case.base)
            cli = cli_selection(shot, edit, case.base, tmp)
            assert cli == desktop, (case.name, home, cli, desktop)
            _expect(case, desktop, outcome)
            n += 1
    return n


def write_wasm_specs(files: dict[str, Path], tmp: Path) -> list[Path]:
    """One `source-selection` spec per case. The hint does not read calibration or the DAT."""
    built, edit_map = shots(files), edits()
    specs: list[Path] = []
    for case in matrix():
        folder = tmp / case.name
        folder.mkdir(parents=True, exist_ok=True)
        shot_path = xs.write_json(folder / "shot.json", built[case.shot_key])
        edit = edit_map[case.edit_key]
        edit_path = folder / "edit.json"
        if isinstance(edit, str):
            edit_path.write_text(edit, encoding="utf-8")
        else:
            xs.write_json(edit_path, xs.edit_json(edit))
        specs.append(
            xs.write_json(
                folder / "spec.json",
                {"kind": "selection", "shot": str(shot_path), "edit": str(edit_path), "base": case.base},
            )
        )
    return specs


_SPACE_FIELDS = {"output_space": "srgb", "input_gamut": None, "output_gamut": None, "input_range": "full", "output_range": "full"}
LUT_SPACES = {"scene": {"input_space": "slog3", **_SPACE_FIELDS}, "display": {"input_space": "srgb", **_SPACE_FIELDS}}


def _lut_rows() -> list[tuple[str, str, str, str]]:
    """(name, lut kind, base, expect) for one RAW-only photo.

    A scene LUT never plans. A display LUT keeps the no-LUT rule for that base.
    """
    rows = []
    for kind in ("scene", "display"):
        for base in ("auto", "neutral", "standard"):
            expect = "scene" if kind == "scene" else ("neutral" if base == "neutral" else "stock")
            rows.append((f"{kind}/{base}", kind, base, expect))
    rows.append(("scene/auto/recipe", "scene", "auto", "scene"))
    rows.append(("display/auto/recipe", "display", "auto", "recipe"))
    return rows


def _prepare_luts(tmp: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    from support.export_source_run import Lut
    from support.lut_cases import cube_text

    text = cube_text(size=2)
    luts: dict[str, Any] = {}
    spaces: dict[str, Path] = {}
    for name, spec in LUT_SPACES.items():
        luts[name] = Lut(tmp, text, spec, name)
        spaces[name] = xs.write_json(tmp / f"{name}-space.json", spec)
    return luts, spaces


def _lut_edit(kind: str, named: str) -> EditState:
    recipe = RecipeSel("qlaunch", 2) if named.endswith("/recipe") else None
    return EditState(recipe=recipe, lut=kind)


def _expect_lut(name: str, expect: str, desktop: dict[str, Any] | None, outcome: dict[str, Any]) -> None:
    if expect == "scene":
        assert desktop is None, (name, desktop)
        assert "error" not in outcome, (name, outcome.get("error"))
        assert outcome.get("id") == "lut-prediction" and outcome.get("from_raw") is False, (name, outcome.get("id"))
        return
    if expect == "neutral":
        assert desktop is None, (name, desktop)
        assert outcome.get("id") == "lut-prediction" and outcome.get("from_raw") is False, (name, outcome.get("id"))
        return
    if expect == "recipe":
        assert desktop == {"kind": "builtin", "unit": "qlaunch", "slot": 2, "defaulted": False}, (name, desktop)
        return
    assert expect == "stock", expect
    assert desktop == {"kind": "builtin", "unit": "jlaunch", "slot": 4, "defaulted": True}, (name, desktop)


def run_lut_matrix(homes: dict[str, Any], files: dict[str, Path], tmp: Path) -> int:
    """Scene LUT hint is null. Display LUT hint matches the desktop's no-LUT rule."""
    shot = shots(files)["raw-50"]
    luts, spaces = _prepare_luts(tmp)
    n = 0
    for name, kind, base, expect in _lut_rows():
        edit = _lut_edit(kind, name)
        for home in HOMES:
            render = homes[home].render
            saved = render.luts
            render.luts = luts[kind]
            try:
                desktop, outcome = desktop_hint(render, homes[home].lib, shot, edit, base)
            finally:
                render.luts = saved
            cli = cli_selection(shot, edit, base, tmp, spaces[kind])
            assert cli == desktop, (name, home, cli, desktop)
            _expect_lut(name, expect, desktop, outcome)
            n += 1
    return n


def write_lut_wasm_specs(files: dict[str, Path], tmp: Path) -> list[Path]:
    """The same LUT rows, one native/WASM spec each. The hint does not read the DAT."""
    shot_path = xs.write_json(tmp / "shot.json", shots(files)["raw-50"])
    _luts, spaces = _prepare_luts(tmp)
    specs: list[Path] = []
    for name, kind, base, _expect in _lut_rows():
        folder = tmp / name
        folder.mkdir(parents=True, exist_ok=True)
        edit_path = xs.write_json(folder / "edit.json", xs.edit_json(_lut_edit(kind, name)))
        specs.append(
            xs.write_json(
                folder / "spec.json",
                {
                    "kind": "selection",
                    "shot": str(shot_path),
                    "edit": str(edit_path),
                    "base": base,
                    "space": str(spaces[kind]),
                },
            )
        )
    # Sol r2: a `--space` file that is not UTF-8 must give the same CoreError natively and in WASM.
    for kind, base in (("scene", "auto"), ("display", "neutral"), ("display", "standard")):
        folder = tmp / "bad-space" / f"{kind}-{base}"
        folder.mkdir(parents=True, exist_ok=True)
        bad = folder / "space.json"
        bad.write_bytes(b"\xff")
        edit_path = xs.write_json(folder / "edit.json", xs.edit_json(_lut_edit(kind, f"{kind}/{base}")))
        specs.append(
            xs.write_json(
                folder / "spec.json",
                {"kind": "selection", "shot": str(shot_path), "edit": str(edit_path), "base": base, "space": str(bad)},
            )
        )
    return specs
