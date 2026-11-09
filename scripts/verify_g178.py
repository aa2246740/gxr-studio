"""对 69 份 g178 默认完整记录逐单元复核研究预测，结果仅写指定 local/。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import numpy as np

from gxr_studio.camdata import calib, datcodec, store
from gxr_studio.engine import colour, knobs
from gxr_studio.engine.model import Model
from gxr_studio.render.params import RecipeSel
from gxr_studio.render.recipes import CameraData


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args()
    assert sys.flags.isolated and sys.dont_write_bytecode and os.getpriority(os.PRIO_PROCESS, 0) == 15
    root, out = args.research.resolve(), args.out.resolve()
    assert out.is_relative_to(root / "research/studio_g178_codec_r1/local")
    helper_path = Path(__file__).with_name("verify_unit_calibration.py")
    if not helper_path.is_file() or calib.SCHEMA != "gxr-studio/calib/2":
        parser.error("逐单元颜色复核需要 gxr-studio/calib/2 和 verify_unit_calibration.py，请完整同步主分支。")
    out.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("unitcal_helpers", helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    research = root / "research"
    workspace = research / "fit_v4_r2/local/adapter/local_batch"
    freeze = json.loads((workspace / "freeze.json").read_text())
    for row in freeze["files"]:
        if row["path"].endswith(".py"):
            assert helper.sha(root / row["path"]) == row["sha256"], row["path"]
    c040 = helper.load("g178_reference_c040", research / "module_adapt/c040.py")
    v2 = helper.load("g178_reference_v2", research / "fit_v2_g174_r1/v2.py")

    def source(rel):
        frozen = research / "recipe_redo_r1/local/source" / rel
        return frozen if frozen.is_file() else root / rel

    def read(path):
        return json.loads(Path(path).read_text())

    original = source("firmware/package_v39_g154_looks18_test/local/SD_ROOT/GXRCE.DAT")
    factory = datcodec.parse(original.read_bytes())

    class ReferenceModel(Model):
        def predict(self, b, x, channel_mode=None, quantize=False, half=0, bw_chroma="conditional"):
            return v2.predict(self, knobs, b, x, "v2", v2.factory_rows(factory.entry_of(0, 4)), channel_mode, quantize, half, bw_chroma)

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
    helper.functions(runtime, ["camera_model", "release_dat"], scope)
    mapping = dict(np=np)
    helper.functions(research / "bluecyan_r1/physical_calibration.py", ["apply"], mapping)
    values = [0.0, 1.0, 127.0, 512.0, 2048.0, 4095.0]
    inputs = np.vstack(
        [
            np.stack(np.meshgrid(values, values, values, indexing="ij"), axis=-1).reshape(-1, 3),
            np.repeat(np.linspace(0, 4095, 257)[:, None], 3, axis=1),
            np.random.default_rng(20261109).uniform(0, 4095, (2048, 3)),
        ]
    )
    package = research / "build_g178_r1/local/model"
    raw, library = (package / "GXRCE.DAT").read_bytes(), (package / "GXRFX.BIN").read_bytes()
    assert helper.sha(package / "GXRCE.DAT") == "2cb4d5817c9bcad91194f9c2d02b6c135a83524bfcfa5cdc349afb81bb91fae6"
    home = args.home.resolve()
    store.import_from_dat(raw, home, library=library)
    calib.import_calibration(home, calib.dev_sources(root))
    camera = CameraData(home)
    result = dict(
        status="RUNNING",
        dat_sha256=helper.sha(package / "GXRCE.DAT"),
        library_sha256=helper.sha(package / "GXRFX.BIN"),
        fixed_inputs=len(inputs),
        script_sha256=helper.sha(Path(__file__)),
        source_sha256={
            str(p.relative_to(root)): helper.sha(p) for p in (runtime, research / "module_adapt/c040.py", research / "fit_v2_g174_r1/v2.py")
        },
        units={},
    )
    try:
        for index, name in enumerate(("a12-50", "a12-28", "mount-a12")):
            unit = store.UNITS[index]
            coefficients = np.asarray(read(workspace / f"calibration/input-map-{name}.json")["coefficients"])
            mapped = mapping["apply"](inputs, coefficients)
            rows = []
            result["units"][unit] = rows
            for iso in (200, 1600):
                actual = calib.model_for(home, iso, unit=unit)[0]
                reference = scope["camera_model"](index, iso=iso, geometry="native")
                for position, info in enumerate(camera.listing()[index]["recipes"], 1):
                    selection = RecipeSel(unit, info["slot"], kind=info.get("kind", "builtin"), id=info.get("id"))
                    entry = camera.resolve(selection).entry
                    for half in (0, 1):
                        got = actual.predict(entry, inputs, half=half)
                        expected = reference.predict(entry, mapped, half=half)
                        fact = dict(position=position, iso=iso, half=half, **helper.metrics(got, expected))
                        rows.append(fact)
                        assert fact["bit_identical"], (unit, fact)
            print(unit, "23 records, 92 prediction vectors, byte-identical", flush=True)
        result["status"] = "PASS"
    except BaseException as exc:
        result.update(status="FAIL", error=str(exc))
        raise
    finally:
        (out / "prediction.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
