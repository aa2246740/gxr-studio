"""半格 P1a 复审 r2 的回归：读文件出错不记成“不是一对”（N1）；单张读取每次运行只按名字查一次邻居（N3）。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from gxr_studio import halfframe as hf
from gxr_studio import halfframe_compose as hfc
from gxr_studio import halfframe_pairs as hfp
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.library.store import Library
from test_halfframe_p1a import _make_g250_diptych_jpg, _make_g250_first_mpo, _textured_half


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _dcim(volumes: Path, card: str = "CARD") -> Path:
    d = volumes / card / "DCIM" / "100RICOH"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_evaluate_candidate_checked_separates_read_errors_from_mismatches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s1 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=91)
    s2 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=92)
    sx = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=93)
    a = _make_g250_first_mpo(tmp_path / "R0009001.JPG", s1, exif_orientation=1)
    x = _make_g250_first_mpo(tmp_path / "R0009003.JPG", sx, exif_orientation=1)
    b = _make_g250_diptych_jpg(tmp_path / "R0009002.JPG", s1, s2, exif_orientation=1)

    dec, stable = hfp.evaluate_candidate_checked(a, b, orientation_a=1, orientation_b=1)
    assert dec is not None and stable
    dec, stable = hfp.evaluate_candidate_checked(x, b, orientation_a=1, orientation_b=1)
    assert dec is None and stable  # 读到了、指纹不符：可以记成“不是一对”

    real = hf.decode_draft_luma
    for broken in (a.name, b.name):

        def flaky(path: Any, broken: str = broken) -> Any:
            return (False, None) if Path(path).name == broken else real(path)

        monkeypatch.setattr(hf, "decode_draft_luma", flaky)
        dec, stable = hfp.evaluate_candidate_checked(a, b, orientation_a=1, orientation_b=1)
        assert dec is None and not stable, broken
    monkeypatch.setattr(hf, "decode_draft_luma", real)


def test_transient_read_failure_is_not_a_permanent_rejection(
    settings: Any, lib: Library, volumes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复审 r2 N1：导入时第 1 张一时读不出（OSError），恢复后重启要能配上，不能落盘成“不是一对”。"""
    s1 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=81)
    s2 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=82)
    dcim = _dcim(volumes)
    a = _make_g250_first_mpo(dcim / "R0008001.JPG", s1, exif_orientation=1)
    b = _make_g250_diptych_jpg(dcim / "R0008002.JPG", s1, s2, exif_orientation=1)
    a_sha, b_sha = _sha(a), _sha(b)
    real = hf.decode_draft_luma

    def flaky(path: Any) -> Any:
        return (False, None) if Path(path).name.startswith("R0008001") else real(path)

    monkeypatch.setattr(hf, "decode_draft_luma", flaky)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    sid_b = {s["files"]["jpg"]["sha256"]: s for s in lib.list_shots()}[b_sha]["shot_id"]
    assert "pair" not in (lib.get_shot(sid_b) or {})
    monkeypatch.setattr(hf, "decode_draft_luma", real)
    assert not lib._pairs.is_rejected(a_sha, b_sha)
    lib.close()

    lib2 = Library(settings.library_dir)
    lib2.open()
    try:
        assert lib2.halfframe_pair_pending(), "回填应当重新排上这一对"
        got = lib2.get_shot(sid_b)
        assert got is not None and got.get("pair", {}).get("role") == "second", got and got.get("pair")
    finally:
        lib2.close()


def test_single_shot_reads_look_up_neighbors_once_per_run_and_import_still_pairs(lib: Library, volumes: Path) -> None:
    """复审 r2 N3：未配对的第 1 张，单张读取每次运行只按名字查一次邻居；之后导入第 2 张照样配上。"""
    s1 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=61)
    s2 = _textured_half(hfc.FULL_HEIGHT, hfc.FULL_HALF_W, seed=62)
    a = _make_g250_first_mpo(_dcim(volumes, "CARD1") / "R0006001.JPG", s1, exif_orientation=1)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    sid_a = {s["files"]["jpg"]["sha256"]: s for s in lib.list_shots()}[_sha(a)]["shot_id"]

    qlog = lib.trace_queries()
    first = lib.get_shot(sid_a)
    assert first is not None and first.get("half_first") is True
    assert sum("s.name IN" in q for q in qlog) == 1, qlog
    qlog.clear()
    for _ in range(3):
        again = lib.get_shot(sid_a)
        assert again is not None and again.get("half_first") is True
    assert not any("s.name IN" in q for q in qlog), qlog

    b = _make_g250_diptych_jpg(_dcim(volumes, "CARD2") / "R0006002.JPG", s1, s2, exif_orientation=1)
    card2 = next(c for c in find_cards((volumes,)) if c.root.name == "CARD2")
    run_import(lib, card2, ImportOptions(min_free_bytes=0))
    got_a = lib.get_shot(sid_a)
    assert got_a is not None and got_a.get("pair", {}).get("role") == "first", got_a and got_a.get("pair")
    sid_b = {s["files"]["jpg"]["sha256"]: s for s in lib.list_shots()}[_sha(b)]["shot_id"]
    got_b = lib.get_shot(sid_b)
    assert got_b is not None and got_b.get("pair", {}).get("role") == "second"

    lib._pairs.mark_neighbors_checked("x" * 64)
    lib.rebuild()
    assert not lib._pairs.neighbors_checked("x" * 64)
