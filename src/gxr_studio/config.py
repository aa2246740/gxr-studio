"""运行配置。默认值只在 `Settings.from_env()` 里出现；测试一律显式构造。"""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

MiB = 1024 * 1024


def default_home() -> Path:
    """应用数据目录（理光数据、我的配方放这里；M1 暂不写入）。"""
    env = os.environ.get("GXR_STUDIO_HOME")
    if env:
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "GXR Studio"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "GXR Studio"
    return Path.home() / ".local" / "share" / "gxr-studio"


def default_volumes_roots() -> tuple[Path, ...]:
    if sys.platform == "darwin":
        return (Path("/Volumes"),)
    if sys.platform.startswith("linux"):
        user = os.environ.get("USER", "")
        return tuple(Path(p) for p in (f"/media/{user}", f"/run/media/{user}", "/mnt"))
    return ()  # Windows 的卷枚举留到 M3 之后（platform/ 接口）


@dataclass(frozen=True)
class Settings:
    library_dir: Path
    home_dir: Path
    export_dir: Path
    volumes_roots: tuple[Path, ...]
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_hostnames: tuple[str, ...] = ("127.0.0.1", "localhost")
    # 导入前要求本地至少还剩这么多空间（在要复制的字节数之外）
    min_free_bytes: int = 256 * MiB
    # 没有 EXIF 拍摄时间时：mtime = 退回文件时间；unknown_folder = 放进"未知日期"
    undated_policy: str = "mtime"
    web_dir: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        pics = Path.home() / "Pictures"
        volumes = os.environ.get("GXR_STUDIO_VOLUMES")
        return cls(
            library_dir=Path(os.environ.get("GXR_STUDIO_LIBRARY") or pics / "GXR Studio"),
            home_dir=default_home(),
            export_dir=Path(os.environ.get("GXR_STUDIO_EXPORT") or pics / "GXR 导出"),
            volumes_roots=tuple(Path(p) for p in volumes.split(os.pathsep) if p) if volumes is not None else default_volumes_roots(),
            token=os.environ.get("GXR_STUDIO_TOKEN") or secrets.token_urlsafe(32),
            web_dir=Path(__file__).resolve().parents[2] / "web",
        )

    def with_(self, **changes) -> "Settings":
        from dataclasses import replace

        return replace(self, **changes)
