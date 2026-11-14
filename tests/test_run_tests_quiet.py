"""scripts/run_tests_quiet.py 的进程内部分（安静门、JUnit 计数）、tests/support/speed.py，以及「速度预算必须标 exclusive」的守卫。

和 tests/test_run_tests.py 分开放，是因为那个文件加上这些会超过 scripts/large_files.py 的 800 行上限。
"""

from __future__ import annotations

import ast
import importlib.util
import os
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
    busy = q.QuietGate(lambda _limit: (False, "loadgate busy"), fallback, budget=25.0, poll=10.0)
    busy.wait("a", nap, clock=lambda: now[0])
    busy.wait("b", nap, clock=lambda: now[0])
    out = capsys.readouterr().out
    assert "机器一直没安静下来（a）：等了 20 秒，问了 3 次，最后一次 loadgate busy" in out
    assert "机器一直没安静下来（b）：等了 0 秒，问了 1 次" in out and busy.remaining == 5.0  # 剩下不够再等一轮

    def broken(_limit: float) -> tuple[bool, str]:
        raise OSError("iostat missing")

    fixed = q.QuietGate(broken, lambda _limit: (True, "fallback ok"), budget=5.0, poll=1.0)
    assert fixed.wait("c", nap, clock=lambda: now[0]) is True and fixed.primary is None
    assert "fallback ok" in capsys.readouterr().out
    stopped = q.QuietGate(None, lambda _limit: (False, "busy"), budget=100.0, poll=1.0)
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
    original = path.read_text(encoding="utf-8")
    assert q.drop_junit_cases(path, ["tests/test_viewer_browser.py::test_slow[a b]"], {}) == 1
    suite = ET.parse(path).getroot().find("testsuite")
    assert suite is not None and suite.attrib["tests"] == "2" and suite.attrib["failures"] == "0"
    assert [case.attrib["name"] for case in suite.iter("testcase")] == ["test_other", "test_slow[a b]"]
    # 子进程记下的贡献优先：一个元素装着 subtest 和主 call 两个失败报告时扣 2（Sol r1 P2）
    path.write_text(original.replace('tests="3" failures="1"', 'tests="4" failures="2"'), encoding="utf-8")
    counts = {"tests/test_viewer_browser.py::test_slow[a b]": {"tests": 2, "failures": 2, "errors": 0, "skipped": 0}}
    assert q.drop_junit_cases(path, ["tests/test_viewer_browser.py::test_slow[a b]"], counts) == 1
    suite = ET.parse(path).getroot().find("testsuite")
    assert suite is not None and (suite.attrib["tests"], suite.attrib["failures"]) == ("2", "0")


SPEED_HELPERS = {"assert_fast", "speed_budgets", "SpeedBudgets"}
TIMERS = {"perf_counter", "perf_counter_ns", "monotonic", "monotonic_ns", "time", "time_ns", "process_time", "process_time_ns"}
Func = ast.FunctionDef | ast.AsyncFunctionDef


def _aliases(tree: ast.Module) -> tuple[set[str], set[str], set[str]]:
    """速度助手的名字（含 as 别名）、time 模块的名字、从 time 直接导入的计时函数名。"""
    helpers, time_modules, timers = set(SPEED_HELPERS), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                if node.module.split(".")[-1] == "speed" and alias.name in SPEED_HELPERS:
                    helpers.add(alias.asname or alias.name)
                if node.module == "time" and alias.name in TIMERS:
                    timers.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            time_modules |= {alias.asname or alias.name for alias in node.names if alias.name == "time"}
    return helpers, time_modules, timers


def _is_timer(node: ast.AST, time_modules: set[str], timers: set[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.attr in TIMERS and func.value.id in time_modules
    return isinstance(func, ast.Name) and func.id in timers


def _bare_timing(func: Func, time_modules: set[str], timers: set[str]) -> list[int]:
    """assert 里直接比较耗时的行号：两次计时相减（或由它算出来的变量）。速度断言要走 assert_fast。

    只认「计时 − 计时」，所以 `before.st_ctime_ns - time.time_ns()` 这种拿时钟当参照的检查不算。
    """
    clocked: set[str] = set()  # 由计时读数得来的变量，比如 t0 = time.perf_counter()
    elapsed_names: set[str] = set()  # 由耗时得来的变量，比如 dt = time.time() - t0

    def clockish(expr: ast.AST) -> bool:
        return any(_is_timer(n, time_modules, timers) or (isinstance(n, ast.Name) and n.id in clocked) for n in ast.walk(expr))

    def elapsed(expr: ast.AST) -> bool:
        subs = [n for n in ast.walk(expr) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub)]
        return any(clockish(n.left) and clockish(n.right) for n in subs) or any(
            isinstance(n, ast.Name) and n.id in elapsed_names for n in ast.walk(expr)
        )

    assigns = [n for n in ast.walk(func) if isinstance(n, ast.Assign | ast.AnnAssign) and n.value is not None]
    for node in sorted(assigns, key=lambda n: n.lineno):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = {n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)}
        assert node.value is not None
        if elapsed(node.value):
            elapsed_names |= names
        if clockish(node.value):
            clocked |= names
    return sorted(node.lineno for node in ast.walk(func) if isinstance(node, ast.Assert) and elapsed(node.test))


def _marked(decorators: list[ast.expr]) -> bool:
    return any("mark.exclusive" in ast.unparse(d) for d in decorators)


def _pytestmark(body: list[ast.stmt]) -> bool:
    return any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets)
        and "mark.exclusive" in ast.unparse(node.value)
        for node in body
    )


def speed_guard_problems(source: str, filename: str = "<src>") -> tuple[int, list[str]]:
    """返回（用到速度预算的测试个数，问题列表）。

    问题有两种：用到速度助手（含 as 别名、经同模块的函数或类方法）的测试没标 exclusive；
    assert 里直接比较计时（time.perf_counter() - t0 < 3 这类）——这种要改成 assert_fast。
    收集范围和 pytest 一样：模块里的 test_* 函数（含 async）、Test* 类里的 test_* 方法。
    """
    tree = ast.parse(source)
    helpers, time_modules, timers = _aliases(tree)
    funcs: list[tuple[str, Func, bool]] = []  # (名字, 节点, 外层类或模块已经标了 exclusive)
    module_marked = _pytestmark(tree.body)
    for node in tree.body:
        if isinstance(node, Func):
            funcs.append((node.name, node, module_marked))
        elif isinstance(node, ast.ClassDef):
            class_marked = module_marked or _marked(node.decorator_list) or _pytestmark(node.body)
            funcs += [(f"{node.name}.{m.name}", m, class_marked) for m in node.body if isinstance(m, Func)]
    refs = {
        name: {n.id for n in ast.walk(f) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(f) if isinstance(n, ast.Attribute)}
        for name, f, _marked_outer in funcs
    }
    found = {name for name, names in refs.items() if names & helpers}
    short = lambda names: {name.rsplit(".", 1)[-1] for name in names}  # noqa: E731
    while grown := {name for name, names in refs.items() if name not in found and names & short(found)}:
        found |= grown
    users, problems = 0, []
    for name, func, outer in funcs:
        problems += [f"{filename}:{line} {name}：裸计时断言，改用 assert_fast" for line in _bare_timing(func, time_modules, timers)]
        owner = name.split(".")[0]
        collected = func.name.startswith("test_") and ("." not in name or owner.startswith("Test"))
        if collected and name in found:
            users += 1
            if not (outer or _marked(func.decorator_list)):
                problems.append(f"{filename} {name}：用到速度预算，没标 exclusive")
    return users, problems


def test_every_speed_budget_runs_in_the_exclusive_phase() -> None:
    """速度预算只在 exclusive 阶段量（安静门和安静重试只在那里）。新加的速度断言忘了标 exclusive、或者写成裸计时，就在这里失败。"""
    users, problems = 0, []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue  # 这里只测助手本身，不量时间
        found, issues = speed_guard_problems(path.read_text(encoding="utf-8"), path.name)
        users += found
        problems += issues
    assert users >= 9 and not problems, (users, problems)


# Sol r1 P3：原来的检查放过的三种写法（不标 exclusive），各自要被抓到；标上以后不再报。
_BARE = "import time\n\ndef test_bare():\n    started = time.perf_counter()\n    assert time.perf_counter() - started < 3.0\n"
_BARE_DERIVED = "from time import time as now\n\ndef test_derived():\n    t = now()\n    dt = now() - t\n    assert dt < 15\n"
_ALIAS = "from support.speed import assert_fast as fast\n\n{mark}def test_alias():\n    fast(2, 1, 'latency')\n"
_METHOD = (
    "from support.speed import assert_fast\n\nclass TestSpeed:\n    {mark}def test_method(self):\n        self._check()\n\n"
    "    def _check(self):\n        assert_fast(2, 1, 'latency')\n"
)
_ASYNC = "import support.speed as sp\n\n{mark}async def test_async():\n    sp.assert_fast(2, 1, 'latency')\n"
_MARK = "@pytest.mark.exclusive\n"


@pytest.mark.parametrize(
    "source", [_BARE, _BARE_DERIVED, _BARE.replace("def test_bare", _MARK + "def test_bare")], ids=["bare", "derived", "bare-even-if-marked"]
)
def test_the_guard_rejects_bare_timing_asserts(source: str) -> None:
    _users, problems = speed_guard_problems(source)
    assert problems and "裸计时断言" in problems[0], problems


@pytest.mark.parametrize("template", [_ALIAS, _METHOD, _ASYNC], ids=["helper-alias", "class-method", "async-module-alias"])
def test_the_guard_follows_aliases_methods_and_async_tests(template: str) -> None:
    clock_reference = "import time\n\ndef test_ctime(stat):\n    assert abs(stat.st_ctime_ns - time.time_ns()) < 10_000_000_000\n"
    assert speed_guard_problems(clock_reference) == (0, [])  # 拿时钟当参照，不是耗时
    indent = "    " if "class TestSpeed" in template else ""
    users, problems = speed_guard_problems(template.format(mark=""))
    assert users == 1 and len(problems) == 1 and "没标 exclusive" in problems[0], problems
    users, problems = speed_guard_problems(template.format(mark=_MARK.replace("\n", "\n" + indent)))
    assert users == 1 and not problems, problems
    class_marked = "import pytest\n\npytestmark = pytest.mark.exclusive\n" + template.format(mark="")
    assert speed_guard_problems(class_marked) == (1, [])


@pytest.mark.parametrize(
    ("budget", "cost", "tests"),
    [(600.0, 5.0, 10), (600.0, 5.1, 10), (5.0, 5.25, 0)],
    ids=["r1-5s-samples", "r2-5.1s-samples", "r2-5s-budget-5.25s-probe"],
)
def test_sampling_time_counts_against_the_whole_wait_budget(capsys: pytest.CaptureFixture[str], budget: float, cost: float, tests: int) -> None:
    """Sol r1/r2 P2：每 10 秒问一次、一直不安静，每次探针调用耗 cost 秒（loadgate 的 5 秒窗口加上 vm_stat、sysctl 的开销），
    阶段前 + tests 个 exclusive 用例 + 重试前。父进程、子进程、重试前共用一份预算。

    r1 时每个用例前还各采一次（655 秒）；r2 时窗口钉在 5 秒但整次调用没有期限（600.1 秒、5.25 秒）。
    现在每次探针调用的期限是剩下的预算，超时就按不安静算（真探针在子进程里，连进程组收掉），合计不超过预算。
    """
    q: Any = _quiet_module()
    now = [0.0]

    def busy(limit: float) -> tuple[bool, str] | None:
        if cost > limit:  # 期限到了：真探针的子进程在这里被杀掉
            now[0] += max(0.0, limit)
            return None
        now[0] += cost
        return False, f"busy at {now[0]:.1f}"

    def nap(seconds: float) -> bool:
        now[0] += seconds
        return True

    parent = q.QuietGate(busy, busy, budget=budget, poll=10.0)
    parent.wait("exclusive 阶段开始前", nap, clock=lambda: now[0])
    child = q.QuietGate(busy, busy, budget=parent.remaining, poll=10.0)  # 父进程把剩下的交给子进程
    for index in range(tests):
        child.wait(f"exclusive 用例之前：case {index}", nap, clock=lambda: now[0])
    used_by_child = parent.remaining - child.remaining  # 子进程写进结果文件，父进程扣掉
    parent.remaining -= used_by_child
    parent.wait("速度预算重试之前", nap, clock=lambda: now[0])
    out = capsys.readouterr().out
    assert now[0] <= budget, (now[0], out)
    assert out.count("不再采样") == tests + 1, out


def test_a_probe_without_seconds_is_stopped_at_the_deadline(tmp_path: Path) -> None:
    """Sol r2 P2：不收 seconds 的 loadgate 函数也有期限。探针挂住 30 秒，期限 1 秒：期限一到就回来，子进程已经收掉。"""
    q: Any = _quiet_module()
    pidfile = tmp_path / "probe.pid"
    gate_file = tmp_path / "slow_gate.py"
    gate_file.write_text(
        f"import os, time\n\ndef quiet_for_timing():\n    open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n    time.sleep(30)\n"
        "    return True, {}\n",
        encoding="utf-8",
    )
    probe = q.gate_probe(q.load_gate(str(gate_file)))
    assert probe(1.0) is None  # 正常跑完会返回 (True, …)；None 只能是期限到了
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # 子进程连进程组已经杀掉、回收
