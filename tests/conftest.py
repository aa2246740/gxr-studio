"""测试总则：

- 任何测试都不能碰 /Volumes 下的真实卷（用户的卡就插在读卡器里）：`forbid_volumes` 在读写、列目录时直接抛错；
- 默认图库 `~/Pictures/GXR Studio`、导出目录、应用数据目录都被保护（写入即拒绝），测试一律用临时目录；
- 全程禁止联网（M1 验收 8）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gxr_studio import audit

REAL_HOME = Path.home()
for _p in (
    REAL_HOME / "Pictures" / "GXR Studio",
    REAL_HOME / "Pictures" / "GXR 导出",
    REAL_HOME / "Library" / "Application Support" / "GXR Studio",
):
    audit.protect(_p)
audit.forbid_volumes()
audit.forbid_network(True)

sys.path.insert(0, str(Path(__file__).parent))

from gxr_studio.config import Settings  # noqa: E402
from gxr_studio.devtools.synthetic_card import make_card  # noqa: E402
from gxr_studio.library.store import Library  # noqa: E402
from support import samples  # noqa: E402

PORT = 8765


def pytest_configure(config):
    config.addinivalue_line("markers", f"{samples.REQUIRED_MARK}(reason): 缺真实样本，GXR_REQUIRE_SAMPLES=1 时在 setup 失败")


def pytest_runtest_setup(item):
    samples.fail_if_marked(item)


@pytest.fixture(scope="session")
def approved_frame_layouts():
    """摘要、独立参考和 JS 对照共用一份实际布局，只在这里注册一次。"""
    from test_frame_reference import build_frame_layouts

    return build_frame_layouts()


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    if sys.platform == "win32":
        for name, value in {
            "USERPROFILE": str(home),
            "HOMEDRIVE": home.drive,
            "HOMEPATH": str(home)[len(home.drive) :],
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
        }.items():
            monkeypatch.setenv(name, value)
    for name in (
        "GXR_STUDIO_HOME",
        "GXR_STUDIO_LIBRARY",
        "GXR_STUDIO_EXPORT",
        "GXR_STUDIO_VOLUMES",
        "GXR_STUDIO_TOKEN",
        "GXR_STUDIO_SENTRY_DSN",
        "GXR_STUDIO_SENTRY_ENVIRONMENT",
        "GXR_STUDIO_SENTRY_RELEASE",
    ):
        monkeypatch.delenv(name, raising=False)
    audit.network_attempts(clear=True)
    yield
    assert audit.network_attempts(clear=True) == [], "测试期间出现了联网尝试"


@pytest.fixture
def volumes(tmp_path) -> Path:
    v = tmp_path / "volumes"
    v.mkdir()
    return v


@pytest.fixture
def card(volumes):
    """全合成的假卡（不含大文件）。"""
    return make_card(volumes, samples=None)


@pytest.fixture
def real_card(volumes):
    """含 4 对真实样本的假卡（约 90 MB）；样本从 GXR_STUDIO_TEST_PARENT 找（见 support/samples.py），找不到就跳过。"""
    folder = samples.a12_50_samples()
    if not folder.is_dir():
        samples.missing("captures/a12-50 样本不在本机")
    return make_card(volumes, samples=folder)


@pytest.fixture
def settings(tmp_path, volumes) -> Settings:
    return Settings(
        library_dir=tmp_path / "library",
        home_dir=tmp_path / "app-home",
        export_dir=tmp_path / "exports",
        volumes_roots=(volumes,),
        token="test-token-123",
        port=PORT,
        min_free_bytes=0,
    )


@pytest.fixture
def lib(settings) -> Library:
    library = Library(settings.library_dir).open()
    yield library
    library.close()
