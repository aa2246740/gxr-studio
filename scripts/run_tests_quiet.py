#!/usr/bin/env python3
"""exclusive 阶段：先等机器安静再量墙钟，速度预算没过的用例安静后重跑一次。

速度预算本身不放宽。这里只管量的时候机器够不够安静：

- 安静的判断：环境变量 GXR_LOADGATE 指向的 loadgate.py 可用时，用它的 `quiet_for_timing()`
  （没有这个函数就用 `cap(0)` 的 admit）；没设置、文件不在、加载失败或调用出错时，改用公开的回退：
  1 分钟负载均值低于 CPU 数的一半。公共仓库不依赖那条私有路径。
- 每 GXR_QUIET_POLL_S 秒（默认 10）问一次。一次 runner 里所有等待合计最多 GXR_QUIET_MAX_WAIT_S 秒
  （默认 1800）。用完以后照样跑，每次只再问一次，并打印一行「一直没安静下来」和当时的负载。
- 每次等待打印一行：等了多久、问了几次、最后看到的负载。
- 速度断言抛 `SpeedBudgetExceeded`（类上 `gxr_speed_budget = True`，见 tests/support/speed.py）。
  exclusive 用例最后一次尝试只因它失败（setup、teardown 都没出错）时，记成 speed，父进程等安静后在
  exclusive 阶段把这些用例重跑一次，以第二次为准。别的失败一律不重跑。不解析断言文字。
  这是和 pytest-rerunfailures 分开的机制，`--force-reruns=0` 时照样生效。
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

LOADGATE_ENV = "GXR_LOADGATE"
MAX_WAIT_ENV = "GXR_QUIET_MAX_WAIT_S"
POLL_ENV = "GXR_QUIET_POLL_S"
BUDGET_ENV = "GXR_RUN_TESTS_QUIET_BUDGET"
DEFAULT_MAX_WAIT_S = 1800.0
DEFAULT_POLL_S = 10.0

Probe = Callable[[], tuple[bool, str]]
Nap = Callable[[float], bool]


def load_gate(path: str) -> Any | None:
    source = Path(path)
    if not source.is_file():
        print(f"{LOADGATE_ENV} 指向的文件不存在：{path}。", flush=True)
        return None
    try:
        spec = importlib.util.spec_from_file_location("gxr_loadgate", source)
        if spec is None or spec.loader is None:
            print(f"{LOADGATE_ENV} 无法加载：{path}。", flush=True)
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:
        print(f"{LOADGATE_ENV} 加载失败（{exc}）。", flush=True)
        return None
    return module


def _seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"{name}={raw!r} 不是数字，按 {default:g} 秒。", flush=True)
        return default
    return max(0.0, value)


def _describe(info: object) -> str:
    if not isinstance(info, dict):
        return repr(info)[:200]
    keys = ("idle_cores", "ncpu", "load1", "mem_free", "mem_pressure", "spare")
    picked = {key: info[key] for key in keys if key in info}
    return json.dumps(picked or info, ensure_ascii=False, default=str)[:200]


def loadavg_probe(getloadavg: Callable[[], tuple[float, float, float]] = os.getloadavg, ncpu: int | None = None) -> Probe:
    cpus = ncpu or os.cpu_count() or 1
    limit = cpus / 2

    def probe() -> tuple[bool, str]:
        load1 = float(getloadavg()[0])
        return load1 < limit, f"公开回退 load1={load1:.2f}，要 < {limit:g}（cpu {cpus}）"

    return probe


def gate_probe(module: Any) -> Probe | None:
    """loadgate 的安静判断。模块里两样都没有时返回 None。"""
    quiet = getattr(module, "quiet_for_timing", None)
    if callable(quiet):

        def by_quiet() -> tuple[bool, str]:
            ok, info = quiet()
            return bool(ok), f"loadgate.quiet_for_timing {_describe(info)}"

        return by_quiet
    cap = getattr(module, "cap", None)
    if callable(cap):

        def by_cap() -> tuple[bool, str]:
            _cap, info = cap(0)
            return bool(info.get("admit")), f"loadgate.cap admit {_describe(info)}"

        return by_cap
    return None


class QuietGate:
    """一次 runner 里共用的安静门。等待时间合计不超过 budget 秒。"""

    def __init__(self, primary: Probe | None, fallback: Probe, *, budget: float, poll: float) -> None:
        self.primary = primary
        self.fallback = fallback
        self.remaining = budget
        self.poll = poll

    def check(self) -> tuple[bool, str]:
        if self.primary is not None:
            try:
                return self.primary()
            except Exception as exc:
                print(f"{LOADGATE_ENV} 的安静判断出错（{exc!r}），这次 runner 改用公开回退。", flush=True)
                self.primary = None
        return self.fallback()

    def wait(self, label: str, nap: Nap, clock: Callable[[], float] = time.monotonic) -> bool:
        """等到安静或预算用完。返回 False 表示等的时候收到了信号。"""
        started = clock()
        checks = 0
        while True:
            checks += 1
            quiet, seen = self.check()
            waited = clock() - started
            if quiet:
                self.remaining = max(0.0, self.remaining - waited)
                print(f"等机器安静（{label}）：等了 {waited:.0f} 秒，问了 {checks} 次，安静了，{seen}。", flush=True)
                return True
            if waited + self.poll > self.remaining:
                self.remaining = max(0.0, self.remaining - waited)
                print(
                    f"机器一直没安静下来（{label}）：等了 {waited:.0f} 秒，问了 {checks} 次，最后一次 {seen}；"
                    f"等待预算用完（合计 {MAX_WAIT_ENV}），照样跑，墙钟结果可能偏慢。",
                    flush=True,
                )
                return True
            if not nap(self.poll):
                self.remaining = max(0.0, self.remaining - (clock() - started))
                return False


def env_gate() -> Any | None:
    path = os.environ.get(LOADGATE_ENV, "").strip()
    return load_gate(path) if path else None


def make_gate(module: Any | None, *, budget: float | None = None) -> QuietGate:
    """module 是已经加载的 loadgate，None 表示没有，用公开回退。budget 默认读 GXR_QUIET_MAX_WAIT_S。"""
    primary = gate_probe(module) if module is not None else None
    if module is not None and primary is None:
        print(f"{LOADGATE_ENV} 里没有 quiet_for_timing 或 cap，安静判断改用公开回退。", flush=True)
    if budget is None:
        budget = _seconds(MAX_WAIT_ENV, DEFAULT_MAX_WAIT_S)
    return QuietGate(primary, loadavg_probe(), budget=budget, poll=_seconds(POLL_ENV, DEFAULT_POLL_S) or DEFAULT_POLL_S)


def _sleep(seconds: float) -> bool:
    time.sleep(seconds)
    return True


def _is_speed(kind: type[BaseException]) -> bool:
    return getattr(kind, "gxr_speed_budget", False) is True


class ExclusivePlugin:
    """exclusive 子进程里：每个用例之前等安静；结束时按 node id 写下 passed / speed / failed。"""

    def __init__(self, outcomes: Path, gate: QuietGate | None) -> None:
        self.outcomes = outcomes
        self.gate = gate
        self.state: dict[str, str] = {}
        self.budget = gate.remaining if gate is not None else 0.0

    @pytest.hookimpl(tryfirst=True)  # 等待排在 rerunfailures 的 protocol 之前
    def pytest_runtest_protocol(self, item: Any, nextitem: Any) -> None:
        del nextitem
        if self.gate is not None:
            self.gate.wait(f"exclusive 用例之前：{item.nodeid}", _sleep)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item: Any, call: Any) -> Any:
        del item
        report = yield
        if call.excinfo is not None and report.failed and _is_speed(call.excinfo.type):
            report.gxr_speed_budget = True
        return report

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.when == "setup":
            self.state[report.nodeid] = "passed"
        if report.skipped and self.state.get(report.nodeid) == "passed":
            self.state[report.nodeid] = "skipped"
        if not report.failed:
            return
        speed = report.when == "call" and getattr(report, "gxr_speed_budget", False)
        current = self.state.get(report.nodeid, "passed")
        self.state[report.nodeid] = "speed" if speed and current == "passed" else "failed"

    def pytest_sessionfinish(self) -> None:
        used = self.budget - self.gate.remaining if self.gate is not None else 0.0
        data = {"outcomes": self.state, "quiet_waited_s": max(0.0, used)}
        self.outcomes.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def exclusive_plugin(plan_path: str) -> ExclusivePlugin:
    budget = os.environ.get(BUDGET_ENV)
    gate = make_gate(env_gate(), budget=_seconds(BUDGET_ENV, 0.0)) if budget is not None else None
    return ExclusivePlugin(Path(plan_path).with_suffix(".outcomes.json"), gate)


@dataclass(frozen=True)
class StageResult:
    outcomes: dict[str, str]
    waited: float


def read_outcomes(plan: Path) -> StageResult | None:
    path = plan.with_suffix(".outcomes.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        outcomes = {str(k): str(v) for k, v in data["outcomes"].items()}
        return StageResult(outcomes, float(data.get("quiet_waited_s", 0.0)))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def speed_only(code: int, result: StageResult | None, node_ids: list[str]) -> list[str]:
    """第一次 exclusive 里只因速度预算失败的用例，安静后要重跑的 node id。

    退出码不是「有用例失败」(1)、或者计划里有用例没跑到（比如 -x 提前停了）时返回空，不重跑。
    别的用例的普通失败不重跑，也不影响这里：它们照样让这一阶段失败。
    """
    if code != 1 or result is None or set(result.outcomes) != set(node_ids):
        return []
    return [nodeid for nodeid, value in result.outcomes.items() if value == "speed"]


def _case_key(nodeid: str) -> tuple[str, str]:
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = names[0].replace("/", ".").removesuffix(".py")
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


def drop_junit_cases(path: Path, nodeids: list[str]) -> int:
    """从第一次 exclusive 的 JUnit 里拿掉要重跑的用例（以第二次为准），同步改 suite 的计数。返回拿掉几个。"""
    if not path.is_file() or path.stat().st_size == 0:
        return 0
    keys = [_case_key(nodeid) for nodeid in nodeids]
    tree = ET.parse(path)
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    dropped = 0
    for suite in suites:
        for case in list(suite.findall("testcase")):
            classname, name = case.attrib.get("classname", ""), case.attrib.get("name", "")
            if not any(name == key_name and (classname == key_cls or classname.endswith("." + key_cls)) for key_cls, key_name in keys):
                continue
            for tag, attr in (("failure", "failures"), ("error", "errors"), ("skipped", "skipped")):
                if case.find(tag) is not None:
                    suite.set(attr, str(max(0, int(suite.attrib.get(attr, "0")) - 1)))
            suite.set("tests", str(max(0, int(suite.attrib.get("tests", "0")) - 1)))
            suite.remove(case)
            dropped += 1
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return dropped


def report_retry(first: StageResult, second: StageResult | None, retried: list[str]) -> None:
    for nodeid in retried:
        again = second.outcomes.get(nodeid, "没有结果") if second is not None else "没有结果"
        verdict = "通过" if again == "passed" else f"仍然失败（{again}）"
        print(f"速度预算重试：{nodeid} 第一次只有墙钟预算没过（{first.outcomes[nodeid]}），安静后第二次{verdict}，以第二次为准。", flush=True)


Launch = Callable[[str, list[str], float, tuple[str, ...]], int]


def run_exclusive(gate: QuietGate, work: Path, node_ids: list[str], launch: Launch, nap: Nap, stopped: Callable[[], int | None]) -> int:
    """exclusive 阶段：等安静，跑一次；只有速度预算没过时，再等安静、把那几个用例重跑一次，返回第二次的结果。"""
    if not gate.wait("exclusive 阶段开始前", nap):
        return stopped() or 143
    first = launch("exclusive", node_ids, gate.remaining, ())
    result = read_outcomes(work / "exclusive" / "ids.txt")
    retry = speed_only(first, result, node_ids)
    if not retry or result is None or stopped() is not None:
        return first
    gate.remaining = max(0.0, gate.remaining - result.waited)
    print(f"exclusive 里 {len(retry)} 个用例只有墙钟预算没过，等机器安静后在 exclusive 阶段重跑一次：{', '.join(retry)}", flush=True)
    if not gate.wait("速度预算重试之前", nap):
        return stopped() or 143
    drop_junit_cases(work / "exclusive" / "junit.xml", retry)
    code = launch("exclusive-retry", retry, gate.remaining, ("--force-reruns", "0"))
    report_retry(result, read_outcomes(work / "exclusive-retry" / "ids.txt"), retry)
    if "failed" in result.outcomes.values():
        print("exclusive 第一次里还有不是墙钟预算的失败，不重跑，这一阶段照样失败。", flush=True)
        return first
    return code
