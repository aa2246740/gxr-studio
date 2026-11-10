"""名字按完整记录身份显示，拟合底子仍按内置槽位；真实副本缺失时跳过。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from g178_support import need_bundle
from gxr_studio.camdata import datcodec, store
from gxr_studio.lut.fit import Fitter
from gxr_studio.lut.scene import Samples
from gxr_studio.render.params import RecipeSel
from gxr_studio.render.recipes import CameraData
from test_camdata_api import client as client
from test_camdata_api import reader as reader

ROOT = Path(__file__).resolve().parents[1]
BASE_NAMES = ["GR 正片", "GR 负片", "GR 高对比黑白", "原厂标准", "空位 5", "空位 6", "空位 7", "空位 8"]


def scene(units):
    if shutil.which("node") is None:
        pytest.skip("没有 Node")
    result = subprocess.run(
        ["node", str(ROOT / "tests/support/recipe_names.mjs"), str(ROOT / "web/assets")],
        input=json.dumps({"units": units}),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_records_sharing_bases_keep_their_names_and_positions():
    rows = [{"slot": i, "number": f"{i:02}"} for i in range(1, 5)]
    rows += [
        {"kind": "record", "slot": slot, "id": key * 64, "name": name}
        for slot, key, name in ((6, "a", "Synthetic A"), (6, "b", "Synthetic B"), (3, "c", "Synthetic C"), (3, "d", "Synthetic D"))
    ]
    out = scene([{"id": "jlaunch", "label": "50mm", "recipes": rows}])
    expected = [f"{i:02} {name}" for i, name in enumerate(BASE_NAMES[:4] + ["Synthetic A", "Synthetic B", "Synthetic C", "Synthetic D"], 1)]
    assert [f"{r['number']} {r['name']}" for r in out["camera"][0]] == expected
    assert out["labels"][0] == expected
    assert out["lut"] == ["A12 50mm · 独立完整配方"] * len(expected)
    assert out["bases"][0] == [f"{i:02} {name}" for i, name in enumerate(BASE_NAMES, 1)]


def test_fit_searches_available_builtin_positions_without_using_record_bases():
    rows = [{"slot": slot} for slot in range(1, 5)] + [{"kind": "record", "slot": 6}, {"kind": "record", "slot": 3}]
    seen = set()

    def controls(_unit, slot):
        assert slot in (1, 2, 3, 4), "完整记录的底子槽位不是可选的内置位置"
        return [{"name": "contrast", "default": 0, "levels": [0]}]

    def resolve(selection):
        assert selection.kind == "builtin"
        seen.add(selection.slot)
        return SimpleNamespace(entry=bytes([selection.slot]))

    camera = SimpleNamespace(stock=[b"synthetic"] * 3, listing=lambda: [{"recipes": rows}] * 3, controls=controls, resolve=resolve)
    codes = np.array([[32, 32, 32], [64, 64, 64], [128, 128, 128], [224, 224, 224]], dtype=float)
    samples = Samples(codes, codes, np.ones(4), np.zeros(4, np.int8), np.array([True, False, False, False]), [])
    model = SimpleNamespace(predict=lambda _entry, values: values)
    result = Fitter(camera, model, samples, codes, codes, profile="quick").run()
    assert seen == {1, 2, 3, 4}
    assert [c["slot"] for c in result["candidates"]] == [1, 2]


@pytest.mark.parametrize("generation", (176, 178, 181, 182))
def test_real_bundle_names_positions_and_import_counts(generation, tmp_path, client):
    raw, library = need_bundle(generation)
    dat = datcodec.parse(raw, library)
    home = tmp_path / "camera"
    store.import_from_dat(raw, home, library=library)
    camera = CameraData(home)
    units = camera.listing()
    out = scene(units)
    offset = 8 if generation == 176 else 4
    all_labels = []
    for index, unit in enumerate(units):
        names = BASE_NAMES[:offset] + [r.name for r in dat.recipes if r.unit_mask >> index & 1]
        expected = [f"{i:02} {name}" for i, name in enumerate(names, 1)]
        assert [r["number"] for r in unit["recipes"]] == [f"{i:02}" for i in range(1, len(names) + 1)]
        assert [r["position"] for r in unit["recipes"]] == list(range(1, len(names) + 1))
        assert [f"{r['number']} {r['name']}" for r in out["camera"][index]] == expected
        assert out["labels"][index] == expected
        assert out["bases"][index] == [f"{i:02} {name}" for i, name in enumerate(BASE_NAMES, 1)]
        all_labels.extend(expected)
        codes = np.zeros((4, 3))
        samples = Samples(codes, codes, np.ones(4), np.zeros(4, np.int8), np.array([True, False, False, False]), [])
        fitter = Fitter(camera, None, samples, codes, codes, unit=unit["id"], profile="quick")
        for slot in range(1, offset + 1):
            assert fitter.entry_of(slot, {}) == camera.resolve(RecipeSel(unit["id"], slot)).entry
    assert len(out["lut"]) == len(all_labels)
    assert all(text.endswith(" · 独立完整配方") for text in out["lut"])
    response = client.post("/api/camdata/import-upload", content=raw + library, headers={"X-GXR-Dat-Bytes": str(len(raw))})
    assert response.status_code == 200, response.text
    counts = [23, 22, 23] if generation == 176 else [23, 23, 23]
    for unit, count in zip(units, counts, strict=True):
        assert f"{unit['label']} {count} 条" in response.json()["message"]
