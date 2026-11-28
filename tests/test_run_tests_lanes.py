"""scripts/run_tests.py 的浏览器分道（GXR_BROWSER_LANES=2）和 browser_alone 单独阶段。

单独放一个文件：tests/test_run_tests_plan.py 再加这些会超过 800 行。拉起 runner 的用例沿用那边的
假 loadgate 和仓库里的临时小用例。
"""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from test_run_tests import _alive, _stop_pid, _write_tree
from test_run_tests import scratch as scratch  # 夹具
from test_run_tests_plan import _go, _junit, _plan_module

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(os.environ.get("GXR_RUN_TESTS_INNER") == "1", reason="inner runner")

# ---- 只看计划 ----------------------------------------------------------------------------------------------

# a 100 秒、b 10、c 10、d 3 个用例共 10 秒；s 是 browser_alone，40 秒。
_IDS = [
    "tests/p/test_a.py::test_one",
    "tests/p/test_b.py::test_one",
    "tests/p/test_s.py::test_one",
    "tests/p/test_c.py::test_one",
    "tests/p/test_d.py::TestK::test_x",
    "tests/p/test_d.py::TestK::test_y",
    "tests/p/test_d.py::test_z[1 2]",
]
_ALONE = ["tests/p/test_s.py::test_one"]
_TIMES = [
    ("tests.p.test_a", "test_one", 100.0),
    ("tests.p.test_b", "test_one", 10.0),
    ("tests.p.test_s", "test_one", 40.0),
    ("tests.p.test_c", "test_one", 10.0),
    ("tests.p.test_d.TestK", "test_x", 4.0),
    ("tests.p.test_d.TestK", "test_y", 4.0),
    ("tests.p.test_d", "test_z[1 2]", 2.0),
]


def test_lanes_split_by_junit_time_keep_files_together_and_leave_alone_for_last(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan = _plan_module()
    env = {plan.TIMES_ENV: str(_junit(tmp_path / "last.xml", _TIMES))}
    lanes, solo = plan.lane_plan(_IDS, _ALONE, 2, ROOT, env)
    assert lanes == [[_IDS[0]], [_IDS[1], _IDS[3], *_IDS[4:]]]
    assert solo == _ALONE
    out = capsys.readouterr().out
    assert "浏览器分道计划用时（分钟）：lane-0 1.7, lane-1 0.5；单独阶段 0.7（1 个用例）。" in out


def test_lanes_without_times_split_by_count(capsys: pytest.CaptureFixture[str]) -> None:
    plan = _plan_module()
    lanes, solo = plan.lane_plan(_IDS, _ALONE, 2, ROOT, {})
    # 按用例数、文件名排序：a、b 各 1 个，c 1 个，d 3 个 -> [a, c] [b, d]
    assert lanes == [[_IDS[0], _IDS[3]], [_IDS[1], *_IDS[4:]]]
    assert solo == _ALONE
    assert "浏览器分道（没有用时数据，按用例数）：lane-0 2 个, lane-1 4 个；单独阶段 1 个。" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("value", "workers", "lanes", "note"),
    [
        (None, 4, 1, ""),
        ("1", 4, 1, ""),
        (" 1 ", 4, 1, ""),
        ("2", 4, 2, "GXR_BROWSER_LANES=2：浏览器阶段分 2 道（min(2, 计算分片的进程数 4)）。"),
        ("2", 1, 1, "GXR_BROWSER_LANES=2：浏览器阶段分 1 道（min(2, 计算分片的进程数 1)）。"),
        ("3", 4, 1, "GXR_BROWSER_LANES='3' 不认识（只认 1 和 2），浏览器阶段照原来一道串行。"),
        ("two", 4, 1, "GXR_BROWSER_LANES='two' 不认识"),
        ("0", 4, 1, "GXR_BROWSER_LANES='0' 不认识"),
    ],
)
def test_lane_count_and_bad_values_fall_back_to_one_lane(
    capsys: pytest.CaptureFixture[str], value: str | None, workers: int, lanes: int, note: str
) -> None:
    plan = _plan_module()
    env = {} if value is None else {plan.LANES_ENV: value}
    assert plan.browser_lanes(env, workers) == lanes
    out = capsys.readouterr().out
    assert (note in out) if note else out == ""


@pytest.mark.parametrize("fault", ["alone-also-in-a-lane", "lost-from-lanes", "extra-in-a-lane"])
def test_lanes_and_alone_must_cover_the_browser_set_exactly_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fault: str
) -> None:
    """分道本身出错（这里换掉 _balance 来模拟）时，并集守门要大声失败，不能悄悄少跑或多跑。"""
    plan = _plan_module()
    real = plan._balance

    def broken(*args: Any) -> list[tuple[list[str], float]]:
        planned = real(*args)
        first = planned[0][0]
        if fault == "alone-also-in-a-lane":
            first.append(_ALONE[0])
        elif fault == "lost-from-lanes":
            first.pop()
        else:
            first.append("tests/p/test_x.py::test_new")
        return planned

    assert plan.lane_plan(_IDS, _ALONE, 2, ROOT, {})[1] == _ALONE
    monkeypatch.setattr(plan, "_balance", broken)
    with pytest.raises(SystemExit) as stop:
        plan.lane_plan(_IDS, _ALONE, 2, ROOT, {})
    assert stop.value.code == 2
    assert "分片计划和收集到的 node id 对不上" in capsys.readouterr().out


def test_changed_files_go_first_in_their_lane_and_in_the_alone_phase() -> None:
    plan = _plan_module()
    ids = [*_IDS, "tests/p/test_t.py::test_one"]
    alone = [*_ALONE, ids[-1]]
    env = {plan.CHANGED_ENV: os.pathsep.join(["tests/p/test_d.py", "tests/p/test_t.py"])}
    lanes, solo = plan.lane_plan(ids, alone, 2, ROOT, env)
    assert lanes == [[_IDS[0], _IDS[3]], [*_IDS[4:], _IDS[1]]]
    assert solo == [ids[-1], *_ALONE]


# ---- 拉起真 runner --------------------------------------------------------------------------------------

_RECORD = """import hits, json, os, sys, time
from pathlib import Path
import pytest


def _rec(name, **extra):
    plan = Path(sys.argv[2]).read_text(encoding='utf-8').split() if sys.argv[1:2] == ['--exec-plan'] else []
    row = {'name': name, 'slot': Path(os.environ['TMPDIR']).parent.name, 'tmpdir': os.environ['TMPDIR'], 'argv': sys.argv[1:], 'plan': plan}
    hits.hit('J ' + json.dumps({**row, **extra}))


def _seen(name, seconds=60):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if any(json.loads(l[2:])['name'] == name for l in open(os.environ['HITS']) if l.startswith('J ')):
            return True
        time.sleep(0.05)
    return False


"""


def _lane_file(tree: Path, name: str, body: str, mark: str = "browser_serial") -> None:
    marks = ", ".join(f"pytest.mark.{m}" for m in mark.split())
    (tree / f"test_{name}.py").write_text(f"{_RECORD}pytestmark = [{marks}]\n\n{body}", encoding="utf-8")


def _pair(me: str, other: str) -> str:
    """分道时等另一道的用例开始，证明两道同时在跑；一道时不等。"""
    return (
        f"def test_{me}(tmp_path):\n    _rec('{me}-start', tmp=str(tmp_path))\n"
        f"    if os.environ.get('GXR_BROWSER_LANES') == '2':\n        assert _seen('{other}-start'), 'the other lane never ran alongside'\n"
        f"    _rec('{me}-end')\n"
    )


def _lanes_tree(tree: Path) -> None:
    _write_tree(tree, exclusive=False)
    _lane_file(tree, "w1", _pair("w1", "w2"))
    _lane_file(tree, "w2", _pair("w2", "w1"))
    _lane_file(tree, "solo", "def test_solo(tmp_path):\n    _rec('solo', tmp=str(tmp_path))\n", "browser_serial browser_alone")


def _rows(tmp: Path) -> list[dict[str, Any]]:
    path = tmp / "hits.log"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    return [json.loads(line[2:]) for line in lines if line.startswith("J ")]


def _work(out: str) -> str:
    return next(line.split("分片临时目录 ", 1)[1].strip() for line in out.splitlines() if "分片临时目录 " in line)


def _commands(proc_out: str, rows: list[dict[str, Any]]) -> list[tuple[str, list[str], list[str]]]:
    """每个阶段第一次记录时的 (阶段, 命令行, 计划)，按开始的先后；临时目录换成 <W>。"""
    work = _work(proc_out)
    seen: dict[str, tuple[str, list[str], list[str]]] = {}
    for row in rows:
        seen.setdefault(row["slot"], (row["slot"], [a.replace(work, "<W>") for a in row["argv"]], row["plan"]))
    return list(seen.values())


def _today(rel: str, ids: list[str]) -> list[str]:
    """origin/main 的浏览器阶段命令（run_tests._stage_cmd），没有 GXR_STOP_ON_FAILURE 时不带 -x。"""
    head = ["--exec-plan", "<W>/browser/ids.txt", "--", "-q", "--durations=20", "--reruns", "1", "--cov=gxr_studio", "--cov-report="]
    tail = ["--cov-fail-under=0", "--basetemp", "<W>/browser/base", "--junitxml", "<W>/browser/junit.xml"]
    return [*head, *tail, *(f"{rel}/{name}" for name in ids)]


def test_one_lane_mode_runs_todays_commands_and_lane_mode_does_not(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _lanes_tree(tree)
    rel = str(tree.relative_to(ROOT))
    browser_ids = [f"{rel}/test_solo.py::test_solo", f"{rel}/test_w1.py::test_w1", f"{rel}/test_w2.py::test_w2"]
    runs: dict[str, list[tuple[str, list[str], list[str]]]] = {}
    for value in (None, "1", "bogus", "2"):
        tmp = tmp_path / str(value)
        tmp.mkdir()
        proc = _go(tmp, tree, {} if value is None else {"GXR_BROWSER_LANES": value})
        out = proc.stdout + proc.stderr
        assert proc.returncode == 0, out
        runs[str(value)] = _commands(out, _rows(tmp))
        assert ("GXR_BROWSER_LANES='bogus' 不认识" in out) == (value == "bogus"), out
    today = [cmd for cmd in runs["None"] if cmd[0] == "browser"]
    assert today == [("browser", _today(rel, ["test_solo.py", "test_w1.py", "test_w2.py"]), browser_ids)], runs["None"]
    assert runs["1"] == runs["None"] and runs["bogus"] == runs["None"], runs
    assert [cmd[0] for cmd in runs["None"]] == ["browser"], runs["None"]
    lanes = {slot: plan for slot, _argv, plan in runs["2"]}
    assert lanes == {"browser-lane-0": browser_ids[1:2], "browser-lane-1": browser_ids[2:], "browser-alone": browser_ids[:1]}, runs["2"]


def test_lanes_run_together_and_the_alone_phase_runs_after_both(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _lanes_tree(tree)
    _lane_file(tree, "excl", "def test_excl():\n    _rec('excl')\n", "exclusive")
    proc = _go(tmp_path, tree, {"GXR_BROWSER_LANES": "2"})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    order = [row["name"] for row in _rows(tmp_path)]
    assert set(order[:2]) == {"w1-start", "w2-start"} and set(order[2:4]) == {"w1-end", "w2-end"}, order
    assert order[4:] == ["solo", "excl"], order
    assert "浏览器阶段：2 道并行，不拿锁，2 个用例；之后单独阶段 1 个。" in out
    assert "浏览器分道（没有用时数据，按用例数）：lane-0 1 个, lane-1 1 个；单独阶段 1 个。" in out
    names = sorted(case.attrib["name"] for case in ET.parse(tmp_path / "junit.xml").getroot().iter("testcase"))
    assert names == ["test_excl", "test_normal_alpha", "test_normal_beta", "test_solo", "test_w1", "test_w2"]


def test_each_lane_has_its_own_tmpdir_and_basetemp(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _lanes_tree(tree)
    proc = _go(tmp_path, tree, {"GXR_BROWSER_LANES": "2"})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    work = Path(_work(out))
    starts = {row["name"]: row for row in _rows(tmp_path) if "tmp" in row}
    for name, slot in (("w1-start", "browser-lane-0"), ("w2-start", "browser-lane-1"), ("solo", "browser-alone")):
        row = starts[name]
        assert row["slot"] == slot and row["tmpdir"] == str(work / slot / "tmp"), row
        assert Path(row["tmp"]).is_relative_to(work / slot / "base"), row
        assert (work / slot / "junit.xml").is_file() and (work / slot / ".coverage").exists(), slot


def test_a_failing_lane_stops_the_other_lane_and_later_phases(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _write_tree(tree, exclusive=False)
    _lane_file(
        tree,
        "w1",
        "def test_w1():\n    _rec('w1-start')\n    assert _seen('w2-start')\n    assert False, 'real regression'\n\n"
        "def test_w1_after():\n    _rec('w1-after')\n",
    )
    _lane_file(
        tree,
        "w2",
        "def test_w2():\n    Path(os.environ['HITS'] + '.pid').write_text(str(os.getpid()))\n"
        "    _rec('w2-start')\n    time.sleep(45)\n    _rec('w2-end')\n",
    )
    _lane_file(tree, "solo", "def test_solo():\n    _rec('solo')\n", "browser_serial browser_alone")
    _lane_file(tree, "excl", "def test_excl():\n    _rec('excl')\n", "exclusive")
    proc = _go(tmp_path, tree, {"GXR_BROWSER_LANES": "2", "GXR_STOP_ON_FAILURE": "1"})
    out = proc.stdout + proc.stderr
    pid = int(Path(str(tmp_path / "hits.log") + ".pid").read_text(encoding="utf-8"))
    try:
        assert proc.returncode == 1, out
        assert {row["name"] for row in _rows(tmp_path)} == {"w1-start", "w2-start"}, out
        assert "[browser-lane-0] 失败已成定局（GXR_STOP_ON_FAILURE=1），停掉其他分片。" in out, out
        assert "不再开始浏览器单独阶段" in out and "不再开始exclusive 阶段" in out, out
        assert not _alive(pid)
        cases = {case.attrib["name"]: case for case in ET.parse(tmp_path / "junit.xml").getroot().iter("testcase")}
        assert set(cases) == {"test_normal_alpha", "test_normal_beta", "test_w1"}, cases
        assert cases["test_w1"].find("failure") is not None
    finally:
        _stop_pid(pid)


def test_changed_files_run_first_in_their_lane_and_in_the_alone_phase(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _write_tree(tree, exclusive=False)
    for name in ("w1", "w2", "w3"):
        _lane_file(tree, name, f"def test_{name}():\n    _rec('{name}')\n")
    for name in ("s1", "s2"):
        _lane_file(tree, name, f"def test_{name}():\n    _rec('{name}')\n", "browser_serial browser_alone")
    changed = [str((tree / "test_w3.py").relative_to(ROOT)), str(tree / "test_s2.py")]
    proc = _go(tmp_path, tree, {"GXR_BROWSER_LANES": "2", "GXR_CHANGED_TESTS": os.pathsep.join(changed)})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    by_slot: dict[str, list[str]] = {}
    for row in _rows(tmp_path):
        by_slot.setdefault(row["slot"], []).append(row["name"])
    # 按用例数：w1、w3 在第 0 道，w2 在第 1 道；改过的 w3 和 s2 各自先跑。
    assert by_slot == {"browser-lane-0": ["w3", "w1"], "browser-lane-1": ["w2"], "browser-alone": ["s2", "s1"]}, out
