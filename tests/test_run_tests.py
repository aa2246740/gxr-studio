"""scripts/run_tests.py 的分片计划、报告和信号清理。

这些测试自己拉起 runner，跑仓库里的临时小用例（放在仓库外时 pytest 会把
rootdir 走到别的配置上，分片拿到的相对路径对不上）。内层用假的 loadgate，
避免机器忙时变成 76，把计划本身的对错盖住。
"""

from __future__ import annotations

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

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_tests.py"
PROBE = ROOT / "src" / "gxr_studio" / "_run_tests_cov_probe.py"

# 内层 runner 再收集到本文件时直接跳过，避免计划泄漏后套娃把整仓再跑一遍。
pytestmark = pytest.mark.skipif(os.environ.get("GXR_RUN_TESTS_INNER") == "1", reason="inner runner")


@pytest.fixture
def scratch() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="run-tests-", dir=ROOT))
    PROBE.write_text("def touch():\n    return 1\n", encoding="utf-8")
    try:
        yield path
    finally:
        PROBE.unlink(missing_ok=True)
        shutil.rmtree(path, ignore_errors=True)


def _gate(path: Path, workers: int) -> None:
    path.write_text(f"def workers():\n    return {workers}, {{'fake': True}}\n", encoding="utf-8")


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
    PROBE.write_text(
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
