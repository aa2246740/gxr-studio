"""A1a 的合成卡和合成卷信息（设计 §5.6 第四道）。

- 卡上的每个字节都在测试里现场生成，放在 `tmp_path` 下；没有任何理光文件、切片或它们的 hex/base64；
- 照片是固定长度的随机字节（JPG 带 JPEG 开头）：同名、同长度、同 mtime、内容不同的两张卡（ID01）很好造；
- `fake_dat` / `fake_gxrfx` 只是带魔数的随机字节，给私有字节检查用，不是相机能读的文件；
- `FakeProbe` 代替 statfs 和 diskutil：可以指定卷 UUID（含缺失、空值、失败、超时、格式错），也能模拟重新挂载。
"""

from __future__ import annotations

import os
import plistlib
import random
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from gxr_studio import audit
from gxr_studio.cards.session import StatFs, VolumeProbe

FIXED_MTIME = datetime(2026, 10, 20, 12, 0, 0)
PHOTO_BYTES = 6000
DNG_BYTES = 9000


def _rand(seed: Any, n: int) -> bytes:
    return random.Random(str(seed)).randbytes(n)  # nosec B311  测试夹具的内容，不是密钥


def jpeg_bytes(seed: Any, n: int = PHOTO_BYTES) -> bytes:
    return b"\xff\xd8\xff\xfe\x00\x10synthetic-jpg\x00" + _rand(("jpg", seed), n - 22) + b"\xff\xd9"


def dng_bytes(seed: Any, n: int = DNG_BYTES) -> bytes:
    """不是 TIFF 的随机字节：导入只关心内容哈希，读元数据会直接放弃。"""
    return b"FAKE-DNG" + _rand(("dng", seed), n - 8)


def avi_bytes(seed: Any, n: int = 5000) -> bytes:
    body = b"AVI LIST" + _rand(("avi", seed), n - 16)
    return b"RIFF" + (len(body)).to_bytes(4, "little") + body


def fake_dat(seed: Any = 0, n: int = 89936) -> bytes:
    """开头是 GXRCE001 的随机字节：只为了让私有字节检查有东西可拦。"""
    return b"GXRCE001" + _rand(("dat", seed), n - 8)


def fake_gxrfx(seed: Any = 0, n: int = 4096) -> bytes:
    data = bytearray(_rand(("fx", seed), n))
    data[100:108] = b"GXUI140\0"
    return bytes(data)


def stamp(path: Path, when: datetime = FIXED_MTIME) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


@dataclass
class FakeCard:
    root: Path
    files: dict[str, bytes] = field(default_factory=dict)

    def path(self, rel: str) -> Path:
        return self.root / rel

    def write(self, rel: str, data: bytes, when: datetime = FIXED_MTIME) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        stamp(p, when)
        self.files[rel] = data
        return p

    @property
    def photo_paths(self) -> list[str]:
        return sorted(r for r in self.files if r.upper().endswith((".JPG", ".DNG")))


def make_fake_card(
    volumes: Path,
    label: str = "NO NAME",
    *,
    seed: Any = 0,
    pairs: int = 2,
    jpg_only: int = 1,
    videos: int = 0,
    dcim_extra: dict[str, bytes] | None = None,
    root_extra: dict[str, bytes] | None = None,
) -> FakeCard:
    """`volumes/label/DCIM/100RICOH/` 下：`pairs` 对 DNG+JPG、`jpg_only` 张只有 JPG、`videos` 段 .AVI。
    同一组参数、不同 `seed`：路径、长度、mtime 全相同，内容不同。"""
    card = FakeCard(Path(volumes) / label)
    (card.root / "DCIM" / "100RICOH").mkdir(parents=True)
    n = 1
    for _ in range(pairs):
        card.write(f"DCIM/100RICOH/R00100{n:02d}.JPG", jpeg_bytes((seed, n)))
        card.write(f"DCIM/100RICOH/R00100{n:02d}.DNG", dng_bytes((seed, n)))
        n += 1
    for _ in range(jpg_only):
        card.write(f"DCIM/100RICOH/R00100{n:02d}.JPG", jpeg_bytes((seed, n)))
        n += 1
    for _ in range(videos):
        card.write(f"DCIM/100RICOH/R00100{n:02d}.AVI", avi_bytes((seed, n)))
        n += 1
    for rel, data in (dcim_extra or {}).items():
        card.write(f"DCIM/{rel}", data)
    for rel, data in (root_extra or {}).items():
        card.write(rel, data)
    return card


def replace_card(card: FakeCard, *, seed: Any, **kw: Any) -> FakeCard:
    """拔掉这张卡、在同一个挂载路径插上另一张：同样的文件名、长度、mtime，内容不同。"""
    audit.unprotect(card.root)  # 发现卡时登记的写保护；这里是在模拟"换了一张卡"
    shutil.rmtree(card.root)
    return make_fake_card(card.root.parent, card.root.name, seed=seed, **kw)


class FakeProbe(VolumeProbe):
    """合成的 statfs + diskutil。`uuids[挂载路径]` 决定 diskutil 的回答：

    - UUID 字符串：正常；
    - None：plist 里没有 VolumeUUID；"" ：空值；
    - "fail"：退出码 1；"timeout"：超时；"garbage"：不是 plist；"wrong-mount"：plist 的挂载点对不上。
    """

    def __init__(self) -> None:
        super().__init__(runner=self._run)
        self.uuids: dict[str, Any] = {}
        self.generation: dict[str, int] = {}
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def set_uuid(self, root: Path, value: Any) -> None:
        self.uuids[str(root)] = value

    def remount(self, root: Path) -> None:
        """同一个挂载路径重新挂载：f_fsid、设备名都变。"""
        self.generation[str(root)] = self.generation.get(str(root), 0) + 1

    def statfs(self, path: str) -> StatFs:
        os.lstat(path)
        gen = self.generation.get(path, 0)
        return StatFs((gen, 77), "msdos", path, f"/dev/disk9s{gen}")

    def _run(self, args: list[str], **kw: Any) -> Any:
        self.calls.append((list(args), dict(kw)))
        mount = args[-1]
        value = self.uuids.get(mount)
        if value == "timeout":
            raise subprocess.TimeoutExpired(args, kw.get("timeout", 0))
        if value == "fail":
            return subprocess.CompletedProcess(args, 1, b"", b"error")
        if value == "garbage":
            return subprocess.CompletedProcess(args, 0, b"not a plist", b"")
        info: dict[str, Any] = {"MountPoint": "/elsewhere" if value == "wrong-mount" else mount, "VolumeName": Path(mount).name}
        if value not in (None, "wrong-mount"):
            info["VolumeUUID"] = value
        elif value == "wrong-mount":
            info["VolumeUUID"] = "11111111-2222-3333-4444-555555555555"
        return subprocess.CompletedProcess(args, 0, plistlib.dumps(info), b"")
