"""Read-only research reference replay. Outputs contain private calibration; keep --out local.

Run with python -I -B, nice 15, loadgate admission and the shared heavy lock.
No research code is patched or imported through its worker/admission entry points.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import numpy as np

from gxr_studio.camdata import calib, datcodec, store
from gxr_studio.engine import colour, knobs
from gxr_studio.engine.model import Model


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def functions(path, names, scope):
    tree = ast.parse(path.read_text())
    tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in tree.body} == set(names)
    exec(compile(tree, str(path), "exec"), scope)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metrics(a, b):
    delta = np.abs(a - b)
    return dict(
        bit_identical=a.dtype == b.dtype and a.tobytes() == b.tobytes(),
        max_abs_rgb=float(delta.max(initial=0)),
        changed_components=int(np.count_nonzero(delta)),
        components=int(delta.size),
        changed_u8_components=int(np.count_nonzero(np.floor(a + 0.5) != np.floor(b + 0.5))),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True, help="Isolated app data outside any Git worktree")
    args = parser.parse_args()
    assert sys.flags.isolated and sys.dont_write_bytecode
    assert os.getpriority(os.PRIO_PROCESS, 0) == 15
    root, out = args.research.resolve(), args.out.resolve()
    assert out.is_relative_to(root / "research/studio_unitcal_r1/local")
    out.mkdir(parents=True, exist_ok=True)
    research = root / "research"
    workspace = research / "fit_v4_r2/local/adapter/local_batch"
    freeze = json.loads((workspace / "freeze.json").read_text())
    frozen_python = [row for row in freeze["files"] if row["path"].endswith(".py")]
    for row in frozen_python:
        assert sha(root / row["path"]) == row["sha256"], "Frozen source drift: " + row["path"]
    c040 = load("unitcal_reference_c040", research / "module_adapt/c040.py")
    v2 = load("unitcal_reference_v2", research / "fit_v2_g174_r1/v2.py")

    def source(rel):
        frozen = research / "recipe_redo_r1/local/source" / rel
        return frozen if frozen.is_file() else root / rel

    def read(path):
        return json.loads(Path(path).read_text())

    original = source("firmware/package_v39_g154_looks18_test/local/SD_ROOT/GXRCE.DAT")
    dat = datcodec.parse(original.read_bytes())

    class ReferenceModel(Model):
        def predict(self, b, x, channel_mode=None, quantize=False, half=0, bw_chroma="conditional"):
            return v2.predict(self, knobs, b, x, "v2", v2.factory_rows(dat.entry_of(0, 4)), channel_mode, quantize, half, bw_chroma)

    runtime = source("research/recipe_redo_r1/runtime.py")
    scope = dict(
        np=np,
        fs=colour,
        _models={},
        Model=ReferenceModel,
        geometry_mode=c040.geometry_mode,
        module_spec=c040.module_spec,
        source_copy=source,
        read=read,
        parse=datcodec.parse,
    )
    functions(runtime, ["camera_model", "release_dat"], scope)
    reference_input = dict(np=np)
    functions(research / "bluecyan_r1/physical_calibration.py", ["apply"], reference_input)
    inputs = np.vstack(
        [
            np.stack(
                np.meshgrid(
                    [0.0, 1.0, 127.0, 512.0, 2048.0, 4095.0],
                    [0.0, 1.0, 127.0, 512.0, 2048.0, 4095.0],
                    [0.0, 1.0, 127.0, 512.0, 2048.0, 4095.0],
                    indexing="ij",
                ),
                axis=-1,
            ).reshape(-1, 3),
            np.repeat(np.linspace(0, 4095, 257)[:, None], 3, axis=1),
            np.random.default_rng(20261109).uniform(0, 4095, (2048, 3)),
        ]
    )
    np.save(out / "fixed-input.npy", inputs, allow_pickle=False)
    home = args.home.resolve()
    store.import_from_dat(original.read_bytes(), home)
    sources = calib.dev_sources(root)
    assert len(sources) == 3
    calib.import_calibration(home, sources, origin={"kind": "local-reference-replay"})
    result = dict(
        label="电脑预测",
        source_sha256={
            str(p.relative_to(root)): sha(p)
            for p in (
                runtime,
                research / "module_adapt/c040.py",
                research / "fit_v2_g174_r1/v2.py",
                research / "bluecyan_r1/physical_calibration.py",
                original,
            )
        },
        fixed_inputs=len(inputs),
        frozen_python_sources_verified=len(frozen_python),
        script_sha256=sha(Path(__file__)),
        units={},
    )
    profiles = ("L10__a12-50", "L13__a12-28", "L16__mount-a12")
    for index, unit in enumerate(("a12-50", "a12-28", "mount-a12")):
        camera_unit = store.UNITS[index]
        mapping_path = workspace / f"calibration/input-map-{unit}.json"
        coefficients = np.asarray(read(mapping_path)["coefficients"])
        materialized = research / "fit_v4_r2/local/materialized" / profiles[index]
        profile = read(materialized / "profile.json")
        entries = [dat.entry_of(index, slot) for slot in range(1, 9)]
        entries.append((materialized / "materialized.record552").read_bytes())
        mapped = reference_input["apply"](inputs, coefficients)
        norm = inputs / 4095
        basis = np.c_[np.ones(len(norm)), norm]
        if len(coefficients) == 10:
            r, g, b = norm.T
            basis = np.c_[basis, r * r, g * g, b * b, r * g, r * b, g * b]
        reductions = dict(
            matmul=basis @ coefficients,
            einsum=np.einsum("ni,ij->nj", basis, coefficients),
            sum=(basis[:, :, None] * coefficients[None, :, :]).sum(axis=1),
            inner=np.inner(basis, coefficients.T),
        )
        sequential = np.zeros((len(inputs), 3))
        for j in range(len(coefficients)):
            sequential += basis[:, j, None] * coefficients[j]
        reductions["sequential"] = sequential
        alternates = {k: inputs + 4095 * v for k, v in reductions.items()}
        prediction_rows, bounds, old_diffs = [], [], []
        for iso in (200, 1600):
            actual = calib.model_for(home, iso, unit=camera_unit)[0]
            ref = scope["camera_model"](index, iso=iso, geometry="native")
            for key in ("stock", "d0", "d1", "S_eff"):
                a, b = getattr(actual.engine.ctx, key), getattr(ref.engine.ctx, key)
                assert a.dtype == b.dtype and a.tobytes() == b.tobytes(), (unit, iso, key)
            assert np.array_equal(actual.engine.table.nodes, ref.engine.table.nodes)
            core = copy.copy(actual)
            core.input_coefficients = None
            old = None
            if index == 0:
                cp = source(f"research/color_fit_v2/06_classic_neg/round4/local/cache/data_{iso}.npz")
                ring = source("research/hue_ring/local/" + ("ring_model_v2.json" if iso == 200 else "ring_model_v2_iso1600.json"))
                with np.load(cp, allow_pickle=False) as z:
                    old = Model(z["stock"], z["d0"], z["d1"], read(ring))
            for ei, entry in enumerate(entries):
                for half in (0, 1):
                    expected = ref.predict(entry, mapped, half=half)
                    got = actual.predict(entry, inputs, half=half)
                    row = dict(iso=iso, entry=ei + 1, half=half, **metrics(got, expected))
                    prediction_rows.append(row)
                    bound = max(float(np.max(np.abs(core.predict(entry, x, half=half) - expected))) for x in alternates.values())
                    bounds.append(bound)
                    assert row["max_abs_rgb"] <= bound, (unit, row, bound)
                    if old is not None and half == 0:
                        baseline = old.predict(entry, inputs)
                        frozen_core = Model(actual.engine.ctx.stock, actual.engine.ctx.d0, actual.engine.ctx.d1, actual.engine.table.model)
                        old_diffs.append(
                            dict(
                                iso=iso,
                                entry=ei + 1,
                                **metrics(got, baseline),
                                neutral_ramp=metrics(got[216:473], baseline[216:473]),
                                full_cube=metrics(got[:216], baseline[:216]),
                                random_cube=metrics(got[473:], baseline[473:]),
                                modifiers_only=metrics(frozen_core.predict(entry, inputs), baseline),
                                input_only=metrics(old.predict(entry, mapped), baseline),
                                lookup_only=metrics(core.predict(entry, inputs), baseline),
                            )
                        )
        result["units"][unit] = dict(
            input_map_sha256=sha(mapping_path),
            polynomial_terms=len(coefficients),
            c040_geometry=profile.get("c040_geometry"),
            route=profile.get("route"),
            profile_sha256=sha(materialized / "profile.json"),
            source_groups={k: v["sha256"] for k, v in profile["source_groups"].items()},
            calibrated_isos=list(calib.UNIT_ISOS[camera_unit]),
            exposure_unit="a12-50",
            comparisons=prediction_rows,
            all_bit_identical=all(r["bit_identical"] for r in prediction_rows),
            max_abs_rgb=max(r["max_abs_rgb"] for r in prediction_rows),
            numpy_input_reduction_max_code=max(float(np.max(np.abs(x - mapped))) for x in alternates.values()),
            numpy_output_reduction_max_code=max(bounds),
            original_studio_50mm_comparison=old_diffs,
        )
    path = out / "parity.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                u: {
                    k: v
                    for k, v in row.items()
                    if k
                    in (
                        "all_bit_identical",
                        "max_abs_rgb",
                        "numpy_input_reduction_max_code",
                        "numpy_output_reduction_max_code",
                        "polynomial_terms",
                    )
                }
                for u, row in result["units"].items()
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
