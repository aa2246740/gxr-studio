"""AST scan: no file under ``tests/`` finds samples on its own (see ``support/samples.py``).

It flags executable code only, so comments and docstrings never match:

- reading the variable: ``os.environ.get("GXR_STUDIO_TEST_PARENT")``, ``os.getenv(...)``,
  ``os.environ[...]`` (load) and ``"GXR_STUDIO_TEST_PARENT" in os.environ``;
- the product's ``DEFAULT_SAMPLES`` (a name, an attribute or an import);
- a path built from ``__file__`` that climbs above the repository: ``.parents[k]``,
  ``.parent`` chains, ``os.path.dirname``, also through module names assigned from such a path.
  The depth is counted from the file's real place, so ``tests/x.py`` may reach ``parents[1]``
  (the repo) and ``tests/support/x.py`` ``parents[2]``.

Only the exact files in ``EXEMPT`` (paths relative to ``tests/``) are skipped.
"""

from __future__ import annotations

import ast
from pathlib import Path

VARIABLE = "GXR_STUDIO_TEST_PARENT"
EXEMPT = frozenset({"support/samples.py", "test_sample_paths.py"})


def _is_variable(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value == VARIABLE


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


class _Scan(ast.NodeVisitor):
    def __init__(self, depth: int) -> None:
        self.depth = depth  # directories between the repo root and the file
        self.names: dict[str, int] = {}
        self.found: list[tuple[int, str]] = []

    def level(self, node: ast.AST) -> int | None:
        """How far above the file's own path ``node`` points: the file is -1, its folder 0, the repo ``depth``."""
        if isinstance(node, ast.Name):
            return -1 if node.id == "__file__" else self.names.get(node.id)
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            base = self.level(node.value)
            return None if base is None else base + 1
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents":
            base = self.level(node.value.value)
            if base is None:
                return None
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, int):
                return base + index.value + 1
            return self.depth + 1  # a computed index: treat it as climbing out
        if isinstance(node, ast.Call):
            func = _name_of(node.func)
            if isinstance(node.func, ast.Attribute) and func in {"resolve", "absolute", "expanduser"}:
                return self.level(node.func.value)
            if func in {"Path", "PurePath", "PosixPath", "str", "fspath", "abspath", "realpath", "normpath"} and node.args:
                return self.level(node.args[0])
            if func == "dirname" and node.args:
                base = self.level(node.args[0])
                return None if base is None else base + 1
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            base = self.level(node.left)
            if base is not None and isinstance(node.right, ast.Constant) and node.right.value == "..":
                return base + 1
            return None if base is None or base <= self.depth else base
        return None

    def flag(self, node: ast.AST, what: str) -> None:
        self.found.append((getattr(node, "lineno", 0), what))

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        level = self.level(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name) and level is not None:
                self.names[target.id] = level

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        if node.value is not None and isinstance(node.target, ast.Name):
            level = self.level(node.value)
            if level is not None:
                self.names[node.target.id] = level

    def visit_Call(self, node: ast.Call) -> None:
        func = _name_of(node.func)
        if func in {"get", "getenv", "pop", "setdefault", "environ"} and node.args and _is_variable(node.args[0]):
            self.flag(node, f"自己读 {VARIABLE}")
        self.check_climb(node)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Load) and _name_of(node.value) == "environ" and _is_variable(node.slice):
            self.flag(node, f"自己读 {VARIABLE}")
        self.check_climb(node)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if _is_variable(node.left) and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            if any(_name_of(c) == "environ" for c in node.comparators):
                self.flag(node, f"自己读 {VARIABLE}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "DEFAULT_SAMPLES":
            self.flag(node, "DEFAULT_SAMPLES")
        self.check_climb(node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "DEFAULT_SAMPLES":
            self.flag(node, "DEFAULT_SAMPLES")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if any(alias.name == "DEFAULT_SAMPLES" for alias in node.names):
            self.flag(node, "DEFAULT_SAMPLES")

    def check_climb(self, node: ast.AST) -> None:
        level = self.level(node)
        if level is not None and level > self.depth:
            self.flag(node, "按 __file__ 找仓库上一级")


def scan_source(source: str, depth: int) -> list[tuple[int, str]]:
    """Findings ``(line, what)`` for one file ``depth`` directories below the repo root."""
    scan = _Scan(depth)
    scan.visit(ast.parse(source))
    return sorted(set(scan.found))


def scan_tests(tests: Path) -> list[str]:
    """Every finding under the ``tests/`` folder ``tests`` (its parent is the repo root)."""
    out = []
    for path in sorted(tests.rglob("*.py")):
        rel = path.relative_to(tests).as_posix()
        if rel in EXEMPT:
            continue
        depth = len(path.relative_to(tests.parent).parts) - 1
        out += [f"{rel}:{line}: {what}" for line, what in scan_source(path.read_text(encoding="utf-8"), depth)]
    return out
