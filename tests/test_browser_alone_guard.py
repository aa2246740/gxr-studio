"""browser_alone 守门：碰共用资源的浏览器测试必须标 browser_alone。

合并检查外壳设 GXR_BROWSER_LANES=2 时，browser_serial 分两道并行跑，只有 browser_alone 等两道都跑完再单独跑。
会被并行打乱的共用资源里，grep 找得到的是 mobile/dist（开发守门往里写诱饵文件，四个测试读生产包）
和 /tmp/gxr-heavy.lock（exclusive 的锁）。测试文件本身或它点名加载的 tests/support/*.mjs 驱动里出现这些，
文件里就必须有 browser_alone。其余共用资源的盘点见分道那次的报告。
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SUPPORT = TESTS / "support"
SHARED = re.compile(r"mobile/dist|[\"']mobile[\"']\s*/\s*[\"']dist[\"']|[\"']mobile[\"'],\s*[\"']dist[\"']|gxr-heavy\.lock")
DRIVER = re.compile(r"([A-Za-z0-9_]+\.mjs)")


def _browser_files() -> list[Path]:
    files = (p for p in TESTS.glob("test_*.py") if not p.name.startswith("test_run_tests"))
    return sorted(p for p in files if "browser_serial" in p.read_text(encoding="utf-8"))


def _hits(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    found = [f"{path.name}: {m.group(0)}" for m in SHARED.finditer(text)]
    for name in sorted(set(DRIVER.findall(text))):
        driver = SUPPORT / name
        if driver.is_file():
            found += [f"{path.name} -> {name}: {m.group(0)}" for m in SHARED.finditer(driver.read_text(encoding="utf-8"))]
    return found


def test_browser_tests_that_touch_shared_resources_run_alone() -> None:
    missing = [hit for path in _browser_files() if "browser_alone" not in path.read_text(encoding="utf-8") for hit in _hits(path)]
    assert not missing, "这些浏览器测试碰到共用资源，要在 pytestmark 里加 pytest.mark.browser_alone：\n" + "\n".join(missing)


def test_browser_alone_is_only_used_next_to_browser_serial() -> None:
    texts = {p.name: p.read_text(encoding="utf-8") for p in TESTS.glob("test_*.py")}
    stray = [name for name, text in texts.items() if "mark.browser_alone" in text and "browser_serial" not in text]
    assert not stray, stray


def test_the_guard_sees_the_known_shared_users() -> None:
    names = {path.name for path in _browser_files() if _hits(path)}
    known = {
        "test_mobile_dev_guard.py",
        "test_mobile_gpu_device_loss_browser.py",
        "test_mobile_perf_startup_browser.py",
        "test_mobile_desktop_layout_browser.py",
        "test_mobile_polish3_browser.py",
    }
    assert known <= names, sorted(known - names)
