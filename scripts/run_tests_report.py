#!/usr/bin/env python3
"""父进程把各阶段的 JUnit 和覆盖率收成用户要的那一份。"""

from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAIL_UNDER = "85"


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
    print(f"junit {dest} tests={tests} failures={failures} errors={errors} skipped={skipped}", flush=True)


def _gate_report(combined: Path, specs: list[str]) -> int:
    command = [sys.executable, "-I", "-m", "coverage", "report", "--data-file", str(combined), f"--fail-under={FAIL_UNDER}", "--show-missing"]
    if any(spec.partition(":")[2] == "skip-covered" for spec in specs if spec.startswith("term")):
        command.append("--skip-covered")
    return subprocess.run(command, cwd=ROOT).returncode  # noqa: S603


def _emit_markdown(combined: Path, dest: Path, append: bool) -> int:
    script = (
        "import coverage, sys\n"
        "cov = coverage.Coverage(data_file=sys.argv[1])\n"
        "cov.load()\n"
        "with open(sys.argv[2], sys.argv[3], encoding='utf-8') as handle:\n"
        "    cov.report(ignore_errors=True, file=handle, output_format='markdown')\n"
    )
    mode = "a" if append else "w"
    return subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", script, str(combined), str(dest), mode],
        cwd=ROOT,
    ).returncode


def _emit_cov(combined: Path, spec: str) -> int:
    if spec == "":
        return 0
    kind, _sep, dest = spec.partition(":")
    if kind in {"term", "term-missing"}:
        return 0
    data = ["--data-file", str(combined)]
    if kind == "xml":
        command = ["xml", *data, *(["-o", dest] if dest else [])]
    elif kind == "html":
        command = ["html", *data, *(["-d", dest] if dest else [])]
    elif kind == "json":
        command = ["json", *data, *(["-o", dest] if dest else [])]
    elif kind == "lcov":
        command = ["lcov", *data, *(["-o", dest] if dest else [])]
    elif kind == "annotate":
        command = ["annotate", *data, *(["-d", dest] if dest else [])]
    elif kind == "markdown":
        return _emit_markdown(combined, Path(dest or "coverage.md"), append=False)
    elif kind == "markdown-append":
        return _emit_markdown(combined, Path(dest or "coverage.md"), append=True)
    else:
        print(f"不认识的覆盖率报告 {spec}", flush=True)
        return 2
    return subprocess.run([sys.executable, "-I", "-m", "coverage", *command], cwd=ROOT).returncode  # noqa: S603


def combine_coverage(data_files: list[Path], reports: list[str]) -> int:
    present = [path for path in data_files if path.is_file() and path.stat().st_size > 0]
    if not present:
        print("没有覆盖率数据，无法核对 85% 门槛。", flush=True)
        return 1
    combined = present[0].parent / ".coverage.combined"
    combine = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-I",
            "-m",
            "coverage",
            "combine",
            "--keep",
            "--data-file",
            str(combined),
            *[str(path) for path in present],
        ],
        cwd=ROOT,
    )
    if combine.returncode != 0:
        return combine.returncode
    status = _gate_report(combined, reports)
    for spec in reports:
        code = _emit_cov(combined, spec)
        if status == 0 and code != 0:
            status = code
    return status
