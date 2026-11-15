"""Structural guard for the export-pipeline tests' privacy (Sol r1–r3 P1).

The guarantee is at the exit: every test and fixture carries ``@redact_failures``
(``tests/support/export_pipeline_cases.py``), which turns any failure into a report of the
exception type, the file:line and a hash, and swallows stdout/stderr writes, logging and warnings
while the test runs. This file checks that structure over every
``tests/test_core_parity_export_pipeline*.py`` (this one included):

1. every ``test_*`` function and every ``@pytest.fixture`` function carries ``@redact_failures``;
2. no ``print``, ``logging``, ``sys.stdout`` / ``sys.stderr`` or ``warnings.warn`` in these files
   (tallies go through ``note``, which writes to a file);
3. every ``parametrize`` has literal argument values, or literal ids, or ``anon_ids(...)``.

A lint (4) still bans bare ``assert`` that compares anything but plain scalars (no ``len`` of a
variable, no dynamic f-string format specs); it is not the privacy guarantee. Exemptions only
through the list below, each with its reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

from support.export_pipeline_cases import check, redact_failures

TESTS = Path(__file__).resolve().parent
SHORT = 40

# (file name, the assert's source text) -> why it is safe. Rule 4 only.
ALLOW_LIST: dict[tuple[str, str], str] = {}

_LOG_CALLS = ("warn", "error", "warning", "info", "debug", "exception", "critical", "log")


def _decorator_names(fn: ast.FunctionDef) -> list[str]:
    return [ast.unparse(d.func if isinstance(d, ast.Call) else d) for d in fn.decorator_list]


def _ids_ok(ids: ast.expr | None) -> bool:
    if ids is None:
        return False
    if isinstance(ids, (ast.List, ast.Tuple)):
        return all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in ids.elts)
    return isinstance(ids, ast.Call) and isinstance(ids.func, ast.Name) and ids.func.id == "anon_ids"


def _literal(node: ast.expr | None) -> bool:
    if node is None:
        return False
    try:
        ast.literal_eval(node)
    except ValueError:
        return False
    return True


def _structure(source: str, name: str) -> list[str]:
    """Rules 1–3 for one file."""
    out = []
    tree = ast.parse(source)
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        decos = _decorator_names(fn)
        is_fixture = any(d.endswith("fixture") for d in decos)
        if (fn.name.startswith("test_") or is_fixture) and "redact_failures" not in decos:
            out.append(f"{name}:{fn.lineno} {fn.name} lacks @redact_failures")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            out.append(f"{name}:{node.lineno} print")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(m == "logging" or m.startswith("logging.") for m in mods):
                out.append(f"{name}:{node.lineno} logging")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "sys" and node.attr in ("stdout", "stderr", "__stdout__", "__stderr__"):
                out.append(f"{name}:{node.lineno} sys.{node.attr}")
            if node.value.id in ("logging", "warnings") and node.attr in _LOG_CALLS:
                out.append(f"{name}:{node.lineno} {node.value.id}.{node.attr}")
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("parametrize"):
            ids = next((k.value for k in node.keywords if k.arg == "ids"), None)
            values = node.args[1] if len(node.args) > 1 else None
            if not (_ids_ok(ids) or (ids is None and _literal(values))):
                out.append(f"{name}:{node.lineno} parametrize ids are not literal or anon_ids")
    return out


def _plain(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        v = node.value
        return v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= SHORT)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _plain(node.operand)
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_plain(e) for e in node.elts)
    if isinstance(node, ast.Call):
        # len() only of a literal: pytest prints the argument of len(variable) (Sol r3 P1).
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
            if v.format_spec is not None and not _joined_ok(v.format_spec):  # nested specs (Sol r3 P1)
                return False
    return True


def _message_ok(msg: ast.expr | None) -> bool:
    if msg is None:
        return True
    if isinstance(msg, ast.Constant) and isinstance(msg.value, str):
        return True
    return isinstance(msg, ast.JoinedStr) and _joined_ok(msg)


def violations(source: str, name: str) -> list[str]:
    """Rule 4: bare ``assert`` comparing non-plain operands, as ``name:line``."""
    out = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assert):
            continue
        text = ast.get_source_segment(source, node) or ""
        if (name, text.strip()) in ALLOW_LIST:
            continue
        if not all(_plain(o) for o in _operands(node.test)) or not _message_ok(node.msg):
            out.append(f"{name}:{node.lineno}")
    return out


def _files() -> list[Path]:
    return sorted(TESTS.glob("test_core_parity_export_pipeline*.py"))


@redact_failures
def test_every_test_is_redacted_and_writes_nothing() -> None:
    files = _files()
    check(len(files) >= 6, f"only {len(files)} files")
    found = [v for f in files for v in _structure(f.read_text(encoding="utf-8"), f.name)]
    check(not found, "structure: " + ", ".join(found))


@redact_failures
def test_no_bare_assert_compares_photo_data() -> None:
    found = [v for f in _files() for v in violations(f.read_text(encoding="utf-8"), f.name)]
    check(not found, "bare asserts that can print photo data: " + ", ".join(found))


@redact_failures
def test_allow_list_entries_still_exist() -> None:
    """A stale allow-list entry would silently cover a future line with the same text."""
    texts = set()
    for f in _files():
        s = f.read_text(encoding="utf-8")
        texts |= {(f.name, (ast.get_source_segment(s, n) or "").strip()) for n in ast.walk(ast.parse(s)) if isinstance(n, ast.Assert)}
    check(all(k in texts for k in ALLOW_LIST), "stale allow-list entries")


@redact_failures
def test_guard_catches_the_known_forms() -> None:
    lint_bad = [
        "assert got == py_file",
        'assert got == py_file, f"{stem}: {first_difference(got, py_file)}"',
        'assert hdr_state["active"] is True, (stem, hdr_state)',
        "assert code == 0, payload",
        "assert np.array_equal(canvas, py_rgb)",
        "assert len(lens) == 0",
        'assert len({"lens": lens}) == 0',
        'assert 1 == 2, f"{0:{lens}}"',
        'assert x == "' + "a" * (SHORT + 1) + '"',
    ]
    lint_good = ["assert True", 'assert 1 in (1, 2), "plain"', "assert -1 < 0", "assert len((1, 2)) == 2", 'assert 1 == 1, f"{0:>4}"']
    for src in lint_bad:
        check(len(violations(src, "probe.py")) == 1, "lint misses a bad form")
    for src in lint_good:
        check(len(violations(src, "probe.py")) == 0, "lint flags a plain form")
    structure_bad = [
        "def test_x():\n    pass\n",
        "import pytest\n@pytest.fixture\ndef f():\n    return 1\n",
        "@redact_failures\ndef test_x():\n    print(1)\n",
        "import logging\n",
        "@redact_failures\ndef test_x():\n    sys.stdout.write('x')\n",
        "@redact_failures\ndef test_x():\n    warnings.warn('x')\n",
        "@pytest.mark.parametrize('c', CASES)\n@redact_failures\ndef test_x(c):\n    pass\n",
        "@pytest.mark.parametrize('c', [0], ids=[lens])\n@redact_failures\ndef test_x(c):\n    pass\n",
    ]
    structure_good = [
        "@redact_failures\ndef test_x():\n    pass\n",
        "@pytest.mark.parametrize('c', [0, 1])\n@redact_failures\ndef test_x(c):\n    pass\n",
        "@pytest.mark.parametrize('c', CASES, ids=anon_ids('C', CASES))\n@redact_failures\ndef test_x(c):\n    pass\n",
        "@pytest.fixture\n@redact_failures\ndef f():\n    return 1\n",
    ]
    for src in structure_bad:
        check(len(_structure(src, "probe.py")) >= 1, "structure misses a bad form")
    for src in structure_good:
        check(len(_structure(src, "probe.py")) == 0, "structure flags a good form")
