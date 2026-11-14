"""scripts/run_tests.py 的分片计划、报告和信号清理。

这些测试自己拉起 runner，跑仓库里的临时小用例（放在仓库外时 pytest 会把
rootdir 走到别的配置上，分片拿到的相对路径对不上）。内层用假的 loadgate，
避免机器忙时变成 76，把计划本身的对错盖住。
"""

from __future__ import annotations

import ast
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_tests.py"


def _probe(scratch: Path) -> Path:
    return scratch / "_run_tests_cov_probe.py"


# 内层 runner 再收集到本文件时直接跳过，避免计划泄漏后套娃把整仓再跑一遍。
pytestmark = pytest.mark.skipif(os.environ.get("GXR_RUN_TESTS_INNER") == "1", reason="inner runner")


@pytest.fixture
def scratch() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="run-tests-", dir=ROOT))
    _probe(path).write_text("def touch():\n    return 1\n", encoding="utf-8")
    # 只有内层 pytest 加载探针目录，其他分片扫描产品源码时不会收录这个临时模块。
    (path / "conftest.py").write_text(
        "from pathlib import Path\nimport gxr_studio\n\ngxr_studio.__path__.append(str(Path(__file__).parent))\n",
        encoding="utf-8",
    )
    try:
        yield path
    finally:
        _probe(path).unlink(missing_ok=True)
        shutil.rmtree(path, ignore_errors=True)


def _gate(path: Path, workers: int) -> None:
    """假的 loadgate：给定进程数，安静判断总是安静，免得真机器忙时外层自测也去等。"""
    path.write_text(
        f"def workers():\n    return {workers}, {{'fake': True}}\n\ndef quiet_for_timing():\n    return True, {{'fake': True}}\n",
        encoding="utf-8",
    )


def _rcfile(path: Path) -> None:
    path.write_text(
        "[run]\nsource = gxr_studio\n[report]\ninclude = */_run_tests_cov_probe.py\n",
        encoding="utf-8",
    )


def _write_tree(directory: Path, *, exclusive: bool = True, space: bool = False) -> None:
    directory.mkdir(parents=True)
    (directory / "hits.py").write_text(
        "import fcntl\nimport os\n\n"
        "def hit(name):\n"
        "    with open(os.environ['HITS'], 'a', encoding='utf-8') as handle:\n"
        "        fcntl.flock(handle, fcntl.LOCK_EX)\n"
        "        handle.write(name + '\\n')\n"
        "        handle.flush()\n"
        "        fcntl.flock(handle, fcntl.LOCK_UN)\n",
        encoding="utf-8",
    )
    (directory / "test_alpha.py").write_text(
        "import hits\nfrom gxr_studio import _run_tests_cov_probe as target\n\n"
        "def test_normal_alpha():\n"
        "    assert target.touch() == 1\n"
        "    hits.hit('alpha')\n",
        encoding="utf-8",
    )
    (directory / "test_beta.py").write_text(
        "import hits\nfrom gxr_studio import _run_tests_cov_probe as target\n\n"
        "def test_normal_beta():\n"
        "    assert target.touch() == 1\n"
        "    hits.hit('beta')\n",
        encoding="utf-8",
    )
    if space:
        (directory / "test_space.py").write_text(
            "import hits\nimport pytest\nfrom gxr_studio import _run_tests_cov_probe as target\n\n"
            "@pytest.mark.parametrize('label', ['a b'])\n"
            "def test_space(label):\n"
            "    assert target.touch() == 1\n"
            "    hits.hit('space')\n",
            encoding="utf-8",
        )
    if exclusive:
        (directory / "test_exclusive.py").write_text(
            "import hits\nimport pytest\nfrom gxr_studio import _run_tests_cov_probe as target\n\n"
            "@pytest.mark.exclusive\n"
            "def test_exclusive_probe():\n"
            "    assert target.touch() == 1\n"
            "    hits.hit('exclusive')\n",
            encoding="utf-8",
        )


def _runner_env(tmp: Path, addopts: list[str], *, workers: int) -> dict[str, str]:
    gate = tmp / "gate.py"
    _gate(gate, workers)
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["GXR_LOADGATE"] = str(gate)
    env["GXR_RUN_TESTS_INNER"] = "1"
    env["HITS"] = str(tmp / "hits.log")
    env["COVERAGE_FILE"] = str(tmp / "outer.coverage")
    env["COVERAGE_RCFILE"] = str(tmp / "coveragerc")
    env["PYTEST_ADDOPTS"] = shlex.join(addopts)
    return env


def _run(tmp: Path, addopts: list[str], *, workers: int = 2, timeout: float = 90) -> subprocess.CompletedProcess[str]:
    _rcfile(tmp / "coveragerc")
    return subprocess.run(
        [sys.executable, "-I", str(RUNNER)],
        cwd=ROOT,
        env=_runner_env(tmp, addopts, workers=workers),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _hits(tmp: Path) -> dict[str, int]:
    path = tmp / "hits.log"
    found: dict[str, int] = {}
    if not path.is_file():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        found[line] = found.get(line, 0) + 1
    return found


def _junit_tests(path: Path) -> int:
    root = ET.parse(path).getroot()
    return sum(int(suite.attrib.get("tests", "0")) for suite in root.iter("testsuite"))


def _base_opts(tree: Path, junit: Path) -> list[str]:
    relative = tree.relative_to(ROOT)
    return ["--force-reruns=0", "--rootdir", str(ROOT), "--junitxml", str(junit), "-o", f"testpaths={relative}"]


def test_coverage_report_survives_runner_probe_cleanup(tmp_path: Path, scratch: Path) -> None:
    """并行分片的覆盖率扫描不能收录 runner 自测稍后会删除的临时模块。"""
    script = tmp_path / "import_package.py"
    script.write_text("import gxr_studio\nassert gxr_studio.__version__\n", encoding="utf-8")
    data = tmp_path / "scan.coverage"
    command = [sys.executable, "-I", "-m", "coverage"]
    config = f"--rcfile={ROOT / 'pyproject.toml'}"
    measured = subprocess.run(
        [*command, "run", config, "--source=gxr_studio", f"--data-file={data}", str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert measured.returncode == 0, measured.stdout + measured.stderr
    _probe(scratch).unlink()
    report = subprocess.run(
        [*command, "report", config, f"--data-file={data}", "--fail-under=0"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert report.returncode == 0, report.stdout + report.stderr


def test_positional_selectors_run_each_planned_test_once(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _write_tree(tree, space=True)
    junit = tmp_path / "junit.xml"
    proc = _run(tmp_path, ["--force-reruns=0", "--rootdir", str(ROOT), "--junitxml", str(junit), str(tree.relative_to(ROOT))])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _hits(tmp_path) == {"alpha": 1, "beta": 1, "space": 1, "exclusive": 1}
    assert _junit_tests(junit) == 4


def test_empty_exclusive_stage_still_runs_the_normal_test(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _write_tree(tree)
    junit = tmp_path / "junit.xml"
    proc = _run(tmp_path, [*_base_opts(tree, junit), "-k", "normal_alpha"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _hits(tmp_path) == {"alpha": 1}
    assert _junit_tests(junit) == 1
    assert "没有 exclusive" in proc.stdout


def test_empty_normal_stage_still_runs_the_exclusive_test(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _write_tree(tree)
    junit = tmp_path / "junit.xml"
    proc = _run(tmp_path, [*_base_opts(tree, junit), "-k", "test_exclusive_probe"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _hits(tmp_path) == {"exclusive": 1}
    assert _junit_tests(junit) == 1
    assert "没有普通测试" in proc.stdout


def test_markexpr_does_not_reexpand_into_exclusive(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _write_tree(tree)
    junit = tmp_path / "junit.xml"
    proc = _run(tmp_path, [*_base_opts(tree, junit), "-m", "not exclusive"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _hits(tmp_path) == {"alpha": 1, "beta": 1}
    assert _junit_tests(junit) == 2


def test_junit_alias_and_coverage_xml_use_the_combined_result(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    tree.mkdir()
    _probe(scratch).write_text(
        "def first():\n    return 17\n\ndef second():\n    return 23\n\ndef third():\n    return 31\n",
        encoding="utf-8",
    )
    (tree / "test_alpha.py").write_text(
        "from gxr_studio import _run_tests_cov_probe as target\n\ndef test_normal_alpha():\n    assert target.first() == 17\n",
        encoding="utf-8",
    )
    (tree / "test_beta.py").write_text(
        "from gxr_studio import _run_tests_cov_probe as target\n\ndef test_normal_beta():\n    assert target.second() == 23\n",
        encoding="utf-8",
    )
    (tree / "test_exclusive.py").write_text(
        "import pytest\nfrom gxr_studio import _run_tests_cov_probe as target\n\n"
        "@pytest.mark.exclusive\n"
        "def test_exclusive_probe():\n"
        "    assert target.third() == 31\n",
        encoding="utf-8",
    )
    junit = tmp_path / "junit.xml"
    xml_report = tmp_path / "coverage.xml"
    relative = tree.relative_to(ROOT)
    proc = _run(
        tmp_path,
        [
            "--force-reruns=0",
            "--rootdir",
            str(ROOT),
            "--junit-xml",
            str(junit),
            "--cov-report=xml:" + str(xml_report),
            "-o",
            f"testpaths={relative}",
        ],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _junit_tests(junit) == 3
    rate = float(ET.parse(xml_report).getroot().attrib["line-rate"])
    assert rate == 1.0, ET.parse(xml_report).getroot().attrib


def _signal_tree(directory: Path) -> None:
    directory.mkdir(parents=True)
    (directory / "test_alpha.py").write_text(
        "import json\nimport os\nimport subprocess\nimport sys\nimport time\nfrom pathlib import Path\n\n"
        "def test_normal_alpha():\n"
        "    proc = subprocess.Popen(\n"
        "        [sys.executable, '-I', '-c', 'import time; time.sleep(180)'],\n"
        "        start_new_session=True,\n"
        "    )\n"
        "    Path(os.environ['SIGNAL_READY']).write_text(json.dumps({'pytest': os.getpid(), 'sleep': proc.pid}))\n"
        "    try:\n"
        "        time.sleep(180)\n"
        "    finally:\n"
        "        proc.terminate()\n"
        "        proc.wait(timeout=10)\n",
        encoding="utf-8",
    )
    (directory / "test_exclusive.py").write_text(
        "import pytest\n\n@pytest.mark.exclusive\ndef test_exclusive_probe():\n    assert True\n",
        encoding="utf-8",
    )


def _alive(pid: int) -> bool:
    listed = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False)
    return listed.returncode == 0 and not listed.stdout.strip().startswith("Z")


def _stop_pid(pid: int) -> None:
    if not _alive(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_signal(tmp: Path, scratch: Path, signum: int, expected: int) -> None:
    tree = scratch / "tree"
    _signal_tree(tree)
    _rcfile(tmp / "coveragerc")
    ready = tmp / "ready.json"
    relative = tree.relative_to(ROOT)
    env = _runner_env(tmp, ["--force-reruns=0", "--rootdir", str(ROOT), str(relative)], workers=1)
    env["SIGNAL_READY"] = str(ready)
    proc = subprocess.Popen(
        [sys.executable, "-I", str(RUNNER)],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pids: dict[str, int] = {}
    try:
        deadline = time.monotonic() + 45
        while not ready.is_file() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        output = ""
        if not ready.is_file():
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            output = proc.stdout.read() if proc.stdout is not None else ""
        assert ready.is_file(), "runner 没有进入会留下孙进程的用例\n" + output
        pids = json.loads(ready.read_text(encoding="utf-8"))
        proc.send_signal(signum)
        code = proc.wait(timeout=30)
        time.sleep(0.5)
        assert code == expected, code
        assert not _alive(pids["pytest"])
        assert not _alive(pids["sleep"])
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=10)
        for pid in pids.values():
            _stop_pid(pid)


def test_sigterm_reaps_the_grandchild_process_group(tmp_path: Path, scratch: Path) -> None:
    _run_signal(tmp_path, scratch, signal.SIGTERM, 143)


def test_sigint_reaps_the_grandchild_process_group(tmp_path: Path, scratch: Path) -> None:
    _run_signal(tmp_path, scratch, signal.SIGINT, 130)


def test_split_addopts_keeps_reports_and_selectors_apart() -> None:
    import importlib.util

    sys.path.insert(0, str(RUNNER.parent))
    spec = importlib.util.spec_from_file_location("gxr_run_tests_mod", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    raw = "--force-reruns=0 --junit-xml report.xml --cov-report=xml:cover.xml --cov-report term-missing"
    raw += " -k 'normal alpha' -m 'not exclusive' tests/tree"
    parsed = module.split_addopts(raw)
    assert parsed.junit == "report.xml"
    assert parsed.cov_reports == ["xml:cover.xml", "term-missing"]
    assert parsed.selection == ["-k", "normal alpha", "-m", "not exclusive", "tests/tree"]
    assert "--junit-xml" not in parsed.kept
    assert "--junitxml" not in parsed.kept
    assert not any(arg.startswith("--cov-report") for arg in parsed.kept)
    assert "-k" not in parsed.kept
    assert "tests/tree" not in parsed.kept
    assert "--force-reruns=0" in parsed.kept


def test_raise_nice_keeps_going_when_the_os_refuses(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """有的启动环境不许改优先级（2026-11-12 make check 里 os.nice(0) 报 EPERM），runner 要照常跑。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("gxr_run_tests_nice", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    def refuse(*_args: object) -> int:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "nice", refuse, raising=False)
    monkeypatch.setattr(os, "getpriority", lambda *_args: 0, raising=False)
    monkeypatch.setattr(os, "setpriority", refuse, raising=False)
    module.raise_nice()
    assert "nice 15" in capsys.readouterr().out


def test_cov_config_option_reaches_the_combined_report(tmp_path: Path, scratch: Path) -> None:
    """只在 PYTEST_ADDOPTS 里写 --cov-config（不设 COVERAGE_RCFILE）时，父进程合并和出报告也要用它。"""
    tree = scratch / "tree"
    _write_tree(tree)
    rcfile = tmp_path / "only-probe.coveragerc"
    _rcfile(rcfile)
    xml_report = tmp_path / "coverage.xml"
    opts = [*_base_opts(tree, tmp_path / "junit.xml"), f"--cov-config={rcfile}", f"--cov-report=xml:{xml_report}"]
    env = _runner_env(tmp_path, opts, workers=2)
    env.pop("COVERAGE_RCFILE")
    proc = subprocess.run(
        [sys.executable, "-I", str(RUNNER)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    root = ET.parse(xml_report).getroot()
    assert root.attrib["lines-valid"] == root.attrib["lines-covered"] == "2"
    assert float(root.attrib["line-rate"]) == 1.0


def _run_with_config(
    tmp_path: Path, tree: Path, cov_config: str, reports: list[str], rcfile_env: str | None
) -> subprocess.CompletedProcess[str]:
    opts = [*_base_opts(tree, tmp_path / "junit.xml"), f"--cov-config={cov_config}", *(f"--cov-report={spec}" for spec in reports)]
    env = _runner_env(tmp_path, opts, workers=2)
    env.pop("COVERAGE_RCFILE")
    if rcfile_env is not None:
        env["COVERAGE_RCFILE"] = rcfile_env
    return subprocess.run(
        [sys.executable, "-I", str(RUNNER)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )


def _xml_lines(path: Path) -> tuple[str, str]:
    root = ET.parse(path).getroot()
    return root.attrib["lines-covered"], root.attrib["lines-valid"]


def test_file_reports_leave_the_coverage_floor_to_the_gate(tmp_path: Path, scratch: Path) -> None:
    """配置里写 fail_under = 100 时，xml/html/json/lcov 不能在 85% 门槛之外再卡一次（串行 pytest 用 --cov-fail-under=85）。"""
    _probe(scratch).write_text(
        "def touch():\n    a = 1\n    b = a + 1\n    c = b + 1\n    d = c + 1\n    return d - 3\n\n\ndef unused():\n    return 2\n",
        encoding="utf-8",
    )
    tree = scratch / "tree"
    _write_tree(tree)
    rcfile = tmp_path / "strict.coveragerc"
    rcfile.write_text(
        "[run]\nsource = gxr_studio\n[report]\ninclude = */_run_tests_cov_probe.py\nfail_under = 100\n",
        encoding="utf-8",
    )
    xml_report = tmp_path / "coverage.xml"
    reports = [f"xml:{xml_report}", f"json:{tmp_path / 'coverage.json'}", f"html:{tmp_path / 'html'}", f"lcov:{tmp_path / 'coverage.lcov'}"]
    proc = _run_with_config(tmp_path, tree, str(rcfile), reports, None)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _xml_lines(xml_report) == ("7", "8")
    assert (tmp_path / "coverage.json").is_file()
    assert (tmp_path / "html" / "index.html").is_file()
    assert (tmp_path / "coverage.lcov").is_file()


def test_cov_config_value_reaches_coverage_unchanged(tmp_path: Path, scratch: Path) -> None:
    """父进程原样转交 --cov-config：字面量 .coveragerc 仍走 coverage 的默认查找（含 COVERAGE_RCFILE），
    指向无后缀文件的 *.toml 链接仍按 TOML 读。不能先解析成绝对路径或链接目标。"""
    assert not (ROOT / ".coveragerc").exists()
    tree = scratch / "tree"
    _write_tree(tree)
    env_rc = tmp_path / "from-env.coveragerc"
    _rcfile(env_rc)
    default_xml = tmp_path / "default.xml"
    proc = _run_with_config(tmp_path, tree, ".coveragerc", [f"xml:{default_xml}"], str(env_rc))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _xml_lines(default_xml) == ("2", "2")

    real = tmp_path / "toml-without-suffix"
    real.write_text(
        '[tool.coverage.run]\nsource = ["gxr_studio"]\n[tool.coverage.report]\ninclude = ["*/_run_tests_cov_probe.py"]\n',
        encoding="utf-8",
    )
    link = tmp_path / "via-link.toml"
    link.symlink_to(real)
    toml_xml = tmp_path / "toml.xml"
    (tmp_path / "hits.log").unlink(missing_ok=True)
    proc = _run_with_config(tmp_path, tree, str(link), [f"xml:{toml_xml}"], None)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _xml_lines(toml_xml) == ("2", "2")


# ---- exclusive：先等安静，只有墙钟预算没过的用例安静后再跑一次 ----------------------------------------

_SPEED_CLASS = "class SpeedBudgetExceeded(AssertionError):\n    gxr_speed_budget = True\n\n"


def _exclusive_tree(directory: Path, body: str, *, extra: str = "") -> None:
    """一个普通用例加一个 exclusive 用例。body 是 exclusive 用例的函数体；runs 文件数这个用例跑了几次。"""
    _write_tree(directory, exclusive=False)
    (directory / "test_speed.py").write_text(
        "import os\nimport hits\nimport pytest\n\n" + _SPEED_CLASS + extra + "@pytest.mark.exclusive\ndef test_speed_probe(request):\n"
        "    hits.hit('speed')\n"
        "    runs = sum(1 for line in open(os.environ['HITS'], encoding='utf-8') if line.strip() == 'speed')\n" + body,
        encoding="utf-8",
    )


def _quiet_env(tmp: Path, gate_body: str | None, *, budget: float = 30, poll: float = 0.05) -> dict[str, str]:
    """gate_body 是假 loadgate 里 quiet_for_timing 之外的源码；None 表示不设 GXR_LOADGATE。"""
    env = {"GXR_QUIET_MAX_WAIT_S": str(budget), "GXR_QUIET_POLL_S": str(poll), "GATE_COUNT": str(tmp / "gate-count")}
    if gate_body is not None:
        gate = tmp / "quiet_gate.py"
        gate.write_text("import os\n\ndef workers():\n    return 2, {'fake': True}\n\n" + gate_body, encoding="utf-8")
        env["GXR_LOADGATE"] = str(gate)
    return env


def _run_quiet(tmp: Path, tree: Path, extra_env: dict[str, str], *, unset_gate: bool = False) -> subprocess.CompletedProcess[str]:
    _rcfile(tmp / "coveragerc")
    env = _runner_env(tmp, _base_opts(tree, tmp / "junit.xml"), workers=2)
    if unset_gate:
        env.pop("GXR_LOADGATE")
    env.update(extra_env)
    return subprocess.run([sys.executable, "-I", str(RUNNER)], cwd=ROOT, env=env, text=True, capture_output=True, timeout=120, check=False)


_COUNTING_GATE = """
def quiet_for_timing():
    path = os.environ['GATE_COUNT']
    n = (int(open(path).read()) if os.path.exists(path) else 0) + 1
    open(path, 'w').write(str(n))
    return n > {busy}, {{'idle_cores': 9.0 if n > {busy} else 1.0, 'check': n}}
"""


def _junit_case(path: Path, name: str) -> list[ET.Element]:
    return [case for case in ET.parse(path).getroot().iter("testcase") if case.attrib.get("name") == name]


def test_quiet_immediately_runs_the_exclusive_stage_after_one_check(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _exclusive_tree(tree, "    assert runs == 1\n")
    proc = _run_quiet(tmp_path, tree, _quiet_env(tmp_path, _COUNTING_GATE.format(busy=0)))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "等机器安静（exclusive 阶段开始前）：等了 0 秒，问了 1 次，安静了，loadgate.quiet_for_timing" in out
    assert "等机器安静（exclusive 用例之前：" in out and "test_speed_probe" in out
    assert _hits(tmp_path) == {"alpha": 1, "beta": 1, "speed": 1}


def test_quiet_after_three_busy_checks(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _exclusive_tree(tree, "    assert runs == 1\n")
    proc = _run_quiet(tmp_path, tree, _quiet_env(tmp_path, _COUNTING_GATE.format(busy=3)))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "等机器安静（exclusive 阶段开始前）：" in out and "问了 4 次，安静了" in out, out
    assert '安静了，loadgate.quiet_for_timing {"idle_cores": 9.0}' in out
    assert "一直没安静下来" not in out


def test_never_quiet_times_out_and_runs_anyway(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _exclusive_tree(tree, "    assert runs == 1\n")
    env = _quiet_env(tmp_path, _COUNTING_GATE.format(busy=10**9), budget=0.4, poll=0.1)
    proc = _run_quiet(tmp_path, tree, env)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "机器一直没安静下来（exclusive 阶段开始前）" in out and "照样跑" in out and "idle_cores" in out, out
    assert "机器一直没安静下来（exclusive 用例之前：" in out  # 预算用完以后每个用例只问一次
    assert _hits(tmp_path)["speed"] == 1


@pytest.mark.parametrize(
    ("gate_body", "unset", "expect"),
    [
        (None, True, ["没有设置", "公开回退 load1="]),
        ("def quiet_for_timing():\n    raise RuntimeError('iostat gone')\n", False, ["安静判断出错", "iostat gone", "公开回退 load1="]),
        ("", False, ["没有 quiet_for_timing 或 cap", "公开回退 load1="]),
        ("def cap(running):\n    return running + 1, {'admit': True, 'spare': 3.0}\n", False, ["loadgate.cap admit"]),
    ],
    ids=["unset", "raises", "no-function", "cap-only"],
)
def test_loadgate_when_usable_and_the_public_fallback_otherwise(
    tmp_path: Path, scratch: Path, gate_body: str | None, unset: bool, expect: list[str]
) -> None:
    tree = scratch / "tree"
    _exclusive_tree(tree, "    assert runs == 1\n")
    # 回退看的是真机器的 load1：预算给得很短，忙也只等一下。
    proc = _run_quiet(tmp_path, tree, _quiet_env(tmp_path, gate_body, budget=0.3, poll=0.1), unset_gate=unset)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    for text in expect:
        assert text in out, (text, out)
    if "公开回退" not in expect[-1]:
        assert "公开回退" not in out


_RETRY_PASSES = "    if runs == 1:\n        raise SpeedBudgetExceeded('visibleMs 7194 > 1000')\n"
_ALWAYS_SLOW = "    raise SpeedBudgetExceeded(f'visibleMs 7194 > 1000 (run {runs})')\n"
_FUNCTIONAL = "    assert runs == 99, 'box did not move'\n"
_TEARDOWN = "    request.addfinalizer(lambda: (_ for _ in ()).throw(RuntimeError('teardown broke')))\n" + _ALWAYS_SLOW


def test_a_speed_failure_is_retried_once_when_quiet_and_the_second_run_counts(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _exclusive_tree(tree, _RETRY_PASSES)
    proc = _run_quiet(tmp_path, tree, _quiet_env(tmp_path, _COUNTING_GATE.format(busy=0)))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert _hits(tmp_path)["speed"] == 2
    assert "[exclusive] " in out and "[exclusive-retry] " in out  # 两次的输出都在
    assert "等机器安静（速度预算重试之前）" in out
    assert "test_speed_probe 第一次只有墙钟预算没过（speed），等安静（或等待预算用完）后重跑，第二次通过，以第二次为准" in out
    cases = _junit_case(tmp_path / "junit.xml", "test_speed_probe")
    assert len(cases) == 1 and cases[0].find("failure") is None
    suite = ET.parse(tmp_path / "junit.xml").getroot().find("testsuite")
    assert suite is not None and suite.attrib["failures"] == "0" and suite.attrib["tests"] == "3"


@pytest.mark.parametrize("body", [_FUNCTIONAL, _TEARDOWN], ids=["functional", "speed-then-teardown-error"])
def test_a_non_speed_failure_is_never_retried(tmp_path: Path, scratch: Path, body: str) -> None:
    tree = scratch / "tree"
    _exclusive_tree(tree, body)
    proc = _run_quiet(tmp_path, tree, _quiet_env(tmp_path, _COUNTING_GATE.format(busy=0)))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert _hits(tmp_path)["speed"] == 1
    assert "[exclusive-retry]" not in out and "速度预算重试" not in out


def test_a_speed_failure_that_fails_twice_stays_failed(tmp_path: Path, scratch: Path) -> None:
    tree = scratch / "tree"
    _exclusive_tree(tree, _ALWAYS_SLOW)
    proc = _run_quiet(tmp_path, tree, _quiet_env(tmp_path, _COUNTING_GATE.format(busy=0)))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert _hits(tmp_path)["speed"] == 2
    assert "等安静（或等待预算用完）后重跑，第二次仍然失败（speed）" in out and "(run 2)" in out
    cases = _junit_case(tmp_path / "junit.xml", "test_speed_probe")
    assert len(cases) == 1 and cases[0].find("failure") is not None


def _quiet_module() -> object:
    import importlib.util

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
