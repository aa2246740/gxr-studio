"""A lint for accidental leaks in the export-pipeline tests (Sol r4, threat model in docs/mobile.md).

The threat model: test code written in this repo's normal style, run with ``make test``,
``make check`` or pytest ``-q`` / ``-v`` / ``-vv``, must not leak photo-derived data by accident.
The guarantee comes from the redacted helpers in ``tests/support/export_pipeline_cases.py``; this
lint only catches the usual slips, over every ``tests/test_core_parity_export_pipeline*.py`` and
the export-pipeline canary support files. It does not try to resolve aliases or class-method tricks
(deliberately adversarial code is code review's job). Each violation is a rule code plus file:line:

- ``E_ASSERT``: a bare ``assert`` comparing anything but plain scalars (including ``len`` of a
  non-literal, and dynamic f-string format specs);
- ``E_RAISE``: ``raise AssertionError(...)`` or ``pytest.fail(...)`` with non-literal content;
- ``E_OUTPUT``: ``print``, ``logging``, ``sys.stdout`` / ``sys.stderr`` or ``warnings.warn``;
- ``E_IDS``: parametrize ids that are not literals or ``anon_ids(<count>)``, a non-literal
  ``pytest.param(id=...)``, or fixture ``params`` / ``ids`` that are not literals;
- ``E_LABEL``: ``check(...)`` with a label that is not a literal string.
"""

from __future__ import annotations

import ast
from pathlib import Path

from support.export_pipeline_cases import check, lint_report

TESTS = Path(__file__).resolve().parent
SHORT = 40


def _files() -> list[Path]:
    return sorted([*TESTS.glob("test_core_parity_export_pipeline*.py"), *(TESTS / "support").glob("export_pipeline_canary*.py")])


def _literal(node: ast.expr | None) -> bool:
    if node is None:
        return True
    try:
        ast.literal_eval(node)
    except ValueError:
        return False
    return True


def _literal_str(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _plain(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        v = node.value
        return v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= SHORT)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _plain(node.operand)
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_plain(e) for e in node.elts)
    if isinstance(node, ast.Call):
        return (
            isinstance(node.func, ast.Name)
            and node.func.id == "len"
            and len(node.args) == 1
            and not node.keywords
            and isinstance(node.args[0], (ast.Constant, ast.Tuple, ast.List))
            and _plain(node.args[0])
        )
    return False


def _operands(test: ast.expr) -> list[ast.expr]:
    if isinstance(test, ast.Compare):
        return [test.left, *test.comparators]
    if isinstance(test, ast.BoolOp):
        return [o for v in test.values for o in _operands(v)]
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _operands(test.operand)
    return [test]


def _joined_ok(js: ast.JoinedStr) -> bool:
    for v in js.values:
        if isinstance(v, ast.FormattedValue):
            if not _plain(v.value):
                return False
            if v.format_spec is not None and not _joined_ok(v.format_spec):
                return False
    return True


def _message_ok(msg: ast.expr | None) -> bool:
    if msg is None or _literal_str(msg):
        return True
    return isinstance(msg, ast.JoinedStr) and _joined_ok(msg)


def _anon_ids_ok(node: ast.expr) -> bool:
    """``anon_ids(<int>)``, ``anon_ids(len(NAME))`` or ``anon_ids(len(NAME) + <int>)``: a count only."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "anon_ids"
        and len(node.args) == 1
        and not node.keywords
    ):
        return False
    arg = node.args[0]

    def count(n: ast.expr) -> bool:
        if isinstance(n, ast.Constant) and isinstance(n.value, int):
            return True
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "len"
            and len(n.args) == 1
            and isinstance(n.args[0], ast.Name)
        ):
            return True
        return isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add) and count(n.left) and count(n.right)

    return count(arg)


def _ids_ok(ids: ast.expr | None) -> bool:
    if ids is None:
        return False
    if isinstance(ids, (ast.List, ast.Tuple)):
        return all(_literal_str(e) for e in ids.elts)
    return _anon_ids_ok(ids)


def lint(source: str, name: str) -> list[str]:
    """Each accidental-leak pattern in ``source`` as ``E_RULE name:line``."""
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        at = getattr(node, "lineno", 0)
        if isinstance(node, ast.Assert) and (not all(_plain(o) for o in _operands(node.test)) or not _message_ok(node.msg)):
            out.append(f"E_ASSERT {name}:{at}")
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and ast.unparse(node.exc.func) == "AssertionError":
            if not all(_literal(a) for a in node.exc.args):
                out.append(f"E_RAISE {name}:{at}")
        if isinstance(node, ast.Call):
            func = ast.unparse(node.func)
            if func == "pytest.fail" and node.args and not _literal_str(node.args[0]):
                out.append(f"E_RAISE {name}:{at}")
            if func == "print" or func.startswith("logging.") or func in ("warnings.warn", "sys.stdout.write", "sys.stderr.write"):
                out.append(f"E_OUTPUT {name}:{at}")
            if func.endswith("parametrize"):
                ids = next((k.value for k in node.keywords if k.arg == "ids"), None)
                values = node.args[1] if len(node.args) > 1 else None
                if not (_ids_ok(ids) or (ids is None and values is not None and _literal(values))):
                    out.append(f"E_IDS {name}:{at}")
            if func == "pytest.param" and any(k.arg == "id" and not _literal_str(k.value) for k in node.keywords):
                out.append(f"E_IDS {name}:{at}")
            if func.endswith("fixture") and any(k.arg in ("params", "ids") and not _literal(k.value) for k in node.keywords):
                out.append(f"E_IDS {name}:{at}")
            if func == "check" and len(node.args) >= 2 and not _literal_str(node.args[1]):
                out.append(f"E_LABEL {name}:{at}")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(m == "logging" or m.startswith("logging.") for m in mods):
                out.append(f"E_OUTPUT {name}:{at}")
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
            and node.attr in ("stdout", "stderr")
        ):
            out.append(f"E_OUTPUT {name}:{at}")
    return sorted(set(out), key=lambda v: (v.split()[1], v))


def test_export_pipeline_tests_pass_the_lint() -> None:
    files = _files()
    check(len(files) >= 7, "export-pipeline test files are missing")
    lint_report([v for f in files for v in lint(f.read_text(encoding="utf-8"), f.name)])


def test_lint_catches_the_known_patterns() -> None:
    bad = {
        "assert got == py_file": "E_ASSERT",
        "assert len(lens) == 0": "E_ASSERT",
        'assert len({"lens": lens}) == 0': "E_ASSERT",
        'assert 1 == 2, f"{0:{lens}}"': "E_ASSERT",
        "assert code == 0, payload": "E_ASSERT",
        "raise AssertionError(lens)": "E_RAISE",
        "pytest.fail(lens, pytrace=False)": "E_RAISE",
        "print(lens)": "E_OUTPUT",
        "import logging": "E_OUTPUT",
        "sys.stdout.write(lens)": "E_OUTPUT",
        "warnings.warn(lens)": "E_OUTPUT",
        "@pytest.mark.parametrize('c', CASES)\ndef test_x(c):\n    pass\n": "E_IDS",
        "@pytest.mark.parametrize('c', [0], ids=[lens])\ndef test_x(c):\n    pass\n": "E_IDS",
        "@pytest.mark.parametrize('c', [0], ids=anon_ids(lens, [0]))\ndef test_x(c):\n    pass\n": "E_IDS",
        "@pytest.mark.parametrize('c', [pytest.param(0, id=lens)], ids=['safe'])\ndef test_x(c):\n    pass\n": "E_IDS",
        "@pytest.fixture(params=[lens])\ndef f(request):\n    return 1\n": "E_IDS",
        'check(False, f"{lens}")': "E_LABEL",
    }
    for src, rule in bad.items():
        found = lint(src, "probe.py")
        check(len(found) >= 1 and all(v.startswith(rule) for v in found), "the lint misses a known pattern")
    good = [
        "assert True",
        'assert 1 in (1, 2), "plain"',
        "assert len((1, 2)) == 2",
        'check(ok, "literal label")',
        'raise AssertionError("literal")',
        'pytest.fail("literal", pytrace=False)',
        "@pytest.mark.parametrize('c', [0, 1])\ndef test_x(c):\n    pass\n",
        "@pytest.mark.parametrize('c', CASES, ids=anon_ids(len(CASES)))\ndef test_x(c):\n    pass\n",
        "@pytest.mark.parametrize('c', CASES, ids=['a', 'b'])\ndef test_x(c):\n    pass\n",
        "@pytest.fixture(params=[1, 2])\ndef f(request):\n    return 1\n",
    ]
    for src in good:
        check(len(lint(src, "probe.py")) == 0, "the lint flags a plain pattern")


def test_lint_report_names_the_rule_file_and_line(tmp_path: Path) -> None:
    """P3-01: a violation is reported as its rule code and file:line, so it can be fixed."""
    import subprocess
    import sys

    bad = tmp_path / "test_core_parity_export_pipeline_bad.py"
    bad.write_text("def test_missing():\n    assert got == want\n", encoding="utf-8")
    code = "import sys; sys.path[:0] = ['%s']; from support.export_pipeline_cases import lint_report; " % TESTS
    code += "from test_core_parity_export_pipeline_guard import lint; from pathlib import Path; "
    code += "p = Path('%s'); lint_report(lint(p.read_text(), p.name))" % bad
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=60)  # noqa: S603  # nosec B603
    check(proc.returncode != 0, "a violation did not fail the lint")
    check("E_ASSERT test_core_parity_export_pipeline_bad.py:2" in proc.stdout + proc.stderr, "the report lacks the rule, file and line")
