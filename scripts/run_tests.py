#!/usr/bin/env python3
"""把不依赖墙钟速度的测试分片并行跑，再串行跑长时间浏览器流程和 exclusive。

进程数来自环境变量 GXR_LOADGATE 指向的 loadgate.py（workers()，最多 6）。
没设置或文件不存在时，改用 min(cpu_count - 1, 4)，至少 1。公共仓库不写死私有路径。
收集只做一次。子进程只跑计划里的 node id，不再继承位置参数、-m 和 -k。
标了 browser_serial 的用例在计算分片结束之后单独串行，不拿锁。
exclusive 仍串行，并且用 lockf 拿 /tmp/gxr-heavy.lock。开始前和每个用例之前先等机器安静；
只有墙钟预算（SpeedBudgetExceeded）没过的用例，安静后再跑一次，以第二次为准（run_tests_quiet.py）。
JUnit 和覆盖率报告由父进程按合并后的数据写出。门槛 85。
收到 SIGINT 退出 130，收到 SIGTERM 退出 143，并清掉子进程组。
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# python -I 不会把脚本目录放进 sys.path，同目录的报告模块要自己加。
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from run_tests_quiet import BUDGET_ENV, exclusive_plugin, load_gate, make_gate, run_exclusive  # noqa: E402
from run_tests_report import combine_coverage, merge_junit  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LOADGATE_ENV = "GXR_LOADGATE"
LOCK_PATH = "/tmp/gxr-heavy.lock"
MAX_SHARDS = 6
FALLBACK_CAP = 4
_GRACE_S = 1.0
_ALLOWED = ("python", "pytest", "node", "sleep", "lockf", "chrome", "chromium", "headless")
_TAKES_VALUE = {
    "-k",
    "--keyword",
    "-m",
    "--markexpr",
    "-o",
    "--override-ini",
    "-p",
    "-c",
    "--config-file",
    "-W",
    "--pythonwarnings",
    "-r",
    "--report-chars",
    "--basetemp",
    "--junitxml",
    "--junit-xml",
    "--junitprefix",
    "--junit-prefix",
    "--cov",
    "--cov-report",
    "--cov-config",
    "--cov-fail-under",
    "--cov-precision",
    "--cov-context",
    "--reruns",
    "--force-reruns",
    "--only-rerun",
    "--reruns-delay",
    "--reruns-delay-backoff-factor",
    "--rerun-except",
    "--rerun-exclude-path",
    "--reruns-mode",
    "--max-suite-reruns",
    "--durations",
    "--durations-min",
    "--tb",
    "--rootdir",
    "--confcutdir",
    "--deselect",
    "--ignore",
    "--ignore-glob",
    "--import-mode",
    "--capture",
    "--maxfail",
    "--max-warnings",
    "--log-file",
    "--log-file-mode",
    "--log-file-level",
    "--log-file-format",
    "--log-file-date-format",
    "--log-level",
    "--log-format",
    "--log-date-format",
    "--log-cli-level",
    "--log-cli-format",
    "--log-cli-date-format",
    "--log-auto-indent",
    "--log-disable",
    "--verbosity",
    "--color",
    "--code-highlight",
    "--show-capture",
    "--assert",
    "--pastebin",
    "--doctest-glob",
    "--doctest-report",
    "--pdbcls",
    "--lfnf",
    "--last-failed-no-failures",
    "--cache-show",
    "--debug",
}
_SELECTION = {"-k", "--keyword", "-m", "--markexpr", "--deselect"}
_SELECTION_EQ = ("-k=", "--keyword=", "-m=", "--markexpr=", "--deselect=")

_shutdown: int | None = None
_gate_module: object | None = None
_children: list[Running] = []


@dataclass
class Running:
    prefix: str
    proc: subprocess.Popen[str]
    pgid: int


@dataclass(frozen=True)
class Addopts:
    kept: list[str]
    selection: list[str]
    junit: str | None
    cov_reports: list[str]
    cov_config: str | None = None


@dataclass(frozen=True)
class Planned:
    nodeid: str
    kind: str


def _on_signal(signum: int, _frame: object) -> None:
    global _shutdown
    if _shutdown is None:
        _shutdown = signum


def _install_signals() -> None:
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)


def _nap(seconds: float) -> bool:
    """睡一会儿。期间收到信号就返回 False。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if _shutdown is not None:
            return False
        time.sleep(min(0.2, max(0.0, end - time.monotonic())))
    return _shutdown is None


def raise_nice() -> None:
    """把本进程抬到 nice 15。已经不低于 15 就不动。子进程会继承。

    有的启动环境不许改优先级（会报 EPERM）。那时只打印一行，照常跑。
    """
    if not hasattr(os, "getpriority"):
        return
    try:
        if os.getpriority(os.PRIO_PROCESS, 0) < 15:
            os.setpriority(os.PRIO_PROCESS, 0, 15)
    except OSError as exc:
        print(f"没法把本进程调到 nice 15（{exc}），按现在的优先级继续。", flush=True)


def fallback_workers(reason: str) -> int:
    ncpu = os.cpu_count() or 1
    workers = max(1, min(ncpu - 1, FALLBACK_CAP))
    print(f"{reason} 并行进程数改为 {workers}（min(cpu_count-1, {FALLBACK_CAP})，至少 1）。", flush=True)
    return workers


def decide_workers() -> int | None:
    """返回进程数。None 表示收到了信号，0 表示负荷门没放开。"""
    global _gate_module
    path = os.environ.get(LOADGATE_ENV, "").strip()
    if not path:
        return fallback_workers(f"{LOADGATE_ENV} 没有设置。公共仓库不依赖私有的 loadgate.py。")
    module = _gate_module = load_gate(path)
    if module is None:
        return fallback_workers(f"{LOADGATE_ENV} 不可用。")
    for attempt in range(1, 13):
        if _shutdown is not None:
            return None
        workers, gate = module.workers()  # type: ignore[attr-defined]
        print(f"loadgate attempt {attempt} workers={workers} {gate}", flush=True)
        if workers:
            capped = min(int(workers), MAX_SHARDS)
            print(f"并行进程数 {capped}（loadgate.workers()={workers}，上限 {MAX_SHARDS}）。", flush=True)
            return capped
        print("load gate: 这次不新开测试进程，15 秒后再问一次。", flush=True)
        if not _nap(15):
            return None
    print("load gate: 等了约 3 分钟仍然没有空闲，测试没有开始。", flush=True)
    return 0


def _take(args: list[str], index: int) -> str:
    if index + 1 >= len(args):
        raise SystemExit(f"选项缺少参数：{args[index]}")
    return args[index + 1]


def _keep_value(name: str, nxt: str) -> bool:
    if nxt.startswith("-") and name not in {"-W", "--pythonwarnings"}:
        return False
    if name == "--cov" and ("::" in nxt or nxt.endswith(".py") or "/" in nxt):
        return False
    return True


def split_addopts(raw: str) -> Addopts:
    """拆开用户的 PYTEST_ADDOPTS。

    kept 给每个子进程。selection（位置参数、-k、-m、--deselect）只参与一次收集。
    junit 和 --cov-report 留给父进程，按合并结果写最终文件。--basetemp 丢掉，分片各自一套。
    --cov-config 子进程照用，父进程合并和出报告也记下来用同一份。
    """
    args = shlex.split(raw)
    kept: list[str] = []
    selection: list[str] = []
    reports: list[str] = []
    junit: str | None = None
    cov_config: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--cov-config":
            cov_config = _take(args, index)
            kept.extend((arg, cov_config))
            index += 2
            continue
        if arg.startswith("--cov-config="):
            cov_config = arg.split("=", 1)[1]
            kept.append(arg)
            index += 1
            continue
        if arg == "--basetemp" or arg.startswith("--basetemp="):
            index += 2 if arg == "--basetemp" else 1
            continue
        if arg in {"--junitxml", "--junit-xml"}:
            junit = _take(args, index)
            index += 2
            continue
        if arg.startswith("--junitxml=") or arg.startswith("--junit-xml="):
            junit = arg.split("=", 1)[1]
            index += 1
            continue
        if arg == "--cov-report":
            reports.append(_take(args, index))
            index += 2
            continue
        if arg.startswith("--cov-report="):
            reports.append(arg.split("=", 1)[1])
            index += 1
            continue
        if arg.startswith(_SELECTION_EQ):
            selection.append(arg)
            index += 1
            continue
        if arg in _SELECTION:
            selection.extend((arg, _take(args, index)))
            index += 2
            continue
        if arg.startswith("-"):
            kept.append(arg)
            name = arg.split("=", 1)[0]
            if "=" not in arg and name in _TAKES_VALUE and index + 1 < len(args) and _keep_value(name, args[index + 1]):
                kept.append(args[index + 1])
                index += 2
                continue
            index += 1
            continue
        selection.append(arg)
        index += 1
    return Addopts(kept, selection, junit, reports, cov_config)


def collect_ids_main(args: list[str]) -> int:
    import pytest

    class CollectIds:
        def pytest_collection_finish(self, session: pytest.Session) -> None:
            for item in session.items:
                if item.get_closest_marker("exclusive") is not None:
                    kind = "exclusive"
                elif item.get_closest_marker("browser_serial") is not None:
                    kind = "browser"
                else:
                    kind = "normal"
                print(f"NODEID {kind} {item.nodeid}", flush=True)

    return int(pytest.main(args, plugins=[CollectIds()]))


def exec_plan(plan_path: str, pytest_args: list[str]) -> int:
    import pytest

    ids = [line for line in Path(plan_path).read_text(encoding="utf-8").splitlines() if line]
    wanted = set(ids)
    files = {str(Path(node.split("::", 1)[0]).resolve()) for node in ids}

    class Plan:
        def pytest_ignore_collect(self, collection_path: Path, config: pytest.Config) -> bool | None:
            del config
            if collection_path.is_dir() or collection_path.name == "conftest.py" or collection_path.suffix != ".py":
                return None
            try:
                key = str(collection_path.resolve())
            except OSError:
                return None
            if key in files:
                return None
            return True

        def pytest_collection_modifyitems(self, session: pytest.Session, config: pytest.Config, items: list[pytest.Item]) -> None:
            del session
            keep = [item for item in items if item.nodeid in wanted]
            drop = [item for item in items if item.nodeid not in wanted]
            missing = wanted - {item.nodeid for item in keep}
            if missing:
                sample = "\n".join(sorted(missing)[:20])
                raise pytest.UsageError(f"计划里的用例没有收集到：\n{sample}")
            if drop:
                config.hook.pytest_deselected(items=drop)
            items[:] = keep

    plugins: list[object] = [Plan()]
    if BUDGET_ENV in os.environ:
        plugins.append(exclusive_plugin(plan_path))
    return int(pytest.main(pytest_args, plugins=plugins))


def _parse_plan(stdout: str) -> list[Planned]:
    planned: list[Planned] = []
    for line in stdout.splitlines():
        if not line.startswith("NODEID "):
            continue
        kind, nodeid = line[len("NODEID ") :].split(" ", 1)
        if kind not in {"normal", "exclusive", "browser"}:
            raise SystemExit(f"不认识的收集标记：{line}")
        planned.append(Planned(nodeid, kind))
    return planned


def _ps_table() -> dict[int, tuple[int, str, str]]:
    out = subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid=,stat=,comm="], text=True)
    table: dict[int, tuple[int, str, str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, stat, comm = int(parts[0]), int(parts[1]), parts[2], parts[3]
        table[pid] = (ppid, stat, comm)
    return table


def _descendants(table: dict[int, tuple[int, str, str]], roots: list[int]) -> list[int]:
    children: dict[int, list[int]] = {}
    for pid, (ppid, _stat, _comm) in table.items():
        children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    stack = list(roots)
    seen = set(roots)
    while stack:
        current = stack.pop()
        for kid in children.get(current, []):
            if kid not in seen:
                seen.add(kid)
                found.append(kid)
                stack.append(kid)
    return found


def _allowed(comm: str) -> bool:
    name = comm.lower()
    return any(token in name for token in _ALLOWED)


def _comm(pid: int) -> str | None:
    listed = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True, check=False)
    if listed.returncode != 0:
        return None
    comm = listed.stdout.strip()
    return comm or None


def _kill(pid: int, signum: int) -> None:
    try:
        os.kill(pid, signum)
    except OSError:
        pass


def _killpg(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except OSError:
        pass


def _signalable(pid: int, table: dict[int, tuple[int, str, str]], groups: set[int]) -> bool:
    if pid not in table:
        return False
    _ppid, stat, comm = table[pid]
    if stat.startswith("Z") or not _allowed(comm):
        return False
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return False
    return pgid in groups or _allowed(comm)


def _reap() -> None:
    """结束已跟踪的子进程组，以及脱离进程组但仍挂在这棵树上的孙进程。"""
    if not _children:
        return
    roots = [child.proc.pid for child in _children]
    groups = {child.pgid for child in _children}
    table = _ps_table()
    targets = [(pid, table[pid][2]) for pid in _descendants(table, roots) if _signalable(pid, table, groups)]
    for pid, _comm_name in targets:
        _kill(pid, signal.SIGTERM)
    for pgid in groups:
        _killpg(pgid, signal.SIGTERM)
    for pid in roots:
        _kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _GRACE_S
    pending = [pid for pid, _comm_name in targets] + roots
    while time.monotonic() < deadline and any(_comm(pid) for pid in pending):
        time.sleep(0.05)
    for pid, comm in targets:
        if _comm(pid) == comm:
            _kill(pid, signal.SIGKILL)
    for child in _children:
        if _comm(child.proc.pid) and _allowed(_comm(child.proc.pid) or ""):
            try:
                if os.getpgid(child.proc.pid) == child.pgid:
                    _kill(child.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        _killpg(child.pgid, signal.SIGKILL)
        if child.proc.poll() is None:
            try:
                child.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def _read_pipe(stream: object, bucket: list[str]) -> None:
    if stream is None:
        bucket.append("")
        return
    bucket.append(stream.read())  # type: ignore[attr-defined]


def _wait_proc(proc: subprocess.Popen[str]) -> int:
    while True:
        if _shutdown is not None:
            _reap()
            code = proc.poll()
            return 128 + _shutdown if code is None else code
        code = proc.poll()
        if code is not None:
            return code
        time.sleep(0.2)


def _capture(cmd: list[str], env: dict[str, str], prefix: str) -> tuple[int, str, str]:
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    _children.append(Running(prefix, proc, proc.pid))
    if _shutdown is not None:
        _reap()
    out: list[str] = []
    err: list[str] = []
    threads = [
        threading.Thread(target=_read_pipe, args=(proc.stdout, out), daemon=True),
        threading.Thread(target=_read_pipe, args=(proc.stderr, err), daemon=True),
    ]
    for thread in threads:
        thread.start()
    code = _wait_proc(proc)
    for thread in threads:
        thread.join(timeout=5)
    return code, "".join(out), "".join(err)


def _collect(env: dict[str, str]) -> list[Planned]:
    cmd = [sys.executable, "-I", str(Path(__file__).resolve()), "--collect-ids", "--collect-only", "-p", "no:cacheprovider"]
    code, stdout, stderr = _capture(cmd, env, "collect")
    if _shutdown is not None:
        return []
    if stderr.strip():
        print(stderr, file=sys.stderr, end="" if stderr.endswith("\n") else "\n", flush=True)
    planned = _parse_plan(stdout)
    if code != 0:
        sys.stdout.write(stdout)
        raise SystemExit(code)
    return planned


def _partition(planned: list[Planned]) -> tuple[list[str], list[str], list[str]]:
    node_ids = [item.nodeid for item in planned]
    if len(node_ids) != len(set(node_ids)):
        print("收集到了重复的 node id。", flush=True)
        raise SystemExit(2)
    exclusive = [item.nodeid for item in planned if item.kind == "exclusive"]
    browser = [item.nodeid for item in planned if item.kind == "browser"]
    normal = [item.nodeid for item in planned if item.kind == "normal"]
    print(
        f"collected {len(planned)}; exclusive {len(exclusive)}; browser {len(browser)}; parallel {len(normal)}; node-id sets match",
        flush=True,
    )
    return normal, browser, exclusive


def _shards(node_ids: list[str], workers: int) -> list[list[str]]:
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
    planned = [(cmd, size) for cmd, size in zip(commands, sizes, strict=True) if cmd]
    counts = ", ".join(str(size) for _cmd, size in planned)
    print(f"非 exclusive 测试 {sum(sizes)} 个，分到 {len(planned)} 个进程：{counts}。", flush=True)
    return [cmd for cmd, _size in planned]


def _child_env(base: dict[str, str], directory: Path, coverage_file: Path) -> dict[str, str]:
    env = dict(base)
    directory.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(directory)
    env["TMP"] = str(directory)
    env["TEMP"] = str(directory)
    env["COVERAGE_FILE"] = str(coverage_file)
    env["PYTHONPYCACHEPREFIX"] = str(directory / "pycache")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _files_of(node_ids: list[str]) -> list[str]:
    seen: list[str] = []
    known: set[str] = set()
    for node_id in node_ids:
        file_part = node_id.split("::", 1)[0]
        if file_part not in known:
            known.add(file_part)
            seen.append(file_part)
    return seen


def _stage_cmd(plan: Path, extra: list[str], files: list[str]) -> list[str]:
    pytest_args = ["-q", "--durations=20", "--reruns", "1", "--cov=gxr_studio", "--cov-report=", "--cov-fail-under=0", *extra, *files]
    return [sys.executable, "-I", str(Path(__file__).resolve()), "--exec-plan", str(plan), "--", *pytest_args]


def _spawn(prefix: str, cmd: list[str], env: dict[str, str], log_path: Path) -> Running:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    running = Running(prefix, proc, proc.pid)
    _children.append(running)

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            log.flush()
            print(f"[{prefix}] {line}", end="", flush=True)
        log.close()

    threading.Thread(target=pump, name=f"pump-{prefix}", daemon=True).start()
    if _shutdown is not None:
        _reap()
    return running


def _wait_all(jobs: list[Running]) -> list[int]:
    started = time.monotonic()
    last_note = started
    codes: dict[int, int] = {}
    while len(codes) < len(jobs):
        if _shutdown is not None:
            _reap()
            break
        for job in jobs:
            if job.proc.pid in codes:
                continue
            code = job.proc.poll()
            if code is not None:
                codes[job.proc.pid] = code
                print(f"[{job.prefix}] exit {code}", flush=True)
        if len(codes) == len(jobs):
            break
        now = time.monotonic()
        if now - last_note >= 30:
            running = [job.prefix for job in jobs if job.proc.pid not in codes]
            print(f"仍在跑：{', '.join(running)}（已 {int(now - started)} 秒）", flush=True)
            last_note = now
        time.sleep(0.2)
    return [codes.get(job.proc.pid, 1) for job in jobs]


def _write_plan(path: Path, node_ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{node_id}\n" for node_id in node_ids), encoding="utf-8")


def _start_stage(
    prefix: str,
    work: Path,
    node_ids: list[str],
    env: dict[str, str],
    junit_parts: list[Path],
    coverage_files: list[Path],
    lock: list[str] | None = None,
    extra: tuple[str, ...] = (),
) -> Running:
    slot = work / prefix
    junit = slot / "junit.xml"
    coverage = slot / ".coverage"
    plan = slot / "ids.txt"
    _write_plan(plan, node_ids)
    junit_parts.append(junit)
    coverage_files.append(coverage)
    cmd = _stage_cmd(plan, ["--basetemp", str(slot / "base"), "--junitxml", str(junit), *extra], _files_of(node_ids))
    if lock:
        cmd = [*lock, *cmd]
    return _spawn(prefix, cmd, _child_env(env, slot / "tmp", coverage), slot / "pytest.log")


def _stopped() -> int | None:
    if _shutdown is None:
        return None
    return 128 + _shutdown


def _launch_ids(
    prefix: str,
    work: Path,
    node_ids: list[str],
    env: dict[str, str],
    junit_parts: list[Path],
    coverage_files: list[Path],
    lock: list[str] | None = None,
    extra: tuple[str, ...] = (),
) -> int:
    job = _start_stage(prefix, work, node_ids, env, junit_parts, coverage_files, lock, extra)
    return _wait_all([job])[0]


def _exclusive(work: Path, ids: list[str], env: dict[str, str], junit: list[Path], cov: list[Path], lock: list[str]) -> int:
    """先等安静再拿锁跑；只有速度预算没过的用例安静后重跑一次（见 run_tests_quiet.run_exclusive）。"""

    def launch(prefix: str, node_ids: list[str], budget: float, extra: tuple[str, ...]) -> int:
        return _launch_ids(prefix, work, node_ids, {**env, BUDGET_ENV: str(budget)}, junit, cov, lock, extra)

    print(f"exclusive 阶段：lockf -t 3600 -k {LOCK_PATH}，{len(ids)} 个用例。", flush=True)
    return run_exclusive(make_gate(_gate_module), work, ids, launch, _nap, _stopped)


def _run(parsed: Addopts) -> int:
    workers = decide_workers()
    if workers is None:
        return 128 + int(_shutdown)
    if workers == 0:
        return 76
    os.chdir(ROOT)
    child_env = dict(os.environ)
    child_env.pop(BUDGET_ENV, None)
    child_env["PYTEST_ADDOPTS"] = shlex.join(parsed.kept)
    collect_env = dict(child_env)
    collect_env["PYTEST_ADDOPTS"] = shlex.join([*parsed.kept, *parsed.selection])
    normal, browser, exclusive = _partition(_collect(collect_env))
    if (stopped := _stopped()) is not None:
        return stopped
    lock: list[str] | None = None
    if exclusive:
        found = shutil.which("lockf")
        if found is None:
            print("找不到 lockf，exclusive 测试不能在没有锁的情况下跑。", flush=True)
            return 127
        lock = [found, "-t", "3600", "-k", LOCK_PATH]
    work = Path(tempfile.mkdtemp(prefix="gxr-run-tests-"))
    print(f"分片临时目录 {work}", flush=True)
    junit_parts: list[Path] = []
    coverage_files: list[Path] = []
    codes: list[int] = []
    if normal:
        jobs = [
            _start_stage(f"shard-{index}", work, ids, child_env, junit_parts, coverage_files)
            for index, ids in enumerate(_shards(normal, workers))
        ]
        codes.extend(_wait_all(jobs))
    else:
        print("没有普通测试。", flush=True)
    if (stopped := _stopped()) is not None:
        return stopped
    if browser:
        print(f"浏览器阶段：串行，不拿锁，{len(browser)} 个用例。", flush=True)
        codes.append(_launch_ids("browser", work, browser, child_env, junit_parts, coverage_files))
    else:
        print("没有浏览器阶段。", flush=True)
    if (stopped := _stopped()) is not None:
        return stopped
    if exclusive:
        assert lock is not None
        codes.append(_exclusive(work, exclusive, child_env, junit_parts, coverage_files, lock))
    else:
        print("没有 exclusive 测试。", flush=True)
    if (stopped := _stopped()) is not None:
        return stopped
    if parsed.junit:
        merge_junit(junit_parts, Path(parsed.junit))
    status = next((code for code in codes if code != 0), 0)
    # --cov-config 原样转交：字面量 .coveragerc 是 coverage 的默认查找，*.toml 靠文件名认格式，
    # 解析成绝对路径或链接目标都会改掉这两条。父子进程的 cwd 都是 ROOT，相对路径指向同一个文件。
    coverage_status = combine_coverage(coverage_files, parsed.cov_reports, parsed.cov_config)
    if (stopped := _stopped()) is not None:
        return stopped
    if status != 0:
        return status
    return coverage_status


def main() -> int:
    _install_signals()
    raise_nice()
    status = 1
    try:
        status = _run(split_addopts(os.environ.get("PYTEST_ADDOPTS", "")))
    finally:
        _reap()
    if (stopped := _stopped()) is not None:
        return stopped
    return status


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--collect-ids":
        sys.exit(collect_ids_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--exec-plan":
        if len(sys.argv) < 4 or sys.argv[3] != "--":
            print("用法：run_tests.py --exec-plan IDS -- pytest 参数", file=sys.stderr)
            sys.exit(2)
        sys.exit(exec_plan(sys.argv[2], sys.argv[4:]))
    sys.exit(main())
