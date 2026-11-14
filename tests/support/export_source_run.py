"""Run one export-source case on both sides: the real desktop Python, and ``gxr-cli source-*``.

Each runner returns ``(python, rust)`` in the same shape so a test can assert equality and
print the first difference.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from gxr_studio.camdata import store
from gxr_studio.finish import serve
from gxr_studio.raw import open_dng, render_full
from gxr_studio.render import service
from gxr_studio.render.params import EditState, RecipeSel
from gxr_studio.render.recipes import unit_of_lens
from gxr_studio.render.service import RenderUnavailable
from support import export_source_cases as xs
from support.core_cli import CLI, error_of, run_json

LOCAL_REFUSAL = {"kind": "local_recipe_unsupported", "message": "手机上还不能用电脑里的本机配方。", "state": "local_recipe"}


def selection(shot: dict[str, Any], recipe: RecipeSel | None) -> RecipeSel:
    return recipe or RecipeSel(unit_of_lens(shot.get("lens")) or "jlaunch", 4)


def _common_args(h: xs.Home, tmp: Path, sel: RecipeSel | None) -> list[str]:
    args: list[str] = []
    if h.dat is not None:
        args += ["--dat", str(h.dat)]
    if h.calib.is_dir():
        args += ["--calib", str(h.calib)]
    entry = None
    if sel is not None and sel.kind != "local":
        try:
            entry = h.entry(sel)
        except Exception:  # noqa: BLE001  resolve errors belong to the resolve unit
            entry = None
    if entry is not None:
        e = tmp / "entry.bin"
        e.write_bytes(entry)
        args += ["--entry", str(e)]
    args += ["--binding", h.binding()]
    return args


def plan_case(h: xs.Home, tmp: Path, shot: dict[str, Any], edit: EditState, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """`RenderService.plan` + the renderer's labels vs `source-plan`."""
    s = xs.write_json(tmp / "shot.json", shot)
    e = xs.write_json(tmp / "edit.json", xs.edit_json(edit))
    sel = selection(shot, edit.recipe)
    code, payload = run_json(["source-plan", "--shot", str(s), "--edit", str(e), *_common_args(h, tmp, sel)])
    try:
        p = h.render.plan(shot, edit)
        # The description does not depend on the bake; skip the 1 s bake per case.
        monkeypatch.setattr(service.chain, "bake_chroma", lambda *a, **k: None)
        r = h.render.renderer(shot, edit)
        monkeypatch.undo()
    except RenderUnavailable as exc:
        return {"error": {"kind": exc.code, "message": exc.message, "state": exc.state}}, payload
    py = {
        "selection": {"kind": sel.kind, "unit": sel.unit, "slot": sel.slot, "defaulted": edit.recipe is None},
        "plan": {
            "unit": p.unit,
            "slot": p.slot,
            "defaulted": p.defaulted,
            "entry_sha256": p.entry_sha256,
            "model_iso": p.model_iso,
            "calibrated": p.calibrated,
            "reasons": list(p.reasons),
            "label": p.label,
            "record_sha256": p.record_sha256,
            "binding_sha256": p.binding_sha256,
            "selection_id": p.selection_id,
        },
        "renderer": {
            "id": r.id,
            "label": r.label,
            "filename_tag": r.filename_tag,
            "calibrated": r.calibrated,
            "description": r.description,
            "recipe_binding": r.recipe_binding,
        },
    }
    assert (code == 0) == ("error" not in payload), payload
    return py, payload


def render_case(h: xs.Home, tmp: Path, dng: Path, shot: dict[str, Any], edit: EditState) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """`render_full(open_dng(path), renderer)` vs `source-render` (same entry, pack, ISO, adjust)."""
    renderer = h.render.renderer(shot, edit)
    plan = h.render.plan(shot, edit)
    d = open_dng(dng)
    try:
        py = render_full(d, renderer)
    finally:
        d.release()
    entry, stock = tmp / "entry.bin", tmp / "stock.bin"
    entry.write_bytes(plan.entry)
    stock.write_bytes(store.load_stock04(h.home, plan.unit))
    a = xs.write_json(tmp / "adjust.json", xs.adjust_json(edit.adjust, edit.curve))
    out = tmp / "render.npy"
    args = ["source-render", "--dng", str(dng), "--entry", str(entry), "--stock", str(stock), "--unit", plan.unit]
    args += ["--calib", str(h.calib), "--iso", str(plan.model_iso), "--adjust", str(a), "--out", str(out)]
    code, payload = run_json(args, timeout=300)
    err = error_of(code, payload)
    assert err is None, (dng.name, err)
    rs = np.load(out)
    return py, rs, {"model_iso": plan.model_iso, "unit": plan.unit, "slot": plan.slot, "args": args}


class Lut:
    """A LUT the edit names: Python sees it through `render.luts.target`, Rust as files."""

    def __init__(self, tmp: Path, cube_text: str, spec: dict[str, Any], lut_id: str = "cube-1") -> None:
        from gxr_studio.lut.cube import Cube
        from gxr_studio.lut.spaces import Target

        self.id = lut_id
        self.cube = tmp / f"{lut_id}.cube"
        self.cube.write_text(cube_text, encoding="utf-8")
        self.spec_path = xs.write_json(tmp / f"{lut_id}-space.json", spec)
        self.spec = spec
        self.item = {**spec, "id": lut_id}
        self._cube = Cube.read(self.cube)
        self._target = Target

    def target(self, cube_id: str, **_kw: Any) -> Any:
        from gxr_studio.lut.errors import LutError

        if self.spec.get("input_space") is None:
            raise LutError(422, "lut_space_required", "请选择输入色彩空间")
        s = self.spec
        t = self._target(
            self._cube,
            s["input_space"],
            s.get("output_space", "srgb"),
            s.get("input_gamut"),
            s.get("output_gamut"),
            s.get("input_range", "full"),
            s.get("output_range", "full"),
        )
        return t, self.item


def color_case(
    h: xs.Home,
    tmp: Path,
    shot: dict[str, Any],
    edit: EditState,
    base: str,
    *,
    preview: np.ndarray | None = None,
    lut: Lut | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> tuple[dict[str, Any], dict[str, Any], np.ndarray | None, np.ndarray | None]:
    """`finish.serve.color_image(..., full=True)` vs `source-color-image`.

    Returns (python result, rust result, python rgb, rust rgb). A result is
    `{"from_raw", "labels"}` or `{"error": {...}}`."""
    files = shot["files"]
    has_jpg = "jpg" in files
    s = xs.write_json(tmp / "shot.json", shot)
    e = xs.write_json(tmp / "edit.json", xs.edit_json(edit))
    out = tmp / "color.npy"
    args = ["source-color-image", "--shot", str(s), "--edit", str(e), "--base", base, "--out", str(out)]
    picked = json.loads(_selection(shot, e, base, tmp))["selection"]
    sel = None if picked is None else RecipeSel(picked["unit"], picked["slot"], kind=picked["kind"])
    if sel is not None and edit.recipe is not None and sel == RecipeSel(edit.recipe.unit, edit.recipe.slot, kind=edit.recipe.kind):
        sel = edit.recipe
    if lut is not None and sel is None:
        # A display LUT re-runs color_image with the same base; resolve the default too.
        sel = selection(shot, edit.recipe)
    args += _common_args(h, tmp, sel)
    if "dng" in files:
        args += ["--dng", files["dng"]["relpath"]]
    if has_jpg:
        args += ["--jpg", str(xs.save_rgb(tmp / "jpg.npy", xs.decoded_jpg(Path(files["jpg"]["relpath"]))))]
    if preview is not None:
        args += ["--preview", str(xs.save_rgb(tmp / "preview.npy", preview))]
    if lut is not None:
        args += ["--cube", str(lut.cube), "--space", str(lut.spec_path)]
    code, payload = run_json(args, timeout=300)
    rs_rgb = np.load(out) if code == 0 else None
    rust = payload if code != 0 else {"from_raw": payload["from_raw"], "labels": payload["labels"], "path": payload["path"]}

    patched = monkeypatch is not None
    if patched:
        monkeypatch.setattr(serve, "_embedded_rgb", lambda _path: preview)
        # `bake_chroma` is a pure function of (model, entry, step); reuse it across cases.
        monkeypatch.setattr(service.chain, "bake_chroma", _cached_bake)
        if lut is not None:
            monkeypatch.setattr(h.render, "luts", lut)
    try:
        rgb, from_raw, labels = serve.color_image(h.render, shot, edit, max_edge=None, quality=95, lib=h.lib, base=base, full=True)
        python: dict[str, Any] = {"from_raw": from_raw, "labels": labels}
        py_rgb: np.ndarray | None = np.asarray(rgb)
    except Exception as exc:  # noqa: BLE001  mapped like the CLI's error JSON
        python, py_rgb = {"error": xs.python_error(exc, has_jpg)}, None
    finally:
        if patched:
            monkeypatch.undo()
    return python, rust, py_rgb, rs_rgb


_BAKES: dict[tuple[int, bytes, float], Any] = {}
_REAL_BAKE = service.chain.bake_chroma


def _cached_bake(model: Any, entry: bytes, step: float) -> Any:
    key = (id(model), bytes(entry), float(step))
    hit = _BAKES.get(key)
    if hit is None or hit[0] is not model:
        hit = _BAKES[key] = (model, _REAL_BAKE(model, entry, step))
    return hit[1]


def _selection(shot: dict[str, Any], edit_path: Path, base: str, tmp: Path) -> str:
    import subprocess  # nosec B404

    s = xs.write_json(tmp / "shot.json", shot)
    proc = subprocess.run(
        [str(CLI), "source-selection", "--shot", str(s), "--edit", str(edit_path), "--base", base], capture_output=True, text=True, check=True
    )  # noqa: S603  # nosec B603
    return proc.stdout


def assert_same_result(python: dict[str, Any], rust: dict[str, Any], py_rgb: np.ndarray | None, rs_rgb: np.ndarray | None, label: str) -> None:
    """Labels in the same key order, `from_raw`, errors and the uint8 picture: exact."""
    if "error" in python:
        if python["error"].get("state") == "local_recipe":
            # The desktop resolves local recipes in its library; the phone has none and refuses
            # with its own code. Same exception class (RenderUnavailable) and state.
            assert rust == {"error": LOCAL_REFUSAL}, (label, python, rust)
            return
        assert rust == python, (label, python, rust)
        return
    assert "error" not in rust, (label, python, rust)
    assert rust["from_raw"] is python["from_raw"], (label, python["from_raw"], rust["from_raw"])
    assert list(rust["labels"].items()) == list(python["labels"].items()), (label, python["labels"], rust["labels"])
    assert py_rgb is not None and rs_rgb is not None
    assert rs_rgb.dtype == np.uint8 and py_rgb.dtype == np.uint8, (label, py_rgb.dtype, rs_rgb.dtype)
    assert rs_rgb.shape == py_rgb.shape, (label, py_rgb.shape, rs_rgb.shape)
    bad = np.argwhere(rs_rgb != py_rgb)
    assert len(bad) == 0, (label, len(bad), [(tuple(int(v) for v in i), int(py_rgb[tuple(i)]), int(rs_rgb[tuple(i)])) for i in bad[:5]])
