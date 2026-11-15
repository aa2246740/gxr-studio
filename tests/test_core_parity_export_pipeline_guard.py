"""Static guard (Sol r2 P1): no bare ``assert`` in the export-pipeline tests may compare anything
that could hold real-photo data.

pytest's assertion rewriting prints both operands of a failing ``assert`` (in full under
``-vv``, into stdout and JUnit). Real photos' EXIF reaches these tests through file bytes,
``meta``, layouts, labels and state, so every such comparison goes through a redacting helper
(``same_redacted``, ``same_file_redacted``, ``same_pixels``, ``check``, ``cli_ok``) that compares
with a plain ``if`` and fails with ``pytrace=False``.

This test parses every ``tests/test_core_parity_export_pipeline*.py`` (this file included) and
the export-pipeline support modules, and fails on any ``assert`` whose test or message uses
anything other than plain scalars: ints, floats, bools, ``None``, short literal strings, tuples
of those, and ``len(...)``. The only other way through is ``ALLOW_LIST`` below, one entry per
line with its reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SHORT = 40

# (file name, the assert's source text) -> why it cannot leak photo data.
ALLOW_LIST: dict[tuple[str, str], str] = {}


def _plain(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        v = node.value
        return v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= SHORT)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _plain(node.operand)
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_plain(e) for e in node.elts)
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id == "len" and len(node.args) == 1 and not node.keywords
    return False


def _operands(test: ast.expr) -> list[ast.expr]:
    if isinstance(test, ast.Compare):
        return [test.left, *test.comparators]
    if isinstance(test, ast.BoolOp):
        return [o for v in test.values for o in _operands(v)]
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _operands(test.operand)
    return [test]


def _message_ok(msg: ast.expr | None) -> bool:
    if msg is None:
        return True
    if isinstance(msg, ast.Constant) and isinstance(msg.value, str):
        return True
    if isinstance(msg, ast.JoinedStr):
        return all(not isinstance(v, ast.FormattedValue) or _plain(v.value) for v in msg.values)
    return False


def violations(source: str, name: str) -> list[str]:
    """Each bare ``assert`` in ``source`` that compares non-plain operands, as ``name:line``."""
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
    return sorted([*TESTS.glob("test_core_parity_export_pipeline*.py"), *(TESTS / "support").glob("export_pipeline_*.py")])


def test_no_bare_assert_compares_photo_data() -> None:
    files = _files()
    assert len(files) >= 7
    found = [v for f in files for v in violations(f.read_text(encoding="utf-8"), f.name)]
    if found:
        import pytest

        pytest.fail("bare asserts that can print photo data (use a redacted helper): " + ", ".join(found), pytrace=False)


def test_allow_list_entries_still_exist() -> None:
    """A stale allow-list entry would silently cover a future line with the same text."""
    texts = {
        (f.name, (ast.get_source_segment(s, n) or "").strip())
        for f in _files()
        for s in [f.read_text(encoding="utf-8")]
        for n in ast.walk(ast.parse(s))
        if isinstance(n, ast.Assert)
    }
    stale = [k for k in ALLOW_LIST if k not in texts]
    assert len(stale) == 0


def test_guard_catches_the_r2_cases() -> None:
    bad = [
        "assert got == py_file",
        'assert got == py_file, f"{stem}: {first_difference(got, py_file)}"',
        "assert rs_file == hc.assemble(payload, gain, meta)",
        'assert hdr_state["active"] is True, (stem, hdr_state)',
        "assert code == 0, payload",
        "assert np.array_equal(canvas, py_rgb)",
        'assert prepared["frame"] == want',
        "assert CANARY not in text",
        'assert x == "' + "a" * (SHORT + 1) + '"',
    ]
    good = ["assert len(rs) == len(py) == 588", "assert True", 'assert 1 in (1, 2), "plain"', "assert -1 < 0"]
    for src in bad:
        assert len(violations(src, "probe.py")) == 1
    for src in good:
        assert len(violations(src, "probe.py")) == 0
