"""测试夹具生成器：造一张"假卡"。

假卡 = 临时目录里的 `<卷>/DCIM/100RICOH/…`，内容是：

- 几对真实样本（从 `captures/a12-50/` 复制，找不到就退回合成的）；
- 合成的 DNG+JPG、只有 JPG、只有 DNG；
- EXIF 日期错乱的、完全没有 EXIF 的、方向为 6 的竖拍、同名不同内容（在 `101RICOH/`）、
  不同名同内容（应被去重）；
- 系统垃圾文件（`._xxx`、`Thumbs.db`、`MISC/`、`.Spotlight-V100/`），导入时必须忽略。

用法::

    python -m gxr_studio.devtools.synthetic_card /tmp/fake-volumes      # 在 /tmp/fake-volumes/NO NAME 下造卡
    GXR_STUDIO_VOLUMES=/tmp/fake-volumes uv run gxr-studio                # 让应用把它当成插着的卡
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .. import meta as metamod

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_SAMPLES = REPO_ROOT / "captures" / "a12-50" / "2026-10-19" / "stage2c-first-run"
LENS = "GR LENS A12 50mm F2.5 MACRO"
FALLBACK_MTIME = datetime(2026, 10, 20, 12, 0, 0)  # 没有 EXIF 的文件用它当文件时间


@dataclass
class SyntheticShot:
    directory: str
    stem: str
    kinds: tuple[str, ...]
    taken: datetime | None  # 期望的拍摄时间；None = 没有可用的 EXIF 日期
    mode: str = "ok"  # ok | garbled | none | real
    orientation: int = 1
    real: bool = False
    duplicate_of: str | None = None  # 内容与另一张完全相同
    note: str = ""

    @property
    def expected_date(self) -> str:
        """假定 undated_policy=mtime、批次偏移 0 时应落入的日期。"""
        return (self.taken or FALLBACK_MTIME).date().isoformat()


@dataclass
class SyntheticCard:
    root: Path  # 卡的根（卷）
    shots: list[SyntheticShot] = field(default_factory=list)
    junk: list[str] = field(default_factory=list)

    @property
    def volumes(self) -> Path:
        return self.root.parent

    @property
    def unique_shots(self) -> list[SyntheticShot]:
        return [s for s in self.shots if s.duplicate_of is None]

    def path(self, shot: SyntheticShot, kind: str) -> Path:
        ext = ".DNG" if kind == "dng" else ".JPG"
        return self.root / "DCIM" / shot.directory / f"{shot.stem}{ext}"


# ---- 图像 -------------------------------------------------------------------


def _pattern(seed: int, size: tuple[int, int]) -> Image.Image:
    rng = random.Random(seed)  # nosec B311  假卡的图案，不是密钥
    w, h = size
    img = Image.new("RGB", size, tuple(rng.randrange(40, 200) for _ in range(3)))
    d = ImageDraw.Draw(img)
    for _ in range(12):
        x0, y0 = rng.randrange(w), rng.randrange(h)
        x1, y1 = x0 + rng.randrange(20, w // 2), y0 + rng.randrange(20, h // 2)
        d.rectangle([x0, y0, x1, y1], fill=tuple(rng.randrange(256) for _ in range(3)))
    d.line([(0, 0), (w, h)], fill=(255, 255, 255), width=3)
    d.text((8, 8), f"#{seed}", fill=(255, 255, 255))
    return img


def _exif(taken: datetime | None, mode: str, orientation: int) -> Image.Exif | None:
    if mode == "none":
        return None
    exif = Image.Exif()
    exif[271] = "RICOH"
    exif[272] = "GXR A12"
    exif[274] = orientation
    sub = exif.get_ifd(0x8769)
    if mode == "garbled":
        exif[306] = "garbage"
        sub[36867] = "0000:00:00 00:00:00"
    else:
        stamp = taken.strftime("%Y:%m:%d %H:%M:%S")  # type: ignore[union-attr]
        exif[306] = stamp
        sub[36867] = stamp
        sub[36868] = stamp
    sub[34855] = 200
    sub[42036] = LENS
    return exif


def _write_jpg(path: Path, seed: int, shot: SyntheticShot) -> None:
    exif = _exif(shot.taken, shot.mode, shot.orientation)
    img = _pattern(seed, (640, 480))
    kw = {"exif": exif} if exif is not None else {}
    img.save(path, "JPEG", quality=80, **kw)


def _write_dng(path: Path, seed: int, shot: SyntheticShot) -> None:
    """合成的"DNG"：160×120 的 TIFF，带日期。够用来测配对、日期、缩略图；不含 CFA。"""
    exif = _exif(shot.taken, shot.mode, shot.orientation)
    img = _pattern(seed + 5000, (160, 120))
    kw = {"exif": exif} if exif is not None else {}
    img.save(path, "TIFF", **kw)


def _seed(stem: str) -> int:
    return int.from_bytes(hashlib.sha256(stem.encode()).digest()[:4], "big")


def _stamp(path: Path, when: datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


# ---- 卡 ---------------------------------------------------------------------


def make_card(
    volumes: Path,
    label: str = "NO NAME",
    *,
    samples: Path | None = DEFAULT_SAMPLES,
    real_pairs: int = 4,
) -> SyntheticCard:
    """在 `volumes/label` 下造一张卡，返回期望值清单。"""
    root = Path(volumes) / label
    d100 = root / "DCIM" / "100RICOH"
    d101 = root / "DCIM" / "101RICOH"
    d100.mkdir(parents=True)
    d101.mkdir(parents=True)
    card = SyntheticCard(root=root)

    # 1) 真实样本
    real_stems: list[str] = []
    if samples is not None and Path(samples).is_dir():
        for dng in sorted(Path(samples).glob("*.DNG"))[:real_pairs]:
            jpg = dng.with_suffix(".JPG")
            if not jpg.exists():
                continue
            shutil.copy2(dng, d100 / dng.name)
            shutil.copy2(jpg, d100 / jpg.name)
            m = metamod.read_jpeg_meta(jpg)
            card.shots.append(SyntheticShot("100RICOH", dng.stem, ("dng", "jpg"), m.taken, "real", real=True))
            real_stems.append(dng.stem)

    def add(
        directory: Any, stem: Any, kinds: Any, taken: Any, mode: str = "ok", orientation: int = 1, note: str = "", dup_of: Any = None
    ) -> Any:
        shot = SyntheticShot(directory, stem, tuple(kinds), taken, mode, orientation, False, dup_of, note)
        folder = root / "DCIM" / directory
        seed = _seed(directory + stem)
        if "jpg" in kinds:
            _write_jpg(folder / f"{stem}.JPG", seed, shot)
            _stamp(folder / f"{stem}.JPG", taken or FALLBACK_MTIME)
        if "dng" in kinds:
            _write_dng(folder / f"{stem}.DNG", seed, shot)
            _stamp(folder / f"{stem}.DNG", taken or FALLBACK_MTIME)
        card.shots.append(shot)
        return shot

    # 2) 合成：DNG+JPG 一对
    add("100RICOH", "R0000101", ("dng", "jpg"), datetime(2026, 10, 16, 9, 30, 0))
    add("100RICOH", "R0000102", ("dng", "jpg"), datetime(2026, 10, 17, 10, 15, 30))
    add("100RICOH", "R0000103", ("dng", "jpg"), datetime(2026, 10, 17, 23, 59, 59))
    add("100RICOH", "R0000104", ("dng", "jpg"), datetime(2026, 10, 18, 0, 0, 1))
    add("100RICOH", "R0000105", ("dng", "jpg"), datetime(2026, 10, 18, 14, 0, 0), orientation=6, note="竖拍，方向 6")
    # 3) 只有 JPG / 只有 DNG
    add("100RICOH", "R0000106", ("jpg",), datetime(2026, 10, 18, 15, 0, 0))
    add("100RICOH", "R0000107", ("jpg",), datetime(2026, 10, 19, 8, 0, 0))
    add("100RICOH", "R0000108", ("jpg",), datetime(2026, 10, 19, 9, 0, 0))
    add("100RICOH", "R0000109", ("dng",), datetime(2026, 10, 19, 10, 0, 0))
    add("100RICOH", "R0000110", ("dng",), datetime(2026, 10, 19, 11, 0, 0))
    add("101RICOH", "R0000111", ("dng",), datetime(2026, 10, 19, 12, 0, 0), note="另一个子目录")
    # 4) 日期有问题
    add("100RICOH", "R0000112", ("jpg",), None, mode="garbled", note="EXIF 日期乱码")
    add("100RICOH", "R0000113", ("dng", "jpg"), None, mode="garbled", note="EXIF 日期乱码（一对）")
    add("100RICOH", "R0000114", ("jpg",), None, mode="none", note="完全没有 EXIF")
    # 5) 同名不同内容：101RICOH 里的 R0000101 与 100RICOH 的同名，拍摄日期也相同
    add("101RICOH", "R0000101", ("jpg",), datetime(2026, 10, 16, 9, 30, 0), note="同名不同内容")
    # 6) 不同名同内容：R0000199.JPG 是 R0000106.JPG 的逐字节副本
    twin = d100 / "R0000199.JPG"
    shutil.copy2(d100 / "R0000106.JPG", twin)
    card.shots.append(SyntheticShot("100RICOH", "R0000199", ("jpg",), datetime(2026, 10, 18, 15, 0, 0), "ok", duplicate_of="R0000106"))

    # 7) 垃圾
    junk = [
        "DCIM/100RICOH/._R0000101.JPG",
        "DCIM/100RICOH/Thumbs.db",
        "DCIM/NOTE.TXT",
        "MISC/AUTPRINT.MRK",
        ".Spotlight-V100/Store-V2/x",
    ]
    for rel in junk:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        # `._xxx` 是 macOS 的 AppleDouble 文件：开头是它的魔数，这样才和真卡上看到的一样
        head = b"\x00\x05\x16\x07\x00\x02\x00\x00" if Path(rel).name.startswith("._") else b""
        p.write_bytes(head + b"junk " + rel.encode())
    card.junk = junk
    return card


def tree_snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """整棵目录的 内容哈希 + 大小 + mtime_ns（目录只记 mtime_ns）。用来证明"导入前后卡一字未动"。"""
    snap: dict[str, tuple[Any, ...]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        d = Path(dirpath)
        snap[d.relative_to(root).as_posix() or "."] = ("dir", os.stat(d).st_mtime_ns)
        for fn in sorted(filenames):
            p = d / fn
            st = os.stat(p)
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            snap[p.relative_to(root).as_posix()] = ("file", st.st_size, st.st_mtime_ns, h)
    return snap


def main() -> None:
    ap = argparse.ArgumentParser(description="造一张合成的假卡")
    ap.add_argument("dest", type=Path, help="放卡的目录（会在里面建 <label>/DCIM/…）")
    ap.add_argument("--label", default="NO NAME")
    ap.add_argument("--no-real", action="store_true", help="不复制 captures/ 里的真实样本")
    args = ap.parse_args()
    card = make_card(args.dest, args.label, samples=None if args.no_real else DEFAULT_SAMPLES)
    print(f"卡: {card.root}")
    print(f"照片 {len(card.shots)} 张（其中去重后 {len(card.unique_shots)} 张）")
    print(f"让应用看见它: GXR_STUDIO_VOLUMES={card.volumes}")


if __name__ == "__main__":
    main()
