"""形状被拒的框不盲重试；恢复提示只属于产生它的那份失败。"""

import json
import shutil
import subprocess

import pytest

from test_edit_route import SUPPORT, prepare, run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")


def notice(tmp_path, choice):
    folder = prepare(tmp_path)
    shutil.copy(SUPPORT / "crop_failure_notices.mjs", tmp_path / "crop_failure_notices.mjs")
    res = subprocess.run(["node", str(tmp_path / "crop_failure_notices.mjs"), str(folder), choice], capture_output=True, text=True, timeout=20)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_status_bar_retry_reopens_choices_without_resubmitting_or_stacking_toasts(tmp_path):
    out = run(tmp_path, "shape-status-retry")
    assert out["before"] == {"writes": 1, "errors": 1}
    assert out["after"] == {"writes": 0, "reads": 0, "errors": 1, "status": "failed"}
    assert out["choices"][:2] == ["收进画面", "恢复已保存的框"]
    assert out["focused"] == "收进画面"


def test_repeated_shape_failures_keep_one_actionable_notice(tmp_path):
    assert notice(tmp_path, "repeat")["beforeErrors"] == 1


@pytest.mark.parametrize("choice", ["fit", "restore", "new-crop"])
def test_resolving_the_crop_closes_every_old_error_notice(tmp_path, choice):
    out = notice(tmp_path, choice)
    assert out["status"] == "saved"
    assert out["resolvedErrors"] == []
    assert out["staleCalls"] == [], "旧提示上的按钮不能重新写入或读取已解决的框"


def test_restoring_the_crop_replaces_its_notice_with_a_working_unrelated_edit_retry(tmp_path):
    out = notice(tmp_path, "restore-edit")
    assert out["restored"] == {"status": "failed", "errors": [["重试", ""]], "edit": {"adjust": {"exposure_ev": 1}}}
    assert out["retryWrites"] == [{"url": "/api/photos/a0/edit", "body": {"adjust": {"exposure_ev": 1}, "expected_rev": 1}}]
    assert out["status"] == "saved" and out["errors"] == []


@pytest.mark.parametrize("choice", ["stale-fit", "stale-restore", "stale-retry"])
def test_an_old_notice_cannot_act_on_a_new_failed_draft(tmp_path, choice):
    out = notice(tmp_path, choice)
    assert out["staleCalls"] == []
    assert out["status"] == "failed" and len(out["errors"]) == 1
