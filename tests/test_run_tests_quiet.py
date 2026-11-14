"""scripts/run_tests_quiet.py 的进程内部分（安静门、JUnit 计数）、tests/support/speed.py，以及「速度预算必须标 exclusive」的守卫。

和 tests/test_run_tests.py 分开放，是因为那个文件加上这些会超过 scripts/large_files.py 的 800 行上限。
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_tests.py"


def _quiet_module() -> object:

    sys.path.insert(0, str(RUNNER.parent))
    spec = importlib.util.spec_from_file_location("gxr_run_tests_quiet_mod", RUNNER.parent / "run_tests_quiet.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_quiet_gate_counts_checks_and_shares_one_budget(capsys: pytest.CaptureFixture[str]) -> None:
    """假时钟、假负载：安静前问了几次、预算用完后每次只问一次、loadgate 出错改用回退、load1 回退的门槛。"""
    q: Any = _quiet_module()
    now = [0.0]
    loads = iter([9.0, 8.0, 3.0])
    fallback = q.loadavg_probe(lambda: (next(loads), 0.0, 0.0), ncpu=10)

    def nap(seconds: float) -> bool:
        now[0] += seconds
        return True

    gate = q.QuietGate(None, fallback, budget=100.0, poll=10.0)
    assert gate.wait("phase", nap, clock=lambda: now[0]) is True
    out = capsys.readouterr().out
    assert "等机器安静（phase）：等了 20 秒，问了 3 次，安静了，公开回退 load1=3.00，要 < 5" in out
    assert gate.remaining == 80.0
    busy = q.QuietGate(lambda: (False, "loadgate busy"), fallback, budget=25.0, poll=10.0)
    busy.wait("a", nap, clock=lambda: now[0])
    busy.wait("b", nap, clock=lambda: now[0])
    out = capsys.readouterr().out
    assert "机器一直没安静下来（a）：等了 20 秒，问了 3 次，最后一次 loadgate busy" in out
    assert "机器一直没安静下来（b）：等了 0 秒，问了 1 次" in out and busy.remaining == 5.0  # 剩下不够再等一轮

    def broken() -> tuple[bool, str]:
        raise OSError("iostat missing")

    fixed = q.QuietGate(broken, lambda: (True, "fallback ok"), budget=5.0, poll=1.0)
    assert fixed.wait("c", nap, clock=lambda: now[0]) is True and fixed.primary is None
    assert "fallback ok" in capsys.readouterr().out
    stopped = q.QuietGate(None, lambda: (False, "busy"), budget=100.0, poll=1.0)
    assert stopped.wait("d", lambda _s: False, clock=lambda: now[0]) is False


def test_speed_budgets_report_functional_failures_first() -> None:
    from support.speed import SpeedBudgetExceeded, assert_fast, speed_budgets

    assert getattr(SpeedBudgetExceeded, "gxr_speed_budget", False) is True and issubclass(SpeedBudgetExceeded, AssertionError)
    assert_fast(0.5, 1.0, "fast enough")
    with pytest.raises(SpeedBudgetExceeded, match="要 < 1000"):
        assert_fast(7194, 1000, "visibleMs")
    with pytest.raises(SpeedBudgetExceeded, match="没有在等待窗口内出现"), speed_budgets() as speed:
        speed.below(None, 1000, "visibleMs")
    with pytest.raises(SpeedBudgetExceeded):
        assert_fast(float("nan"), 1000, "nan never passes")
    with pytest.raises(AssertionError) as caught, speed_budgets() as speed:
        speed.at_least(0.4, 1.5, "直方图每秒次数")
        raise AssertionError("box did not move")
    assert type(caught.value) is AssertionError and "直方图每秒次数" in "".join(caught.value.__notes__)
    with speed_budgets() as speed:
        speed.at_most(100, 100, "median")
        speed.at_least(2, 2, "rate")


def test_drop_junit_cases_keeps_the_suite_totals_consistent(tmp_path: Path) -> None:
    q: Any = _quiet_module()
    path = tmp_path / "junit.xml"
    path.write_text(
        '<testsuites><testsuite name="pytest" tests="3" failures="1" errors="0" skipped="0" time="1">'
        '<testcase classname="tests.test_viewer_browser" name="test_slow[a b]"><failure message="x"/></testcase>'
        '<testcase classname="tests.test_viewer_browser" name="test_other"/>'
        '<testcase classname="tests.test_lut.TestCase" name="test_slow[a b]"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    assert q.drop_junit_cases(path, ["tests/test_viewer_browser.py::test_slow[a b]"]) == 1
    suite = ET.parse(path).getroot().find("testsuite")
    assert suite is not None and suite.attrib["tests"] == "2" and suite.attrib["failures"] == "0"
    assert [case.attrib["name"] for case in suite.iter("testcase")] == ["test_other", "test_slow[a b]"]


def _uses_speed(tree: ast.Module) -> set[str]:
    """模块里直接或经同模块的函数用到速度预算的函数名。"""
    funcs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    calls = {name: {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} for name, node in funcs.items()}
    found = {name for name, names in calls.items() if names & {"assert_fast", "speed_budgets", "SpeedBudgets"}}
    while grown := {name for name, names in calls.items() if name not in found and names & found}:
        found |= grown
    return found


def test_every_speed_budget_runs_in_the_exclusive_phase() -> None:
    """速度预算只在 exclusive 阶段量（安静门和安静重试只在那里）。新加的速度断言忘了标 exclusive 就在这里失败。"""
    unmarked: list[str] = []
    users = 0
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue  # 这里只测助手本身，不量时间
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_") and node.name in _uses_speed(tree):
                users += 1
                if "pytest.mark.exclusive" not in {ast.unparse(d) for d in node.decorator_list}:
                    unmarked.append(f"{path.name}::{node.name}")
    assert users >= 9 and not unmarked, (users, unmarked)
