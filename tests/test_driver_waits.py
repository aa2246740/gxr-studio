"""浏览器驱动里的 `waitForFunction` 谓词不能返回 Promise（静态检查，不开浏览器）。

固定的 Playwright 1.61.1 里，`page.waitForFunction(fn)` 只要 `fn` 返回 Promise 就立刻结束，
哪怕它之后解析成 `false`（实测 63 ms）。所以 `async` 谓词、返回 `import(...)`、`.then(...)`、
`new Promise` 或其他 thenable 的谓词什么也没等。要等异步条件，用 `page.evaluate` 轮询
（`support/browser_wait.mjs` 的 `waitForPage`），或者改成读页面已经设好的同步标志。

本测试扫描 `tests/` 下的 `.mjs`/`.js` 驱动和 `test_*.py` 里内联的驱动脚本，找出每个
`waitForFunction(` / `wait_for_function(` 的第一个参数；参数是同文件里定义的名字时，按定义检查。
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
CALL = re.compile(r"\b(?:waitForFunction|wait_for_function)\s*\(")
# 谓词本身是异步的，或会产出 thenable
OFFENDER = re.compile(
    r"^\s*async\b"  # async () => … / async function
    r"|\bawait\b"
    r"|\bimport\s*\("
    r"|\.\s*then\s*\("
    r"|\bnew\s+Promise\b"
    r"|\bPromise\s*\.\s*(?:resolve|reject|all|allSettled|any|race)\b"
)
IDENT = re.compile(r"[A-Za-z_$][\w$]*")
# 正则字面量前面能出现的字符（否则 / 当除号）
_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^") | {""}
_CLOSE = {"(": ")", "[": "]", "{": "}"}


def _skip_string(src: str, i: int) -> int:
    """`src[i]` 是 ' 或 "，返回字符串结束后的位置。"""
    q = src[i]
    i += 1
    while i < len(src) and src[i] != q:
        i += 2 if src[i] == "\\" else 1
    return i + 1


def _skip_template(src: str, i: int) -> int:
    """`src[i]` 是反引号，返回模板结束后的位置（`${…}` 里按表达式处理）。"""
    i += 1
    while i < len(src) and src[i] != "`":
        if src[i] == "\\":
            i += 2
        elif src.startswith("${", i):
            i = _scan(src, i + 2, "}") + 1
        else:
            i += 1
    return i + 1


def _skip_regex(src: str, i: int) -> int:
    i += 1
    in_class = False
    while i < len(src) and src[i] != "\n":
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            i += 1
            while i < len(src) and src[i].isalpha():
                i += 1
            return i
        i += 1
    return i


def _prev_sig(src: str, i: int) -> str:
    j = i - 1
    while j >= 0 and src[j].isspace():
        j -= 1
    if j < 0:
        return ""
    m = re.search(r"[\w$]+$", src[: j + 1])
    if m and m.group() in {"return", "typeof", "case", "in", "of", "void", "delete", "throw"}:
        return ""
    return src[j]


def _scan(src: str, i: int, stop: str) -> int:
    """从 `i` 开始扫到同层的 `stop` 中任一字符，返回其位置（括号、字符串、注释、正则都跳过）。"""
    stack: list[str] = []
    while i < len(src):
        c = src[i]
        if not stack and c in stop:
            return i
        if c in "'\"":
            i = _skip_string(src, i)
            continue
        if c == "`":
            i = _skip_template(src, i)
            continue
        if src.startswith("//", i):
            i = src.find("\n", i)
            i = len(src) if i < 0 else i
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = len(src) if j < 0 else j + 2
            continue
        if c == "/" and _prev_sig(src, i) in _REGEX_PREV:
            i = _skip_regex(src, i)
            continue
        if c in _CLOSE:
            stack.append(_CLOSE[c])
        elif stack and c == stack[-1]:
            stack.pop()
        elif c in ")]}" and not stack:
            return i
        i += 1
    return i


def _definition(src: str, name: str) -> str | None:
    """同文件里 `const|let|var name = …` 或 `function name(…) {…}` 的定义文本。"""
    m = re.search(rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=\s*", src)
    if m:
        return src[m.end() : _scan(src, m.end(), ",;\n")]
    m = re.search(rf"(\basync\s+)?\bfunction\s+{re.escape(name)}\s*\(", src)
    if m:
        body = src.find("{", _scan(src, m.end(), ")"))
        return (m.group(1) or "") + src[body : _scan(src, body + 1, "}") + 1]
    return None


def driver_files() -> list[Path]:
    files = [*TESTS.rglob("*.mjs"), *TESTS.rglob("*.js"), *TESTS.rglob("test_*.py")]
    return sorted(p for p in files if p.name != Path(__file__).name and "node_modules" not in p.parts)


def offenders(files: list[Path] | None = None, root: Path = TESTS.parent) -> list[str]:
    """每个返回 Promise 的 waitForFunction 谓词，`相对路径:行号`。"""
    found: list[str] = []
    for path in files if files is not None else driver_files():
        src = path.read_text(encoding="utf-8")
        for m in CALL.finditer(src):
            start = m.end()
            arg = src[start : _scan(src, start, ",)")].strip()
            if IDENT.fullmatch(arg):
                arg = _definition(src, arg) or ""
            elif arg[:1] in "'\"`":
                arg = arg[1:-1]
            if OFFENDER.search(arg):
                line = src.count("\n", 0, m.start()) + 1
                found.append(f"{path.relative_to(root)}:{line}")
    return found


def test_scanner_flags_promise_predicates(tmp_path: Path) -> None:
    js = tmp_path / "d.mjs"
    js.write_text(
        "\n".join(
            [
                "await page.waitForFunction(() => import('/a.js').then((m) => m.ok()));",  # 1
                "await page.waitForFunction(async () => (await import('/a.js')).ok);",  # 2
                "await page.waitForFunction(() => new Promise((r) => r(false)), null, { timeout: 5 });",  # 3
                "const ready = () => fetch('/x').then((r) => r.ok);",
                "await page.waitForFunction(ready);",  # 5
                "await page.waitForFunction(`import('/a.js').then((m) => m.ok)`);",  # 6
                "await page.waitForFunction(() => Promise.resolve(false));",  # 7
                "await page.waitForFunction(\n  async\n  () => false,\n);",  # 8
                "await page.waitForFunction(() => /\\)/.test(document.title) && !!window.ok, null);",
                "await page.waitForFunction((o) => { const b = document.querySelector('#a'); return !!b && b !== o; }, old);",
                "await page.waitForFunction(() => `${'('}`.length > 0 && window.flag === 'then(');",
                "const sync = () => !!window.done;",
                "await page.waitForFunction(sync);",
                "await waitForPage(page, () => import('/a.js').then((m) => m.ok()));",
                "await page.waitForFunction(() => window.ready.\n  then((v) => v));",  # 18
            ]
        ),
        encoding="utf-8",
    )
    lines = [int(o.rsplit(":", 1)[1]) for o in offenders([js], tmp_path)]
    assert lines == [1, 2, 3, 5, 6, 7, 8, 18]


def test_no_driver_wait_returns_a_promise() -> None:
    found = offenders()
    assert not found, (
        f"{len(found)} 处 waitForFunction 的谓词返回 Promise，Playwright 不会等它（改用 page.evaluate 轮询，"
        "见 tests/support/browser_wait.mjs）:\n" + "\n".join(found)
    )
