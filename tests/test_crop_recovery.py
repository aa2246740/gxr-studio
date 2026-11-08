"""裁剪形状被拒时，用户明确选 fit 或恢复保存框。重试和其他编辑都不暗改框。"""

import json
import shutil
import subprocess

import pytest

from test_edit_route import SUPPORT, prepare

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")


def run_recovery(tmp_path, choice, code="ratio_mismatch"):
    folder = prepare(tmp_path)
    shutil.copy(SUPPORT / "crop_recovery.mjs", tmp_path / "crop_recovery.mjs")
    res = subprocess.run(["node", str(tmp_path / "crop_recovery.mjs"), str(folder), choice, code], capture_output=True, text=True, timeout=20)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@pytest.mark.parametrize("code", ["ratio_mismatch", "crop_outside_image", "too_small", "bad_rect"])
@pytest.mark.parametrize("choice", ["fit", "restore"])
def test_shape_rejection_offers_explicit_recovery_without_an_automatic_write(tmp_path, code, choice):
    out = run_recovery(tmp_path, choice, code)
    assert out["before"] == {"status": "failed", "writes": 1, "saved": [0.1, 0.1, 0.4, 0.75]}
    assert out["labels"][:2] == ["收进画面", "恢复已保存的框"]
    after = out["after"]
    assert after["status"] == "saved" and after["otherRev"] == 7
    assert len(after["adopted"]) == 1 and after["adopted"][0]["fields"] == ["crop"]
    assert after["adopted"][0]["response"]["rect"] == after["saved"]
    if choice == "fit":
        assert len(after["writes"]) == 1 and after["writes"][0]["body"]["fit"] is True
        assert after["writes"][0]["url"] == "/api/photos/a0/crop"
        assert after["saved"] == [0.2, 0.15, 0.3, 0.5625]
    else:
        assert after["writes"] == [] and after["saved"] == [0.1, 0.1, 0.4, 0.75]


def test_restoring_a_crop_keeps_an_unrelated_failed_edit(tmp_path):
    out = run_recovery(tmp_path, "restore-edit-failure")
    assert out["labels"][:2] == ["收进画面", "恢复已保存的框"]
    assert out["after"]["status"] == "failed"
    assert out["after"]["failedEdit"] == {"adjust": {"exposure_ev": 1}}
    assert out["after"]["writes"] == []


def test_a_failed_restore_read_keeps_the_crop_and_can_be_chosen_again(tmp_path):
    out = run_recovery(tmp_path, "restore-read-failure")
    assert out["labels"][:2] == ["收进画面", "恢复已保存的框"]
    assert out["failedRead"]["status"] == "failed" and out["failedRead"]["adopted"] == 0
    assert out["failedRead"]["crop"]["rect"] == [0.2, 0.15, 0.3, 0.6]
    assert out["after"]["status"] == "saved" and out["after"]["writes"] == []


@pytest.mark.parametrize("choice", ["stale", "stale-pending"])
def test_an_old_recovery_toast_cannot_change_a_newer_crop(tmp_path, choice):
    out = run_recovery(tmp_path, choice)
    assert out["labels"][:2] == ["收进画面", "恢复已保存的框"]
    assert out["after"]["writes"] == [] and out["after"]["adopted"] == []
    assert out["after"]["saved"] == [0.1, 0.1, 0.2, 0.375]


def test_a_failed_explicit_fit_can_be_chosen_again(tmp_path):
    out = run_recovery(tmp_path, "fit-failure")
    assert out["labels"][:2] == ["收进画面", "恢复已保存的框"]
    assert out["failedFit"] == {"status": "failed", "shape": True, "writes": 1}
    assert out["after"]["status"] == "saved" and len(out["after"]["writes"]) == 2
