"""Replay private frozen forwards, guards and D65 gates. Write only to this task's local/.

Run under the shared heavy lock with Python -I -B and nice 15. This harness extracts
unchanged reference definitions; production code never loads research Python.
Retained records exercise compatibility, not admission of a newly trained recipe.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from gxr_studio.camdata import calib, datcodec, store
from gxr_studio.camdata.unit_model import input_codes
from gxr_studio.engine import colour, knobs
from gxr_studio.engine import matrix_model as mm
from gxr_studio.engine.model import Model
from gxr_studio.fit_contexts import SampleGroup
from gxr_studio.recipes import RecipeError, RecipeRepository, RenderEnvironment
from gxr_studio.recipes.admission import ORDERS, PHASES, _primary, assess_groups, phase_output
from gxr_studio.recipes.fixtures import guard
from gxr_studio.recipes.history import HISTORICAL_IDS
from gxr_studio.recipes.numerics import integer_objective, predict_mapped
from gxr_studio.render.recipes import CameraData

_spec = importlib.util.spec_from_file_location("full_reference_helpers", Path(__file__).with_name("verify_unit_calibration.py"))
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
functions, load, sha = _helpers.functions, _helpers.load, _helpers.sha


def reference_definitions(path, names, scope):
    tree = ast.parse(path.read_text())
    tree.body = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    assert {node.name for node in tree.body} == set(names)
    exec(compile(tree, str(path), "exec"), scope)


def references(root, frozen):
    c040 = load("full_reference_c040", root / "research/module_adapt/c040.py")
    v2 = load("full_reference_v2", root / "research/fit_v2_g174_r1/v2.py")

    def source(relative):
        copy = root / "research/recipe_redo_r1/local/source" / relative
        return copy if copy.is_file() else root / relative

    def read(path):
        return json.loads(Path(path).read_bytes())

    original = datcodec.parse(source("firmware/package_v39_g154_looks18_test/local/SD_ROOT/GXRCE.DAT").read_bytes())

    class ReferenceModel(Model):
        def predict(self, b, x, channel_mode=None, quantize=False, half=0, bw_chroma="conditional"):
            return v2.predict(self, knobs, b, x, "v2", v2.factory_rows(original.entry_of(0, 4)), channel_mode, quantize, half, bw_chroma)

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
    functions(source("research/recipe_redo_r1/runtime.py"), ["camera_model", "release_dat"], scope)
    functions(frozen / "research/recipe_redo_r1/runtime.py", ["stats"], scope)
    rt = SimpleNamespace(np=np, mm=mm, lab_from_codes=colour.lab_from_codes, delta_e00=colour.delta_e00, stats=scope["stats"])
    assess = dict(np=np, rt=rt, Path=Path, read=read)
    reference_definitions(frozen / "research/model_c040_r1/local/assess.py", ("metric", "Gate"), assess)
    quality = dict(np=np, rt=rt)
    functions(frozen / "research/look_ceiling_r1/quality.py", ["longest_run", "fixtures", "diagnostics"], quality)
    return scope, rt, assess, quality


def reference_predict(model, entry, mapped):
    ordinary = bytearray(entry)
    ordinary[0] = 1
    return model.predict(bytes(ordinary), mapped)


def forward_and_guards(root, table, environments, reference, rt, quality):
    grid = [0.0, 1.0, 127.0, 512.0, 2048.0, 4095.0]
    raw = np.vstack(
        [
            np.stack(np.meshgrid(grid, grid, grid, indexing="ij"), axis=-1).reshape(-1, 3),
            np.repeat(np.linspace(0, 4095, 257)[:, None], 3, axis=1),
            np.random.default_rng(20261110).uniform(0, 4095, (2048, 3)),
        ]
    )
    rows = []
    for record in table["records"]:
        ui, unit = record["unit_index"], store.UNITS[record["unit_index"]]
        entry = (root / record["record_file"]).read_bytes()
        assert sha(root / record["record_file"]) == record["record_sha256"]
        model = environments[unit].model_for(200)[0]
        mapped = input_codes(raw, model.input_coefficients)
        ref = reference["camera_model"](ui, iso=200, geometry="native")

        def predict(value, x):
            return reference_predict(ref, value, x)

        difference = float(abs(predict_mapped(model, entry, mapped) - predict(entry, mapped)).max())
        other = environments[unit].model_for(1600)[0]
        other_mapped = input_codes(raw, other.input_coefficients)
        other_ref = reference["camera_model"](ui, iso=1600, geometry="native")
        difference = max(
            difference, float(abs(predict_mapped(other, entry, other_mapped) - reference_predict(other_ref, entry, other_mapped)).max())
        )
        assert difference < 1e-8, (record["id"], difference)
        stock = environments[unit].stock
        cam = SimpleNamespace(
            mm=mm,
            curve=mm.Curve(np.r_[0, np.frombuffer(stock[20:82], ">u2"), 4095], list(stock[82:115])),
            row=mm.MatrixRow.from_bytes(stock[216:244]),
        )
        gradients, rings = quality["fixtures"](cam)
        helpers = dict(
            np=np,
            rt=rt,
            quality=SimpleNamespace(**quality),
            predict=lambda b, x, _ui, round_output=False: np.rint(predict(b, x)) if round_output else predict(b, x),
        )
        functions(
            root / "research/fit_v4_r2/local/adapter/local_batch/frozen/research/recipe_redo_r1/fit_one.py", ["ring_masks", "guard"], helpers
        )
        old = helpers["guard"](cam, entry, stock, gradients, rings, ui, False)
        new = guard(model, entry, stock)
        assert new["passes"] == old["passes"], record["id"]
        assert new["new_reverse_intervals_by_ring"] == old["new_reverse_intervals_by_ring"], record["id"]
        assert abs(new["max_integer_gradient_step_de00"] - old["max_integer_gradient_step_de00"]) < 1e-10
        rows.append(dict(id=record["id"], forward_max=difference, iso_branches=2, guard_passes=new["passes"]))
    return rows


def primary_parity(group, before, after, strict):
    target = colour.lab_from_codes(group.want)
    old = colour.delta_e00(colour.lab_from_codes(before), target)
    new = colour.delta_e00(colour.lab_from_codes(after), target)
    expected = bool(
        (np.median(new) < np.median(old) - 1e-9 if strict else np.median(new) <= np.median(old) + 1e-10)
        and np.percentile(new, 95) <= np.percentile(old, 95) + 0.25 + 1e-10
    )
    actual = _primary(group, before, after, strict=strict)
    assert actual == expected
    return actual


def integer_parity(model, bank, entry, current, stock, retained, fit):
    values = bank["train"]
    losses = {
        role: integer_objective(model, record, values["x"], values["want"], values["sw"])
        for role, record in (("candidate", entry), ("current", current), ("stock", stock))
    }
    maximum = max(abs(value - retained["integer_training_losses"][role]) for role, value in losses.items())
    assert maximum < 1e-8
    assert abs(losses["candidate"] - fit["objective"]) < 1e-8
    assert abs(losses["current"] - fit["baseline_objective"]) < 1e-8
    assert (losses["candidate"] < min(losses["current"], losses["stock"]) - 1e-9) == retained["integer_training_improves"]
    return maximum


def replay_gates(root, table, environments, assess, frozen):
    phase_scope = dict(np=np)
    functions(frozen / "research/bluecyan_r1/assess.py", ["phase_output"], phase_scope)
    rows = []
    for record in table["records"]:
        if record["position"] != 9:
            continue
        profile = json.loads((root / record["profile"]).read_bytes())
        retained = json.loads((root / record["admission"]["file"]).read_bytes())
        assert sha(root / record["admission"]["file"]) == record["admission"]["sha256"]
        fit = json.loads((root / record["fit_receipt"]["file"]).read_bytes())
        assert sha(root / record["fit_receipt"]["file"]) == record["fit_receipt"]["sha256"]
        assert retained["record_sha256"] == fit["record_sha256"] == record["record_sha256"]
        bank = {}
        for name, path in profile["groups"].items():
            with np.load(path, allow_pickle=False) as data:
                bank[name] = dict(data)
        reconstructed = (root / record["admission"]["file"]).parent / "groups/photo_other_iso.npz"
        if "photo_other_iso" not in bank and reconstructed.is_file():
            with np.load(reconstructed, allow_pickle=False) as data:
                bank["photo_other_iso"] = dict(data)
        gate = assess["Gate"](profile, bank, Path(profile["current_record"]).read_bytes())
        info = json.loads((Path(profile["current_record"]).parent / "photos.json").read_bytes())
        photos = [{**p, "scene": p["scene_group"]} for p in info]
        groups = {
            name: SampleGroup(v["raw_codes"], v["x"], v["xyz"], v["want"], v["base_new"], v["sw"], v["photo_id"], v["iso"])
            for name, v in bank.items()
            if name in gate.rows
        }
        unit = store.UNITS[record["unit_index"]]
        model = environments[unit].model_for(200)[0]
        for group in groups.values():
            group.verify_model(environments[unit])
        entry = (root / record["record_file"]).read_bytes()
        current = Path(profile["current_record"]).read_bytes()
        before = {name: predict_mapped(model, current, g.x) for name, g in groups.items()}
        after = {name: predict_mapped(model, entry, g.x) for name, g in groups.items()}
        archived = {(b["order"], tuple(round(v * 4) for v in b["phase"])): b["passes"] for b in retained["branches"]}
        assert len(archived) == 128 and not retained["partial"]
        integer_max = integer_parity(model, bank, entry, current, environments[unit].stock, retained, fit)
        outcomes = []
        for order, phase in [(None, None), *[(o, p) for o in ORDERS for p in PHASES]]:
            if order is None:
                b, a = ({k: np.rint(v) for k, v in values.items()} for values in (before, after))
            else:
                b, a = ({k: phase_output(v, phase, order) for k, v in values.items()} for values in (before, after))
                for k, v in after.items():
                    assert np.array_equal(a[k], phase_scope["phase_output"](v, np.array(phase) / 4, order, mm))
            expected, actual = gate.assess(b, a, True), assess_groups(unit, groups, photos, b, a)
            assert actual["passes"] == expected["passes"] and actual["failures"] == expected["failures"], record["id"]
            primary = primary_parity(groups["photo_iso200"], b["photo_iso200"], a["photo_iso200"], strict=order is None)
            if order is None:
                assert primary == retained["primary_overall_gate"]
            else:
                assert (actual["passes"] and primary) == archived[(order, phase)]
            for name, group in actual["groups"].items():
                for role in ("current", "candidate"):
                    for metric in ("median", "p95", "similarity_percent"):
                        assert group[role][metric] == expected["groups"][name][role][metric]
            outcomes.append(actual["passes"] and primary)
        assert outcomes[0] == retained["nominal"]["passes"]
        rows.append(
            dict(
                id=record["id"],
                groups={name: len(g.x) for name, g in groups.items()},
                nominal=outcomes[0],
                unique_branches=128,
                quant_passes=sum(outcomes[1:]),
                retained_quant_passes=retained["quantization_passes"],
                integer_loss_max_difference=integer_max,
            )
        )
    return rows


def historical_imports(root, table, environments, home):
    base = root / "research/fit_v4_r2/local"
    evidence = dict(
        authorization=(base / "owner_materialization_authorization.json").read_bytes(),
        equivalence=(base / "equivalence.json").read_bytes(),
        registry=(base / "registry/all_full_registry.json").read_bytes(),
    )
    repo, imported = RecipeRepository(home), []
    for row in table["records"]:
        if row["id"] not in HISTORICAL_IDS:
            continue
        entry = (root / row["record_file"]).read_bytes()
        environment = environments[store.UNITS[row["unit_index"]]]
        saved = repo.import_historical(entry, environment, row["position"], row["id"], name=row["id"], **evidence)
        assert saved["quality"]["state"] == "historical_exception"
        for key in ("admission_basis", "original_admission_unpassed", "acceptance_note"):
            assert saved["quality"][key] == row[key]
        try:
            repo.import_historical(entry, environment, row["position"] + 1, row["id"], name="wrong identity", **evidence)
        except RecipeError as exc:
            assert exc.code == "historical_identity_mismatch"
        else:
            raise AssertionError("wrong historical position accepted")
        imported.append(row["id"])
    assert len(imported) == 7
    return imported


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args()
    assert sys.flags.isolated and sys.dont_write_bytecode and os.getpriority(os.PRIO_PROCESS, 0) == 15
    root, out, home = args.research.resolve(), args.out.resolve(), args.home.resolve()
    assert out.is_relative_to(root / "research/studio_full_recipe_r1/local")
    assert not out.exists(), "preserve first failure; choose a new output"
    out.mkdir(parents=True)
    workspace = root / "research/fit_v4_r2/local/adapter/local_batch"
    freeze = json.loads((workspace / "freeze.json").read_bytes())
    for row in freeze["files"]:
        if row["path"].endswith(".py"):
            assert sha(root / row["path"]) == row["sha256"], row["path"]
    frozen = workspace / "frozen"
    reference, rt, assess, quality = references(root, frozen)
    package = root / "research/build_g178_r1/local/model"
    store.import_from_dat((package / "GXRCE.DAT").read_bytes(), home, library=(package / "GXRFX.BIN").read_bytes())
    calib.import_calibration(home, calib.dev_sources(root))
    camera = CameraData(home)
    environments = {unit: RenderEnvironment.capture(home, camera, unit) for unit in store.UNITS}
    table = json.loads((root / "research/fit_v4_r2/local/registry/all_full_registry.json").read_bytes())
    result = dict(status="RUNNING", new_private_fit=False, script_sha256=sha(Path(__file__)))
    try:
        result["records"] = forward_and_guards(root, table, environments, reference, rt, quality)
        result["gates"] = replay_gates(root, table, environments, assess, frozen)
        result["historical"] = historical_imports(root, table, environments, home)
        result["status"] = "PASS"
    except BaseException as exc:
        result.update(status="FAIL", error=repr(exc))
        raise
    finally:
        (out / "replay.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(dict(status=result["status"], records=len(result["records"]), gate_profiles=len(result["gates"]), historical=7)))


if __name__ == "__main__":
    main()
