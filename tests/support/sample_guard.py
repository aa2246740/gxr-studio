"""AST scan: no file under ``tests/`` finds samples on its own (see ``support/samples.py``).

It flags executable code only, so comments and docstrings never match:

- reading the variable from the environment: ``os.environ.get/pop/setdefault("GXR_STUDIO_TEST_PARENT")``,
  ``os.getenv(...)``, ``os.environ[...]`` (load) and ``"GXR_STUDIO_TEST_PARENT" in os.environ``.
  The receiver must really be the environment: ``os`` (or ``import os as x``), ``from os import environ``
  or ``from os import getenv`` and their aliases. A plain dictionary's ``.get`` is not a read;
- the product's ``DEFAULT_SAMPLES`` (a name, an attribute or an import);
- a path built from ``__file__`` that climbs above the repository: ``.parents[k]``,
  ``.parent`` chains, ``os.path.dirname``, also through names bound to such a path.
  The depth is counted from the file's real place, so ``tests/x.py`` may reach ``parents[1]``
  (the repo) and ``tests/support/x.py`` ``parents[2]``.

Name bindings follow Python's scopes: each function (and lambda and comprehension) has its own,
parameters and local assignments shadow outer names, class bodies are not visible to methods,
and assigning a value the scan cannot follow forgets the old path.

Only the exact files in ``EXEMPT`` (paths relative to ``tests/``) are skipped.
"""

from __future__ import annotations

import ast
from pathlib import Path

VARIABLE = "GXR_STUDIO_TEST_PARENT"
EXEMPT = frozenset({"support/samples.py", "test_sample_paths.py"})
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

# What a tracked name stands for: an int is a path level above the file; a str is an environment object.
Binding = int | str | None
OS, ENVIRON, GETENV = "os", "environ", "getenv"


def _is_variable(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value == VARIABLE


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _stored_names(body: list[ast.stmt]) -> set[str]:
    """Names a function body binds (Python makes them local for the whole body), not looking into nested scopes."""
    out: set[str] = set()
    todo: list[ast.AST] = list(body)
    while todo:
        node = todo.pop()
        if isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            if not isinstance(node, ast.Lambda):
                out.add(node.name)
            continue
        if isinstance(node, _COMPREHENSIONS):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            out.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        todo.extend(ast.iter_child_nodes(node))
    return out


def _arguments(args: ast.arguments) -> list[str]:
    every = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    every += [a for a in (args.vararg, args.kwarg) if a is not None]
    return [a.arg for a in every]


class _Scan(ast.NodeVisitor):
    def __init__(self, depth: int) -> None:
        self.depth = depth  # directories between the repo root and the file
        self.scopes: list[tuple[str, dict[str, Binding]]] = [("module", {OS: OS})]
        self.found: list[tuple[int, str]] = []

    # ---- scopes ----------------------------------------------------------------

    def lookup(self, name: str) -> Binding:
        innermost = len(self.scopes) - 1
        for index in range(innermost, -1, -1):
            kind, names = self.scopes[index]
            if kind == "class" and index != innermost:
                continue  # methods do not see the class body
            if name in names:
                return names[name]
        return None

    def bind(self, name: str, value: Binding) -> None:
        self.scopes[-1][1][name] = value

    def in_scope(self, kind: str, names: dict[str, Binding], body: list[ast.AST]) -> None:
        self.scopes.append((kind, names))
        try:
            for node in body:
                self.visit(node)
        finally:
            self.scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for item in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
            if item is not None:
                self.visit(item)
        self.bind(node.name, None)
        local: dict[str, Binding] = dict.fromkeys(_stored_names(node.body), None)
        local.update(dict.fromkeys(_arguments(node.args), None))
        self.in_scope("function", local, list(node.body))

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for item in [*node.args.defaults, *node.args.kw_defaults]:
            if item is not None:
                self.visit(item)
        self.in_scope("function", dict.fromkeys(_arguments(node.args), None), [node.body])

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for item in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(item)
        self.bind(node.name, None)
        self.in_scope("class", {}, list(node.body))

    def visit_comprehension_scope(self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp) -> None:
        first, *rest = node.generators
        self.visit(first.iter)  # the first iterable is evaluated in the enclosing scope
        parts: list[ast.AST] = [first.target, *first.ifs]
        for generator in rest:
            parts += [generator.iter, generator.target, *generator.ifs]
        parts += [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        self.in_scope("function", {}, parts)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self.visit_comprehension_scope(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self.visit_comprehension_scope(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self.visit_comprehension_scope(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self.visit_comprehension_scope(node)

    # ---- bindings --------------------------------------------------------------

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bind(node.id, None)  # any rebinding forgets the old path
        elif node.id == "DEFAULT_SAMPLES":
            self.flag(node, "DEFAULT_SAMPLES")

    def assign(self, targets: list[ast.expr], value: ast.expr | None) -> None:
        binding: Binding = None
        if value is not None:
            self.visit(value)
            binding = self.binding_of(value)
        for target in targets:
            self.visit(target)
        if len(targets) == 1 and isinstance(targets[0], ast.Name) and binding is not None:
            self.bind(targets[0].id, binding)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.assign(list(node.targets), node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        self.assign([node.target], node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.assign([node.target], node.value)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "os" or alias.name.startswith("os."):
                self.bind(alias.asname or "os", OS if alias.asname is None or alias.name == "os" else None)
            else:
                self.bind((alias.asname or alias.name).split(".")[0], None)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            name = alias.asname or alias.name
            if alias.name == "DEFAULT_SAMPLES":
                self.flag(node, "DEFAULT_SAMPLES")
            if node.module == "os" and alias.name in (ENVIRON, GETENV):
                self.bind(name, alias.name)
            else:
                self.bind(name, None)

    def binding_of(self, node: ast.AST) -> Binding:
        level = self.level(node)
        if level is not None:
            return level
        if isinstance(node, ast.Name):
            return self.lookup(node.id)
        if self.is_environ(node):
            return ENVIRON
        if isinstance(node, ast.Attribute) and node.attr == GETENV and self.lookup(_name_of(node.value)) == OS:
            return GETENV
        return None

    # ---- environment reads -----------------------------------------------------

    def is_environ(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return self.lookup(node.id) == ENVIRON
        return (
            isinstance(node, ast.Attribute) and node.attr == ENVIRON and isinstance(node.value, ast.Name) and self.lookup(node.value.id) == OS
        )

    def is_getenv(self, func: ast.AST) -> bool:
        if isinstance(func, ast.Name):
            return self.lookup(func.id) == GETENV
        return isinstance(func, ast.Attribute) and func.attr == GETENV and isinstance(func.value, ast.Name) and self.lookup(func.value.id) == OS

    def visit_Call(self, node: ast.Call) -> None:
        if node.args and _is_variable(node.args[0]):
            func = node.func
            method = isinstance(func, ast.Attribute) and func.attr in {"get", "pop", "setdefault"} and self.is_environ(func.value)
            if method or self.is_getenv(func):
                self.flag(node, f"自己读 {VARIABLE}")
        self.check_climb(node)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Load) and self.is_environ(node.value) and _is_variable(node.slice):
            self.flag(node, f"自己读 {VARIABLE}")
        self.check_climb(node)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if _is_variable(node.left) and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            if any(self.is_environ(c) for c in node.comparators):
                self.flag(node, f"自己读 {VARIABLE}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "DEFAULT_SAMPLES":
            self.flag(node, "DEFAULT_SAMPLES")
        self.check_climb(node)
        self.generic_visit(node)

    # ---- climbing out of the repo ----------------------------------------------

    def level(self, node: ast.AST) -> int | None:
        """How far above the file's own path ``node`` points: the file is -1, its folder 0, the repo ``depth``."""
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return -1
            bound = self.lookup(node.id)
            return bound if isinstance(bound, int) else None
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
