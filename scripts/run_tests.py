#!/usr/bin/env python3
"""把不依赖墙钟速度的测试分片并行跑，再单独跑 exclusive。

进程数来自环境变量 GXR_LOADGATE 指向的 loadgate.py（workers()，最多 6）。
没设置或文件不存在时，改用 min(cpu_count - 1, 4)，至少 1。公共仓库不写死私有路径。
exclusive 测试串行，并且用 lockf 拿 /tmp/gxr-heavy.lock。覆盖率最后合并，门槛 85。
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOADGATE_ENV = "GXR_LOADGATE"
LOCK_PATH = "/tmp/gxr-heavy.lock"
FAIL_UNDER = "85"
MAX_SHARDS = 6
FALLBACK_CAP = 4


def raise_nice() -> None:
    """把本进程抬到 nice 15。已经不低于 15 就不动。子进程会继承。"""
    if not hasattr(os, "nice"):
        return
    current = os.nice(0)
    if current < 15:
        os.nice(15 - current)


def fallback_workers(reason: str) -> int:
    ncpu = os.cpu_count() or 1
    workers = max(1, min(ncpu - 1, FALLBACK_CAP))
    print(f"{reason} 并行进程数改为 {workers}（min(cpu_count-1, {FALLBACK_CAP})，至少 1）。", flush=True)
    return workers


def decide_workers() -> int:
    path = os.environ.get(LOADGATE_ENV, "").strip()
    if not path:
        return fallback_workers(f"{LOADGATE_ENV} 没有设置。公共仓库不依赖私有的 loadgate.py。")
    source = Path(path)
    if not source.is_file():
        return fallback_workers(f"{LOADGATE_ENV} 指向的文件不存在：{path}。")
    try:
        spec = importlib.util.spec_from_file_location("gxr_loadgate", source)
        if spec is None or spec.loader is None:
            return fallback_workers(f"{LOADGATE_ENV} 无法加载：{path}。")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:
        return fallback_workers(f"{LOADGATE_ENV} 加载失败（{exc}）。")
    for attempt in range(1, 13):
        workers, gate = module.workers()
        print(f"loadgate attempt {attempt} workers={workers} {gate}", flush=True)
        if workers:
            capped = min(int(workers), MAX_SHARDS)
            print(f"并行进程数 {capped}（loadgate.workers()={workers}，上限 {MAX_SHARDS}）。", flush=True)
            return capped
        print("load gate: 这次不新开测试进程，15 秒后再问一次。", flush=True)
        time.sleep(15)
    print("load gate: 等了约 3 分钟仍然没有空闲，测试没有开始。", flush=True)
    return 0


def split_addopts(raw: str) -> tuple[list[str], str | None]:
    """去掉会让分片撞车的 --basetemp / --junitxml，留下其余选项（含 --force-reruns）。"""
    kept: list[str] = []
    junit: str | None = None
    args = shlex.split(raw)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--basetemp" and index + 1 < len(args):
            index += 2
            continue
        if arg.startswith("--basetemp="):
            index += 1
            continue
        if arg == "--junitxml" and index + 1 < len(args):
            junit = args[index + 1]
            index += 2
            continue
        if arg.startswith("--junitxml="):
            junit = arg.split("=", 1)[1]
            index += 1
            continue
        kept.append(arg)
        index += 1
    return kept, junit


def collect_ids_main(args: list[str]) -> int:
    import pytest

    class CollectIds:
        def pytest_collection_finish(self, session: pytest.Session) -> None:
            for item in session.items:
                print("NODEID " + item.nodeid, flush=True)

    return int(pytest.main(args, plugins=[CollectIds()]))


def collect(env: dict[str, str], extra: list[str]) -> list[str]:
    cmd = [
        sys.executable,
        "-I",
        str(Path(__file__).resolve()),
        "--collect-ids",
        "--collect-only",
        "-p",
        "no:cacheprovider",
        *extra,
    ]
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)  # noqa: S603
    ids = [line[len("NODEID ") :] for line in proc.stdout.splitlines() if line.startswith("NODEID ")]
    if proc.returncode != 0:
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(proc.returncode)
    if proc.stderr.strip():
        print(proc.stderr, file=sys.stderr, end="" if proc.stderr.endswith("\n") else "\n", flush=True)
    return ids


def shard_args(node_ids: list[str], exclusive: set[str], workers: int) -> list[list[str]]:
    """同一个文件的用例进同一个进程。文件里没有 exclusive 时只传文件名，避免命令行超长。"""
    grouped: dict[str, list[str]] = {}
    for node_id in node_ids:
        grouped.setdefault(node_id.split("::", 1)[0], []).append(node_id)
    commands: list[list[str]] = [[] for _ in range(workers)]
    sizes = [0 for _ in range(workers)]
    for path in sorted(grouped):
        slot = sizes.index(min(sizes))
        items = sorted(grouped[path])
        sizes[slot] += len(items)
        prefix = path + "::"
        if any(node_id.startswith(prefix) for node_id in exclusive):
            commands[slot].extend(items)
        else:
            commands[slot].append(path)
    planned = [(cmd, size) for cmd, size in zip(commands, sizes, strict=True) if cmd]
    counts = ", ".join(str(size) for _cmd, size in planned)
    print(f"非 exclusive 测试 {sum(sizes)} 个，分到 {len(planned)} 个进程：{counts}。", flush=True)
    return [cmd for cmd, _size in planned]


def child_env(base: dict[str, str], directory: Path, coverage_file: Path) -> dict[str, str]:
    env = dict(base)
    directory.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(directory)
    env["TMP"] = str(directory)
    env["TEMP"] = str(directory)
    env["COVERAGE_FILE"] = str(coverage_file)
    env["PYTHONPYCACHEPREFIX"] = str(directory / "pycache")
    return env


def pytest_run(extra: list[str]) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-m",
        "pytest",
        "-q",
        "--durations=20",
        "--reruns",
        "1",
        "--cov=gxr_studio",
        "--cov-report=",
        *extra,
    ]


@dataclass
class Running:
    prefix: str
    proc: subprocess.Popen[str]


def stream(prefix: str, cmd: list[str], env: dict[str, str], log_path: Path) -> Running:
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)  # noqa: S603

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            log.flush()
            print(f"[{prefix}] {line}", end="", flush=True)
        log.close()

    threading.Thread(target=pump, name=f"pump-{prefix}", daemon=True).start()
    return Running(prefix, proc)


def wait_all(jobs: list[Running]) -> list[int]:
    started = time.monotonic()
    last_note = started
    codes: dict[int, int] = {}
    while len(codes) < len(jobs):
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
        time.sleep(5)
    return [codes[job.proc.pid] for job in jobs]


def stop(jobs: list[Running]) -> None:
    for job in jobs:
        if job.proc.poll() is None:
            job.proc.terminate()
    for job in jobs:
        if job.proc.poll() is None:
            try:
                job.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                job.proc.kill()


def merge_junit(paths: list[Path], dest: Path) -> None:
    tests = failures = errors = skipped = 0
    total_time = 0.0
    cases: list[ET.Element] = []
    for path in paths:
        if not path.is_file() or path.stat().st_size == 0:
            continue
        root = ET.parse(path).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        for suite in suites:
            tests += int(suite.attrib.get("tests", "0"))
            failures += int(suite.attrib.get("failures", "0"))
            errors += int(suite.attrib.get("errors", "0"))
            skipped += int(suite.attrib.get("skipped", "0"))
            total_time += float(suite.attrib.get("time", "0") or 0)
            for case in suite.findall("testcase"):
                cases.append(ET.fromstring(ET.tostring(case)))
    suite = ET.Element(
        "testsuite",
        {
            "name": "pytest",
            "tests": str(tests),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
            "time": f"{total_time:.3f}",
        },
    )
    for case in cases:
        suite.append(case)
    report = ET.Element("testsuites")
    report.append(suite)
    dest.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(report).write(dest, encoding="utf-8", xml_declaration=True)
    print(
        f"junit {dest} tests={tests} failures={failures} errors={errors} skipped={skipped}",
        flush=True,
    )


def combine_coverage(data_files: list[Path]) -> int:
    present = [path for path in data_files if path.is_file() and path.stat().st_size > 0]
    if not present:
        print("没有覆盖率数据，无法核对 85% 门槛。", flush=True)
        return 1
    combined = present[0].parent / ".coverage.combined"
    combine = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-m", "coverage", "combine", "--keep", "--data-file", str(combined), *[str(path) for path in present]],
        cwd=ROOT,
    )
    if combine.returncode != 0:
        return combine.returncode
    report = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-m", "coverage", "report", "--data-file", str(combined), f"--fail-under={FAIL_UNDER}", "--show-missing"],
        cwd=ROOT,
    )
    return report.returncode


def check_ids(all_ids: list[str], exclusive_ids: list[str]) -> list[str]:
    everything = set(all_ids)
    exclusive = set(exclusive_ids)
    missing = sorted(exclusive - everything)
    if missing:
        print("exclusive 收集到了全集里没有的用例：", flush=True)
        print("\n".join(missing), flush=True)
        raise SystemExit(2)
    if len(all_ids) != len(everything) or len(exclusive_ids) != len(exclusive):
        print("收集到了重复的 node id。", flush=True)
        raise SystemExit(2)
    rest = [node_id for node_id in all_ids if node_id not in exclusive]
    print(
        f"collected {len(all_ids)}; exclusive {len(exclusive_ids)}; parallel {len(rest)}; node-id sets match",
        flush=True,
    )
    return rest


def main() -> int:
    raise_nice()
    workers = decide_workers()
    if workers == 0:
        return 76
    os.chdir(ROOT)
    kept, junit_path = split_addopts(os.environ.get("PYTEST_ADDOPTS", ""))
    base = dict(os.environ)
    base["PYTEST_ADDOPTS"] = shlex.join(kept)
    all_ids = collect(base, [])
    exclusive_ids = collect(base, ["-m", "exclusive"])
    rest = check_ids(all_ids, exclusive_ids)
    lockf = shutil.which("lockf")
    if exclusive_ids and lockf is None:
        print("找不到 lockf，exclusive 测试不能在没有锁的情况下跑。", flush=True)
        return 127
    work = Path(tempfile.mkdtemp(prefix="gxr-run-tests-"))
    print(f"分片临时目录 {work}", flush=True)
    junit_parts: list[Path] = []
    coverage_files: list[Path] = []
    jobs: list[Running] = []
    try:
        for index, args in enumerate(shard_args(rest, set(exclusive_ids), workers)):
            slot = work / f"shard-{index}"
            junit = slot / "junit.xml"
            coverage = slot / ".coverage"
            junit_parts.append(junit)
            coverage_files.append(coverage)
            cmd = pytest_run(["--basetemp", str(slot / "base"), "--junitxml", str(junit), *args])
            jobs.append(stream(f"shard-{index}", cmd, child_env(base, slot / "tmp", coverage), slot / "pytest.log"))
        codes = wait_all(jobs) if jobs else []
        if exclusive_ids:
            slot = work / "exclusive"
            junit = slot / "junit.xml"
            coverage = slot / ".coverage"
            junit_parts.append(junit)
            coverage_files.append(coverage)
            inner = pytest_run(["-m", "exclusive", "--basetemp", str(slot / "base"), "--junitxml", str(junit)])
            print(f"exclusive 阶段：lockf -t 3600 -k {LOCK_PATH}，{len(exclusive_ids)} 个用例。", flush=True)
            cmd = [lockf, "-t", "3600", "-k", LOCK_PATH, *inner]
            job = stream("exclusive", cmd, child_env(base, slot / "tmp", coverage), slot / "pytest.log")
            jobs.append(job)
            codes.append(wait_all([job])[0])
        else:
            print("没有 exclusive 测试。", flush=True)
        if junit_path:
            merge_junit(junit_parts, Path(junit_path))
        status = next((code for code in codes if code != 0), 0)
        coverage_status = combine_coverage(coverage_files)
        if status != 0:
            return status
        return coverage_status
    finally:
        stop(jobs)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--collect-ids":
        sys.exit(collect_ids_main(sys.argv[2:]))
    sys.exit(main())
