"""真实样本、研究代码和项目文件夹只有一种找法（support/samples.py）。不需要样本，随时能跑。"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from support import samples

TESTS = Path(__file__).resolve().parent


def test_parent_defaults_to_the_folder_above_the_checkout(monkeypatch):
    monkeypatch.delenv("GXR_STUDIO_TEST_PARENT", raising=False)
    assert samples.sample_parent() == TESTS.parent.parent
    assert samples.captures() == TESTS.parent.parent / "captures"
    assert samples.a12_50_samples() == TESTS.parent.parent / "captures/a12-50/2026-10-19/stage2c-first-run"


def test_parent_follows_the_environment_variable(monkeypatch, tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    monkeypatch.setenv("GXR_STUDIO_TEST_PARENT", str(tmp_path / "link"))
    assert samples.sample_parent() == (tmp_path / "real").resolve()
    assert samples.a12_50_samples() == (tmp_path / "real").resolve() / "captures/a12-50/2026-10-19/stage2c-first-run"


def test_missing_skips_with_the_same_text_by_default(monkeypatch):
    monkeypatch.delenv("GXR_REQUIRE_SAMPLES", raising=False)
    with pytest.raises(pytest.skip.Exception) as caught:
        samples.missing("captures/a12-50 样本不在本机")
    assert caught.value.msg == "captures/a12-50 样本不在本机"


@pytest.mark.parametrize("value", ["0", "", "true"])
def test_only_the_value_1_makes_samples_required(monkeypatch, value):
    monkeypatch.setenv("GXR_REQUIRE_SAMPLES", value)
    with pytest.raises(pytest.skip.Exception):
        samples.missing("x")


def test_missing_fails_under_require_samples(monkeypatch, tmp_path):
    monkeypatch.setenv("GXR_REQUIRE_SAMPLES", "1")
    monkeypatch.setenv("GXR_STUDIO_TEST_PARENT", str(tmp_path))
    with pytest.raises(pytest.fail.Exception) as caught:
        samples.missing("captures/a12-50 样本不在本机")
    assert caught.value.msg.startswith("captures/a12-50 样本不在本机")
    assert str(tmp_path.resolve()) in caught.value.msg


def test_needs_is_the_old_skipif_by_default(monkeypatch):
    monkeypatch.delenv("GXR_REQUIRE_SAMPLES", raising=False)
    absent = samples.needs(False, "研究代码不在本机")
    assert absent.mark.name == "skipif" and absent.mark.args == (True,) and absent.mark.kwargs == {"reason": "研究代码不在本机"}
    present = samples.needs(True, "研究代码不在本机")
    assert present.mark.name == "skipif" and present.mark.args == (False,)


def test_needs_fails_at_setup_under_require_samples(monkeypatch):
    monkeypatch.setenv("GXR_REQUIRE_SAMPLES", "1")
    assert samples.needs(True, "r").mark.args == (False,), "样本在的时候照常跑"
    mark = samples.needs(False, "captures/street-g137-20261031 样本不在本机").mark
    assert mark.name == samples.REQUIRED_MARK
    item = SimpleNamespace(get_closest_marker=lambda name: mark if name == samples.REQUIRED_MARK else None)
    with pytest.raises(pytest.fail.Exception) as caught:
        samples.fail_if_marked(item)  # type: ignore[arg-type]
    assert caught.value.msg.startswith("captures/street-g137-20261031 样本不在本机")
    unmarked = SimpleNamespace(get_closest_marker=lambda name: None)
    samples.fail_if_marked(unmarked)  # type: ignore[arg-type]


def test_no_test_finds_samples_on_its_own():
    """回归：别再按文件位置、`DEFAULT_SAMPLES` 或自己读环境变量找样本（T3 工作树里会悄悄跳过）。"""
    patterns = {
        "自己读 GXR_STUDIO_TEST_PARENT": re.compile(r"environ(?:\.get)?\s*[\(\[]\s*['\"]GXR_STUDIO_TEST_PARENT"),
        "DEFAULT_SAMPLES": re.compile(r"\bDEFAULT_SAMPLES\b"),
        "按文件位置找 captures/research": re.compile(r"parents\[\d\]\s*/\s*['\"](?:captures|research)"),
    }
    # 从文件位置数到仓库上一级：tests/*.py 是 parents[2]，tests/support/*.py 是 parents[3]
    up = r"__file__\)\.resolve\(\)\.parents\[{}\]"
    above = {TESTS: re.compile(up.format(2)), TESTS / "support": re.compile(up.format(3))}
    bad = []
    for path in sorted(TESTS.rglob("*.py")):
        if path.name in {"samples.py", "test_sample_paths.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        found = [what for what, pattern in patterns.items() if pattern.search(text)]
        if path.parent in above and above[path.parent].search(text):
            found.append("按文件位置找上层")
        bad += [f"{path.relative_to(TESTS)}: {what}" for what in found]
    assert bad == []
