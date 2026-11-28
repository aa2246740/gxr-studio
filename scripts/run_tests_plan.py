#!/usr/bin/env python3
"""run_tests.py 的分片计划和「失败已成定局就停」。

三个环境变量都可以不设，只有私有的合并检查外壳会设，公共仓库不依赖它们：
- GXR_TEST_TIMES_JUNIT：上一次通过的 JUnit 文件。按里面每个测试文件的用时分片，数据里没有的文件按
  用例数 × 每个用例用时的中位数估计；文件长的先放，放进当前合计最小的分片。没设、读不了或没有用例时，
  照原来按用例数分。浏览器分道只算计划进浏览器阶段的那些用例自己的用时（同一文件里的非浏览器用例不算），
  数据里没有的用例按中位数估计。
- GXR_STOP_ON_FAILURE=1：计算分片和浏览器阶段的子进程带 -x（重跑之后仍失败才算），一个分片失败就停掉
  其他分片，后面的阶段不再开始。exclusive 阶段从不带 -x，它的速度重试要看到每个用例的结果。
- GXR_CHANGED_TESTS：用 os.pathsep 隔开的测试文件路径。这些文件在各自的分片里、在浏览器阶段里先跑，
  其余的顺序不变。
- GXR_BROWSER_LANES=2：浏览器阶段分 min(2, 计算分片的进程数) 道并行，按同样的用时分道，一个文件的用例
  留在同一道里。标了 browser_alone 的用例等各道都跑完，再单独串行跑。没设或 1 时照原来一道串行；
  别的值打印一行，按一道跑。
"""

from __future__ import annotations

import os
import statistics
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

TIMES_ENV = "GXR_TEST_TIMES_JUNIT"
STOP_ENV = "GXR_STOP_ON_FAILURE"
CHANGED_ENV = "GXR_CHANGED_TESTS"
LANES_ENV = "GXR_BROWSER_LANES"


def _file_of(node_id: str) -> str:
    return node_id.split("::", 1)[0]


def _module_key(file_part: str) -> str:
    """pytest 的 JUnit classname 的模块部分：tests/test_a.py -> tests.test_a。"""
    return file_part.removesuffix(".py").replace("/", ".")


def _case_key(node_id: str) -> tuple[str, str]:
    """node id 在 JUnit 里的 (classname, name)：tests/a.py::K::t[x] -> (tests.a.K, t[x])。参数里的 :: 不拆。"""
    path, bracket, params = node_id.partition("[")
    names = path.split("::")
    names[0] = _module_key(names[0])
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


def _junit_cases(path: str) -> list[tuple[str, str, float]] | str:
    """JUnit 里每个 testcase 的 (classname, name, 秒)。读不了或没有用例时返回原因。"""
    try:
        root = ET.parse(path).getroot()  # noqa: S314 - 本机上一次检查自己写的 JUnit
    except (OSError, ET.ParseError) as exc:
        return f"读不了（{type(exc).__name__}）"
    cases: list[tuple[str, float]] = []
    for case in root.iter("testcase"):
        try:
            seconds = float(case.attrib.get("time", "0") or 0)
        except ValueError:
            continue
        cases.append((case.attrib.get("classname", ""), case.attrib.get("name", ""), max(0.0, seconds)))
    return cases or "里面没有用例"


def _match(classname: str, keys: Mapping[str, str]) -> str | None:
    """classname 对应的测试文件：取最长的、能对上的模块前缀（后面可能还跟着类名）。"""
    parts = classname.split(".")
    for end in range(len(parts), 0, -1):
        found = keys.get(".".join(parts[:end]))
        if found is not None:
            return found
    return None


def file_weights(grouped: Mapping[str, list[str]], env: Mapping[str, str]) -> dict[str, float] | None:
    """每个测试文件的预计秒数。None 表示回退到按用例数分片。"""
    path = env.get(TIMES_ENV, "").strip()
    if not path:
        print(f"{TIMES_ENV} 没有设置，按用例数分片。", flush=True)
        return None
    cases = _junit_cases(path)
    if isinstance(cases, str):
        print(f"{TIMES_ENV} {cases}，按用例数分片。", flush=True)
        return None
    keys = {_module_key(file_part): file_part for file_part in grouped}
    seconds: dict[str, float] = {}
    for classname, _name, spent in cases:
        found = _match(classname, keys)
        if found is not None:
            seconds[found] = seconds.get(found, 0.0) + spent
    median = statistics.median(spent for _classname, _name, spent in cases)
    missing = len(grouped) - len(seconds)
    print(
        f"按 {TIMES_ENV} 的用时分片：{len(seconds)} 个文件有时间，{missing} 个没有，按用例数 × 中位数 {median:.3f} 秒估计。",
        flush=True,
    )
    return {file_part: seconds.get(file_part, len(items) * median) for file_part, items in grouped.items()}


def node_weights(node_ids: list[str], env: Mapping[str, str]) -> dict[str, float] | None:
    """每个 node id 自己的预计秒数（同一 node id 在 JUnit 里出现几次就加起来）。None 表示回退到按用例数分。"""
    path = env.get(TIMES_ENV, "").strip()
    if not path:
        print(f"{TIMES_ENV} 没有设置，按用例数分道。", flush=True)
        return None
    cases = _junit_cases(path)
    if isinstance(cases, str):
        print(f"{TIMES_ENV} {cases}，按用例数分道。", flush=True)
        return None
    times: dict[tuple[str, str], float] = {}
    for classname, name, spent in cases:
        times[(classname, name)] = times.get((classname, name), 0.0) + spent
    median = statistics.median(spent for _classname, _name, spent in cases)
    keys = {node_id: _case_key(node_id) for node_id in node_ids}
    found = sum(1 for key in keys.values() if key in times)
    print(
        f"按 {TIMES_ENV} 的用时分道：{found} 个用例有时间，{len(keys) - found} 个没有，按中位数 {median:.3f} 秒估计。",
        flush=True,
    )
    return {node_id: times.get(key, median) for node_id, key in keys.items()}


def changed_first(node_ids: list[str], root: Path, env: Mapping[str, str]) -> list[str]:
    """GXR_CHANGED_TESTS 里的文件挪到最前面；两边各自的顺序不变。"""
    changed = {(root / part.strip()).resolve() for part in env.get(CHANGED_ENV, "").split(os.pathsep) if part.strip()}
    if not changed:
        return node_ids
    seen: dict[str, bool] = {}
    for node_id in node_ids:
        file_part = _file_of(node_id)
        if file_part not in seen:
            seen[file_part] = (root / file_part).resolve() in changed
    return [n for n in node_ids if seen[_file_of(n)]] + [n for n in node_ids if not seen[_file_of(n)]]


def check_union(plans: list[list[str]], node_ids: list[str]) -> None:
    """各份计划合起来必须正好是输入的 node id：不重复，不丢，不多。不对就大声失败。"""
    flat = [node_id for plan in plans for node_id in plan]
    dupes = sorted(node_id for node_id, count in Counter(flat).items() if count > 1)
    lost = sorted(set(node_ids) - set(flat))
    extra = sorted(set(flat) - set(node_ids))
    if dupes or lost or extra or len(flat) != len(node_ids):
        print(f"分片计划和收集到的 node id 对不上：重复 {len(dupes)}，丢了 {len(lost)}，多了 {len(extra)}。", flush=True)
        for label, sample in (("重复", dupes), ("丢了", lost), ("多了", extra)):
            for node_id in sample[:10]:
                print(f"  {label} {node_id}", flush=True)
        raise SystemExit(2)


def _grouped(node_ids: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for node_id in node_ids:
        grouped.setdefault(_file_of(node_id), []).append(node_id)
    return grouped


def _balance(
    grouped: Mapping[str, list[str]], weights: Mapping[str, float] | None, workers: int, root: Path, env: Mapping[str, str]
) -> list[tuple[list[str], float]]:
    """文件长的先放，放进当前合计最小的那份；没有用时就按用例数。返回非空的各份和它们的合计。"""
    commands: list[list[str]] = [[] for _ in range(workers)]
    sizes = [0.0 for _ in range(workers)]
    order = sorted(grouped) if weights is None else sorted(grouped, key=lambda path: (-weights[path], path))
    for path in order:
        slot = sizes.index(min(sizes))
        items = sorted(grouped[path])
        sizes[slot] += len(items) if weights is None else weights[path]
        commands[slot].extend(items)
    return [(changed_first(cmd, root, env), size) for cmd, size in zip(commands, sizes, strict=True) if cmd]


def shard_plan(node_ids: list[str], workers: int, root: Path, env: Mapping[str, str]) -> list[list[str]]:
    grouped = _grouped(node_ids)
    weights = file_weights(grouped, env)
    planned = _balance(grouped, weights, workers, root, env)
    shards = [cmd for cmd, _size in planned]
    check_union(shards, node_ids)
    counts = ", ".join(str(len(cmd)) for cmd in shards)
    print(f"非 exclusive 测试 {len(node_ids)} 个，分到 {len(shards)} 个进程：{counts}。", flush=True)
    if weights is not None:
        minutes = ", ".join(f"shard-{index} {size / 60:.1f}" for index, (_cmd, size) in enumerate(planned))
        print(f"各分片计划用时（分钟）：{minutes}。", flush=True)
    return shards


def browser_plan(node_ids: list[str], root: Path, env: Mapping[str, str]) -> list[str]:
    ordered = changed_first(node_ids, root, env)
    check_union([ordered], node_ids)
    return ordered


def browser_lanes(env: Mapping[str, str], workers: int) -> int:
    """浏览器阶段分几道。只有 GXR_BROWSER_LANES=2 才分，道数不超过计算分片的进程数。"""
    raw = env.get(LANES_ENV, "").strip()
    if raw in {"", "1"}:
        return 1
    if raw != "2":
        print(f"{LANES_ENV}={raw!r} 不认识（只认 1 和 2），浏览器阶段照原来一道串行。", flush=True)
        return 1
    lanes = min(2, workers)
    print(f"{LANES_ENV}=2：浏览器阶段分 {lanes} 道（min(2, 计算分片的进程数 {workers})）。", flush=True)
    return lanes


def lane_plan(node_ids: list[str], alone: list[str], lanes: int, root: Path, env: Mapping[str, str]) -> tuple[list[list[str]], list[str]]:
    """非 browser_alone 的浏览器用例按用时分 lanes 道，browser_alone 的按收集顺序留给最后单独跑。

    一个文件里的用例要么都在同一道，要么（标了 browser_alone 的那几个）在单独阶段。
    每个文件的分量只加它分进各道的用例自己的用时，不算同一文件里不在浏览器阶段的用例。
    """
    solo_ids = set(alone)
    weights = node_weights(node_ids, env)
    rest = _grouped([node_id for node_id in node_ids if node_id not in solo_ids])
    rest_weights = None if weights is None else {path: sum(weights[node_id] for node_id in ids) for path, ids in rest.items()}
    planned = _balance(rest, rest_weights, lanes, root, env)
    solo = changed_first([node_id for node_id in node_ids if node_id in solo_ids], root, env)
    plans = [cmd for cmd, _size in planned]
    check_union([*plans, solo], node_ids)
    if weights is None or rest_weights is None:
        sizes = ", ".join(f"lane-{index} {len(cmd)} 个" for index, cmd in enumerate(plans))
        print(f"浏览器分道（没有用时数据，按用例数）：{sizes}；单独阶段 {len(solo)} 个。", flush=True)
    else:
        solo_s = sum(weights[node_id] for node_id in solo)
        minutes = ", ".join(f"lane-{index} {size / 60:.1f}" for index, (_cmd, size) in enumerate(planned))
        print(f"浏览器分道计划用时（分钟）：{minutes}；单独阶段 {solo_s / 60:.1f}（{len(solo)} 个用例）。", flush=True)
    return plans, solo


def stop_on_failure(env: Mapping[str, str]) -> bool:
    return env.get(STOP_ENV, "").strip() == "1"


def halted(stop: bool, codes: list[int], stage: str) -> bool:
    """开了 GXR_STOP_ON_FAILURE，而前面已经有阶段失败时，后面的阶段不再开始。"""
    if stop and any(code != 0 for code in codes):
        print(f"{STOP_ENV}=1：前面已经有失败，不再开始{stage}。", flush=True)
        return True
    return False


def final_status(codes: list[int], stop: bool) -> int:
    """整次的退出码。停下来的那次先取真正失败的分片的码（被停掉的分片是负数的信号码），永远不会是 0。"""
    if stop:
        positive = [code for code in codes if code > 0]
        if positive:
            return positive[0]
        return 1 if any(codes) else 0
    return next((code for code in codes if code != 0), 0)
