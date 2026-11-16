"""UI-M3-GROUP：复制 / 套用到这一组的纯函数和字（Node 直接跑 TypeScript），500 张的时间片，以及拍摄时间和桌面同源。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from support import samples
from support.export_pipeline_cases import check, same_redacted
from support.speed import assert_fast

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "tests" / "support" / "mobile_ui_group_unit.mjs"
TIMING = REPO / "tests" / "support" / "mobile_ui_group_timing.mjs"
CAPTURE = REPO / "tests" / "support" / "mobile_ui_group_capture.mjs"
SAMPLES = samples.a12_50_samples()


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    return node


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_node(), "--experimental-strip-types", *args, str(script)]
        if args == ("--test",)
        else [_node(), "--experimental-strip-types", str(script), *args],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_mobile_ui_group_paste_rules_and_messages() -> None:
    """套用的规则（每种跳过、配方整组、memory、来源不算目标）、撤销（rev、最早的样子、20 批）、字和桌面逐句一样。"""
    result = _run(UNIT, "--test")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ℹ fail 0" in result.stdout or "# fail 0" in result.stdout, result.stdout


@pytest.mark.exclusive  # 500 张套用一次，主线程每一片不超过 8 ms
def test_mobile_ui_group_paste_500_photos_in_slices() -> None:
    result = _run(TIMING)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    # 来源不在目标里；每 10 张有 1 张只有 JPG，只套用调整。
    assert report["results"] == report["applied"] == 500
    assert report["recipe"] == 450
    print(f"500 photos: {report}")
    assert_fast(report["maxSliceMs"], 8.0, "500 张套用时主线程最长的一片（毫秒）")


def _taken_ms(taken: datetime) -> int:
    return int(taken.timestamp()) * 1000


def _subsec_ms(path: Path) -> int:
    """桌面 hdr/capture.py 读的那一项 SubSecTimeOriginal（37521），按手机的规则取前三位。"""
    from PIL import Image

    with Image.open(path) as im:
        try:
            sub = im.getexif().get_ifd(0x8769)
        except Exception:
            sub = {}
        raw = sub.get(37521)
    digits = re.sub(r"\D", "", raw.decode("ascii", "ignore") if isinstance(raw, bytes) else str(raw or ""))[:3]
    return int(digits.ljust(3, "0")) if digits else 0


def test_mobile_ui_group_capture_time_matches_desktop(tmp_path: Path) -> None:
    """分组的唯一输入：手机的拍摄时间和桌面 photo_meta 的 taken 同源（本地时间）。

    桌面的 taken 到秒为止（parse_exif_datetime 只取前 19 个字符），手机多带了 SubSecTimeOriginal 的毫秒，
    所以比的是「手机 = taken + 同一张照片的 SubSec」。没有 EXIF 时间时两边都用文件修改时间。"""
    from gxr_studio.cards.importer import decide_date
    from gxr_studio.meta import read_dng_meta, read_jpeg_meta

    pairs = sorted(SAMPLES.glob("*.DNG")) if SAMPLES.is_dir() else []
    pairs = [dng for dng in pairs if dng.with_suffix(".JPG").is_file()]
    samples.need_sample(bool(pairs), f"need DNG+JPG pairs in {SAMPLES}")
    if not pairs:
        pytest.skip(f"need DNG+JPG pairs in {SAMPLES}")
    # 没有拍摄时间的副本：日期字段写成全零（桌面当作没有，手机也认不出），修改时间定一个。
    blank = tmp_path / "R9990005.JPG"
    data = re.sub(rb"\d{4}:\d\d:\d\d \d\d:\d\d:\d\d", b"0000:00:00 00:00:00", pairs[0].with_suffix(".JPG").read_bytes())
    blank.write_bytes(data)
    os.utime(blank, ns=(1_760_000_000_123_000_000, 1_760_000_000_123_000_000))
    items = [{"path": str(dng.with_suffix(".JPG")), "kind": "jpg"} for dng in pairs]
    items += [{"path": str(dng), "kind": "dng"} for dng in pairs]
    items.append({"path": str(blank), "kind": "jpg"})
    manifest = tmp_path / "capture.json"
    manifest.write_text(json.dumps(items), encoding="utf-8")
    result = _run(CAPTURE, str(manifest))
    check(result.returncode == 0, "capture helper ran")
    phone = json.loads(result.stdout.strip().splitlines()[-1])
    want: list[int] = []
    for item in items:
        path = Path(item["path"])
        meta = read_jpeg_meta(path) if item["kind"] == "jpg" else read_dng_meta(path)
        if meta.taken is not None:
            want.append(_taken_ms(meta.taken) + _subsec_ms(path))
            continue
        _date, captured_at, source = decide_date(None, path.stat().st_mtime_ns, timedelta(0), "mtime")
        check(source == "mtime" and captured_at is not None, "desktop falls back to mtime")
        assert captured_at is not None
        want.append(int(datetime.fromisoformat(captured_at).timestamp() * 1000))
    got = [row["ms"] for row in phone]
    same_redacted(got, want, "phone capture ms == desktop taken (+SubSec) or mtime")
    # 副本那一张走的是修改时间（两边都没有 EXIF 时间）。
    check(phone[-1]["exif"] is None and phone[-1]["ms"] == phone[-1]["lastModified"], "blank copy uses lastModified")
    check(all(row["exif"] is not None for row in phone[:-1]), "every sample has an EXIF time")
