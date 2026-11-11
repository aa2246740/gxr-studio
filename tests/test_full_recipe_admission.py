"""D65、逐张近零分母、场景、量化完整性和独立替换门的反例。"""

from __future__ import annotations

import dataclasses
import json
import struct

import numpy as np
import pytest

from full_recipe_support import environment_at, sample_group
from gxr_studio.engine import knobs
from gxr_studio.recipes import GateResult, Quality, RecipeError, check_record
from gxr_studio.recipes.admission import ORDERS, PHASES, _exact, _primary, assess_groups, phase_output, quantization_keys, required_checks
from gxr_studio.recipes.fixtures import guard
from gxr_studio.recipes.history import verify_historical
from gxr_studio.recipes.types import canonical


def photos():
    return [{"scene": "sky"}, {"scene": "skin"}]


def test_none_denominator_uses_absolute_photo_median_and_p95(settings):
    environment = environment_at(settings.home_dir)
    group = sample_group(environment, 8, count=32)
    same = group.base_new
    result = assess_groups("jlaunch", {"photo_iso200": group}, photos(), {"photo_iso200": same}, {"photo_iso200": same})
    assert result["passes"] and result["groups"]["photo_iso200"]["candidate"]["similarity_percent"] is None
    assert result["groups"]["photo_iso200"]["per_photo"][0]["candidate_similarity"] is None
    worse = np.clip(same + 5, 0, 255)
    result = assess_groups("jlaunch", {"photo_iso200": group}, photos(), {"photo_iso200": same}, {"photo_iso200": worse})
    assert "photo_iso200/None_absolute" in result["failures"] and not result["passes"]


def test_primary_strict_original_and_non_strict_replacement(settings):
    environment = environment_at(settings.home_dir)
    group = sample_group(environment, 9)
    assert not _primary(group, group.want, group.want, strict=True)
    assert _primary(group, group.want, group.want, strict=False)
    good, bad = group.want, np.clip(group.want + 4, 0, 255)
    assert _primary(group, bad, good, strict=True)
    assert not _primary(group, good, bad, strict=False)


def test_scenes_other_iso_and_mount_group_have_real_numeric_failures(settings):
    environment = environment_at(settings.home_dir)
    group = sample_group(environment, 4)
    want = np.clip(group.base_new + 10, 0, 255)
    group = dataclasses.replace(group, want=want)
    before = group.base_new
    after = np.clip(before - 10, 0, 255)
    for unit, group_name, failure in (
        ("jlaunch", "photo_iso200", "photo_iso200/scene/sky"),
        ("qlaunch", "photo_other_iso", "photo_other_iso/overall_median_p95"),
        ("l08firm", "photo_other_iso", "photo_other_iso/0.1pp"),
    ):
        result = assess_groups(unit, {group_name: group}, photos(), {group_name: before}, {group_name: after})
        assert not result["passes"] and failure in result["failures"]


def test_all_128_unique_branches_and_partial_proofs_cannot_pass():
    keys = quantization_keys("original")
    assert len(keys) == len(set(keys)) == 128
    assert len(ORDERS) == 2 and len(PHASES) == 64
    checks = tuple(GateResult(key, "pass") for key in keys)
    assert Quality(keys, checks).state == "passed"
    assert Quality(keys, checks[:-1]).state == "needs_evidence"
    assert Quality(keys, checks, partial=True).state == "needs_evidence"
    with pytest.raises(RecipeError, match="重复"):
        Quality(keys, (checks[0],) * 128)
    assert set(quantization_keys("replacement")) <= set(required_checks(("original", "replacement")))
    rgb = np.array([[0.5, 128.5, 254.5], [23.123, 70.456, 180.789]])
    for order in ORDERS:
        for phase in PHASES:
            values = phase_output(rgb, phase, order)
            assert np.array_equal(values, np.rint(values)) and values.shape == rgb.shape


def test_exact_absence_duplicate_and_nonconvergence_are_not_success():
    assert _exact({})[0] == "missing"
    from gxr_studio.recipes.fit import default_plan
    from gxr_studio.recipes.types import digest

    vector = np.zeros(2)
    start = {
        "id": "exact/axis/seed",
        "phase": "exact",
        "vector": vector.tolist(),
        "vector_sha256": digest(vector.tobytes()),
        "settings": default_plan()["settings"],
    }
    complete = {"starts": [start], "attempts": [{"id": start["id"], "success": True}]}
    assert _exact(complete)[0] == "pass"
    complete["attempts"][0]["success"] = False
    assert _exact(complete)[0] == "fail"
    complete["starts"] = [start, start]
    assert _exact(complete)[0] != "pass"


def test_fixed_integer_gradient_guard_is_executed(settings):
    environment = environment_at(settings.home_dir)
    entry = bytearray(environment.stock)
    entry[20:82] = struct.pack(">31H", *range(1, 32))
    entry[456:518] = entry[20:82]
    knobs.checksum(entry)
    check_record(bytes(entry))
    result = guard(environment.model_for(200)[0], bytes(entry), environment.stock)
    assert result["max_integer_gradient_step_de00"] > 2 and not result["passes"]


def test_new_records_cannot_borrow_historical_exception(settings):
    environment = environment_at(settings.home_dir)
    with pytest.raises(RecipeError, match="固定授权"):
        verify_historical(environment.stock, "jlaunch", 8, "g178_08_a12-50", authorization=b"{}", equivalence=b"{}", registry=b"{}")
    rejected = Quality(("original_gate",), (GateResult("original_gate", "fail", canonical({"admission_basis": "owner"})),))
    assert json.loads(canonical(rejected.summary()))["state"] == "rejected"
