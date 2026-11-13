"""Parity for gxr-frame. Python `finish.sfnt` is the reference for `frame-sfnt`."""

from __future__ import annotations

from pathlib import Path

import pytest

from gxr_studio.finish.fonts import ASSETS, FONT_FILES
from gxr_studio.finish.sfnt import _cmap, _tables, read_font
from support.core_cli import error_of, run_json
from support.frame_font import empty_directory, font_without_unicode_cmap

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "NotoSansSC-Shanghai.otf"
CHARS = " A0H!'."


def _sfnt(path: Path, chars: str = CHARS):
    code, payload = run_json(["frame-sfnt", "--font", str(path), "--chars", chars])
    return error_of(code, payload), payload


def test_bundled_fonts_match_read_font() -> None:
    for name, filename in FONT_FILES.items():
        path = ASSETS / "fonts" / filename
        table = read_font(path)
        data = path.read_bytes()
        cmap = _cmap(data, _tables(data)["cmap"][0])
        err, got = _sfnt(path)
        assert err is None, (name, err)
        expected = table.to_json()
        assert got["upm"] == expected["upm"]
        assert got["ascender"] == expected["ascender"]
        assert got["notdef"] == expected["notdef"]
        assert got["cps"] == expected["cps"]
        assert got["adv"] == expected["adv"]
        assert got["box"] == expected["box"]
        assert got["ids"] == [cmap[cp] for cp in expected["cps"]]
        assert len(got["chars"]) == len(CHARS)
        for item, ch in zip(got["chars"], CHARS, strict=True):
            advance, box = table.glyph(ord(ch))
            assert item["cp"] == ord(ch)
            assert item["glyph"] == cmap.get(ord(ch), 0)
            assert item["adv"] == advance
            assert item["box"] == (list(box) if box is not None else None)


def test_sfnt_errors_match_python(tmp_path: Path) -> None:
    cases = [
        (b"ttcfrest", "value_error", "字体集合（.ttc）不读"),
        (b"OTTO", "struct_error", "unpack requires a buffer of 2 bytes"),
        (font_without_unicode_cmap(), "value_error", "cmap 里没有格式 4 或 12 的 Unicode 子表"),
        (empty_directory(), "key_error", "'head'"),
    ]
    for blob, kind, message in cases:
        path = tmp_path / f"{kind}-{len(blob)}.bin"
        path.write_bytes(blob)
        try:
            read_font(path)
        except Exception as exc:
            assert type(exc).__name__ in {"ValueError", "KeyError", "error"}
            assert str(exc) == message
        else:
            raise AssertionError(message)
        err, _payload = _sfnt(path, "A")
        assert err is not None
        assert err.kind == kind
        assert err.message == message


def test_cff_fixture_fails_like_read_font() -> None:
    """The Noto subset is CFF. `read_font` stops at the missing `loca` table."""
    if not FIXTURE.is_file():
        pytest.skip("Noto fixture is not in the checkout")
    with pytest.raises(KeyError, match="loca"):
        read_font(FIXTURE)
    err, _payload = _sfnt(FIXTURE, "上")
    assert err is not None
    assert err.kind == "key_error"
    assert err.message == "'loca'"
