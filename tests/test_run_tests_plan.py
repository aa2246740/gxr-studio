"""scripts/run_tests_plan.py：按上次 JUnit 的用时分片、改过的文件先跑、失败已成定局就停。

和 tests/test_run_tests.py 分开放，是因为那个文件加上这些会超过 scripts/large_files.py 的 800 行上限。
拉起 runner 的用例沿用那边的假 loadgate 和仓库里的临时小用例。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from test_run_tests import _alive, _base_opts, _rcfile, _runner_env, _stop_pid, _write_tree
from test_run_tests import scratch as scratch  # 夹具

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_tests.py"

pytestmark = pytest.mark.skipif(os.environ.get("GXR_RUN_TESTS_INNER") == "1", reason="inner runner")


def _plan_module() -> Any:
    spec = importlib.util.spec_from_file_location("gxr_run_tests_plan_mod", RUNNER.parent / "run_tests_plan.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _old_count_split(node_ids: list[str], workers: int) -> list[list[str]]:
    """origin/main 上 run_tests._shards 的按用例数分片，原样抄来做对照。"""
    grouped: dict[str, list[str]] = {}
    for node_id in node_ids:
        grouped.setdefault(node_id.split("::", 1)[0], []).append(node_id)
    commands: list[list[str]] = [[] for _ in range(workers)]
    sizes = [0 for _ in range(workers)]
    for path in sorted(grouped):
        slot = sizes.index(min(sizes))
        items = sorted(grouped[path])
        sizes[slot] += len(items)
        commands[slot].extend(items)
    return [cmd for cmd in commands if cmd]


def _junit(path: Path, cases: list[tuple[str, str, float]]) -> Path:
    suite = ET.Element("testsuite", {"name": "pytest", "tests": str(len(cases))})
    for classname, name, seconds in cases:
        ET.SubElement(suite, "testcase", {"classname": classname, "name": name, "time": f"{seconds:.3f}"})
    report = ET.Element("testsuites")
    report.append(suite)
    ET.ElementTree(report).write(path, encoding="utf-8", xml_declaration=True)
    return path


# a 1 个、b 1 个、c 1 个、d 3 个用例。按用例数：[a, c] [b, d]；按用时（a 100 秒，其余各 10 秒）：[a] [b, c, d]。
_IDS = [
    "tests/p/test_a.py::test_one",
    "tests/p/test_b.py::test_one",
    "tests/p/test_c.py::test_one",
    "tests/p/test_d.py::TestK::test_x",
    "tests/p/test_d.py::TestK::test_y",
    "tests/p/test_d.py::test_z[1 2]",
]
_TIMES = [
    ("tests.p.test_a", "test_one", 100.0),
    ("tests.p.test_b", "test_one", 10.0),
    ("tests.p.test_c", "test_one", 10.0),
    ("tests.p.test_d.TestK", "test_x", 4.0),
    ("tests.p.test_d.TestK", "test_y", 4.0),
    ("tests.p.test_d", "test_z[1 2]", 2.0),
]


def test_shards_are_balanced_by_junit_time_longest_first(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan = _plan_module()
    env = {plan.TIMES_ENV: str(_junit(tmp_path / "last.xml", _TIMES))}
    shards = plan.shard_plan(_IDS, 2, ROOT, env)
    assert shards == [_IDS[:1], _IDS[1:]]
    assert shards != _old_count_split(_IDS, 2)
    out = capsys.readouterr().out
    assert "4 个文件有时间，0 个没有" in out
    assert "各分片计划用时（分钟）：shard-0 1.7, shard-1 0.5。" in out


def test_files_missing_from_the_times_weigh_count_times_the_median(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """e 不在数据里：3 个用例 × 中位数（4 和 10 的平均，7 秒）= 21 秒，比 b、c 的 10 秒长，先放。"""
    plan = _plan_module()
    ids = [*_IDS, "tests/p/test_e.py::test_1", "tests/p/test_e.py::test_2", "tests/p/test_e.py::test_3"]
    env = {plan.TIMES_ENV: str(_junit(tmp_path / "last.xml", _TIMES))}
    grouped: dict[str, list[str]] = {}
    for node_id in ids:
        grouped.setdefault(node_id.split("::", 1)[0], []).append(node_id)
    weights = plan.file_weights(grouped, env)
    assert weights == {
        "tests/p/test_a.py": 100.0,
        "tests/p/test_b.py": 10.0,
        "tests/p/test_c.py": 10.0,
        "tests/p/test_d.py": 10.0,
        "tests/p/test_e.py": 21.0,
    }
    assert "4 个文件有时间，1 个没有，按用例数 × 中位数 7.000 秒估计" in capsys.readouterr().out
    # a 100 | e 21, b 10, c 10, d 10
    assert plan.shard_plan(ids, 2, ROOT, env) == [_IDS[:1], [*ids[6:], *_IDS[1:]]]


@pytest.mark.parametrize("case", ["unset", "blank", "missing-file", "not-xml", "no-testcases"])
def test_without_usable_times_the_split_is_todays_count_split(tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str) -> None:
    plan = _plan_module()
    env: dict[str, str] = {}
    if case == "blank":
        env[plan.TIMES_ENV] = "  "
    elif case == "missing-file":
        env[plan.TIMES_ENV] = str(tmp_path / "nope.xml")
    elif case == "not-xml":
        (tmp_path / "bad.xml").write_text("<testsuites><testcase", encoding="utf-8")
        env[plan.TIMES_ENV] = str(tmp_path / "bad.xml")
    elif case == "no-testcases":
        env[plan.TIMES_ENV] = str(_junit(tmp_path / "empty.xml", []))
    for workers in (1, 2, 3, 4, 8):
        assert plan.shard_plan(_IDS, workers, ROOT, env) == _old_count_split(_IDS, workers)
    out = capsys.readouterr().out
    assert "按用例数分片" in out and "计划用时" not in out
    assert "非 exclusive 测试 6 个，分到 2 个进程：2, 4。" in out


@pytest.mark.parametrize(
    "plans",
    [[_IDS[:3], _IDS[2:]], [_IDS[:3], _IDS[4:]], [_IDS, ["tests/p/test_x.py::test_new"]], [[*_IDS[:3], _IDS[0]], _IDS[3:]]],
    ids=["duplicate", "lost", "extra", "duplicate-inside-one"],
)
def test_shard_plans_must_equal_the_input_node_ids(capsys: pytest.CaptureFixture[str], plans: list[list[str]]) -> None:
    plan = _plan_module()
    plan.check_union([_IDS[:3], _IDS[3:]], _IDS)
    with pytest.raises(SystemExit) as stop:
        plan.check_union(plans, _IDS)
    assert stop.value.code == 2
    assert "分片计划和收集到的 node id 对不上" in capsys.readouterr().out


def test_changed_files_go_first_and_the_rest_keep_their_order() -> None:
    plan = _plan_module()
    ids = ["t/test_a.py::x", "t/test_b.py::x", "t/test_a.py::y", "t/test_c.py::x", "t/test_b.py::y"]
    env = {plan.CHANGED_ENV: os.pathsep.join(["t/test_b.py", str(ROOT / "t" / "test_c.py"), "", "t/not_planned.py"])}
    assert plan.changed_first(ids, ROOT, env) == ["t/test_b.py::x", "t/test_c.py::x", "t/test_b.py::y", "t/test_a.py::x", "t/test_a.py::y"]
    assert plan.changed_first(ids, ROOT, {}) == ids
    shards = plan.shard_plan(_IDS, 2, ROOT, {plan.CHANGED_ENV: "tests/p/test_d.py"})
    assert shards == [[_IDS[0], _IDS[2]], [*_IDS[3:], _IDS[1]]]


# ---- 拉起真 runner --------------------------------------------------------------------------------------

# 每个用例记一行：名字、所在阶段（分片临时目录的名字）、子进程的命令行里有没有 -x。
_RECORD = (
    "import hits, os, sys\nfrom pathlib import Path\n\n"
    "def _mark(name):\n    hits.hit(f\"{name} {Path(os.environ['TMPDIR']).parent.name} {'-x' in sys.argv}\")\n\n"
)


def _test_file(directory: Path, name: str, body: str, *, mark: str = "") -> None:
    module_mark = f"pytestmark = pytest.mark.{mark}\n\n" if mark else ""
    (directory / f"{name}.py").write_text(_RECORD + "import pytest\n\n" + module_mark + body, encoding="utf-8")


def _hits_module(directory: Path) -> None:
    directory.mkdir(parents=True)
    (directory / "hits.py").write_text(
        "import os\n\ndef hit(name):\n"
        "    with open(os.environ['HITS'], 'a', encoding='utf-8') as handle:\n        handle.write(name + '\\n')\n",
        encoding="utf-8",
    )


def _simple(name: str) -> str:
    return f"def test_{name}():\n    _mark('{name}')\n"


def _go(
    tmp: Path, tree: Path, extra: dict[str, str], *, workers: int = 2, reruns: str = "--force-reruns=0", timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    _rcfile(tmp / "coveragerc")
    opts = [reruns if opt == "--force-reruns=0" else opt for opt in _base_opts(tree, tmp / "junit.xml")]
    env = _runner_env(tmp, opts, workers=workers)
    env.update(extra)
    return subprocess.run([sys.executable, "-I", str(RUNNER)], cwd=ROOT, env=env, text=True, capture_output=True, timeout=timeout, check=False)


def _marks(tmp: Path) -> list[tuple[str, str, str]]:
    path = tmp / "hits.log"
    if not path.is_file():
        return []
    return [tuple(line.split(" ")) for line in path.read_text(encoding="utf-8").splitlines() if " " in line]  # type: ignore[misc]


def test_runner_places_files_by_junit_time(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _write_tree(tree, exclusive=False)
    for name in ("test_a", "test_b", "test_c"):
        _test_file(tree, name, _simple(name[-1]))
    _test_file(tree, "test_d", _simple("d1") + _simple("d2") + _simple("d3"))
    module = str(tree.relative_to(ROOT)).replace("/", ".")
    times = [(f"{module}.test_a", "test_a", 300.0)] + [(f"{module}.test_{n}", f"test_{n}", 30.0) for n in "bc"]
    times += [(f"{module}.test_alpha", "test_normal_alpha", 1.0), (f"{module}.test_beta", "test_normal_beta", 1.0)]
    times += [(f"{module}.test_d", f"test_d{n}", 10.0) for n in "123"]
    junit = _junit(tmp_path / "last.xml", times)
    proc = _go(tmp_path, tree, {"GXR_TEST_TIMES_JUNIT": str(junit)})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    shard = {name: where for name, where, _x in _marks(tmp_path)}
    # 按用例数会是 a、b、c 在 shard-0，d 在 shard-1；按用时：a 300 秒独占一个分片，其余 92 秒在另一个。
    assert shard == {"a": "shard-0", "b": "shard-1", "c": "shard-1", "d1": "shard-1", "d2": "shard-1", "d3": "shard-1"}, out
    assert "各分片计划用时（分钟）：shard-0 5.0, shard-1 1.5。" in out


def _stage_tree(tree: Path) -> None:
    _write_tree(tree, exclusive=False)
    _test_file(tree, "test_normal", _simple("normal"))
    _test_file(tree, "test_web", _simple("web"), mark="browser_serial")
    _test_file(tree, "test_excl", _simple("excl"), mark="exclusive")


@pytest.mark.parametrize("stop", ["1", None])
def test_stop_flag_reaches_shards_and_browser_but_never_exclusive(tmp_path: Path, scratch: Path, stop: str | None) -> None:
    tree = scratch / "tree"
    _stage_tree(tree)
    proc = _go(tmp_path, tree, {"GXR_STOP_ON_FAILURE": stop} if stop else {}, workers=1)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    flags = {name: x for name, _where, x in _marks(tmp_path)}
    on = str(stop == "1")
    assert flags == {"normal": on, "web": on, "excl": "False"}, proc.stdout


def test_a_failure_that_passes_on_its_rerun_does_not_stop_the_run(tmp_path: Path, scratch: Path) -> None:
    """--reruns 1 下 -x 要等重跑：第一次失败、重跑通过的用例不让这次停下，后面的用例和阶段照样跑。"""
    tree = scratch / "tree"
    _stage_tree(tree)
    _test_file(
        tree,
        "test_flaky",
        "def test_flaky():\n    _mark('flaky')\n    assert sum(1 for l in open(os.environ['HITS']) if l.startswith('flaky ')) > 1\n",
    )
    extra = {"GXR_STOP_ON_FAILURE": "1", "GXR_CHANGED_TESTS": str((tree / "test_flaky.py").relative_to(ROOT))}
    proc = _go(tmp_path, tree, extra, workers=1, reruns="--reruns=1")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    marks = _marks(tmp_path)
    assert marks[:2] == [("flaky", "shard-0", "True"), ("flaky", "shard-0", "True")], marks
    assert {name for name, _where, _x in marks[2:]} == {"normal", "web", "excl"}, marks
    assert "不再开始" not in out


def test_a_failing_shard_stops_the_other_shards_and_later_stages(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _hits_module(tree)
    # 分片 0：test_fail（2 个用例，先等分片 1 的慢用例开始再失败）；分片 1：test_slow。
    _test_file(
        tree,
        "test_fail",
        "import time\n\ndef test_a_fails():\n    _mark('fails')\n    end = time.monotonic() + 60\n"
        "    while time.monotonic() < end and 'slow-start' not in open(os.environ['HITS']).read():\n        time.sleep(0.05)\n"
        "    assert False, 'real regression'\n\n" + _simple("after"),
    )
    _test_file(
        tree,
        "test_slow",
        "import time\n\ndef test_slow():\n    Path(os.environ['HITS'] + '.pid').write_text(str(os.getpid()))\n"
        "    _mark('slow-start')\n    time.sleep(45)\n    _mark('slow-end')\n",
    )
    _test_file(tree, "test_web", _simple("web"), mark="browser_serial")
    _test_file(tree, "test_excl", _simple("excl"), mark="exclusive")
    started = time.monotonic()
    proc = _go(tmp_path, tree, {"GXR_STOP_ON_FAILURE": "1"})
    elapsed = time.monotonic() - started
    out = proc.stdout + proc.stderr
    pid = int(Path(str(tmp_path / "hits.log") + ".pid").read_text(encoding="utf-8"))
    try:
        assert proc.returncode == 1, out
        assert elapsed < 30, (elapsed, out)
        assert {name for name, _where, _x in _marks(tmp_path)} == {"fails", "slow-start"}, out
        assert "不再开始浏览器阶段" in out and "不再开始exclusive 阶段" in out, out
        assert not _alive(pid)
        cases = {case.attrib["name"]: case for case in ET.parse(tmp_path / "junit.xml").getroot().iter("testcase")}
        assert set(cases) == {"test_a_fails"} and cases["test_a_fails"].find("failure") is not None, cases
    finally:
        _stop_pid(pid)


def test_a_failing_browser_stage_stops_before_exclusive(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _write_tree(tree, exclusive=False)
    _test_file(tree, "test_web", "def test_web_a():\n    _mark('web_a')\n    assert False\n\n" + _simple("web_b"), mark="browser_serial")
    _test_file(tree, "test_excl", _simple("excl"), mark="exclusive")
    proc = _go(tmp_path, tree, {"GXR_STOP_ON_FAILURE": "1"})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert {name for name, _where, _x in _marks(tmp_path)} == {"web_a"}, out
    assert "不再开始exclusive 阶段" in out
    root = ET.parse(tmp_path / "junit.xml").getroot()
    assert sorted(case.attrib["name"] for case in root.iter("testcase")) == ["test_normal_alpha", "test_normal_beta", "test_web_a"]


def test_changed_test_files_run_first_in_their_shard_and_in_the_browser_stage(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _hits_module(tree)
    for name in ("a", "b", "c"):
        _test_file(tree, f"test_{name}", _simple(name))
    for name in ("w1", "w2", "w3"):
        _test_file(tree, f"test_{name}", _simple(name), mark="browser_serial")
    changed = [str((tree / "test_c.py").relative_to(ROOT)), str(tree / "test_w2.py")]
    proc = _go(tmp_path, tree, {"GXR_CHANGED_TESTS": os.pathsep.join(changed)}, workers=1)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [name for name, _where, _x in _marks(tmp_path)] == ["c", "a", "b", "w2", "w1", "w3"], proc.stdout
