"""Parity for gxr-frame. Python `finish.sfnt` is the reference for `frame-sfnt`."""

from __future__ import annotations

import json
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


def _spec(command: str, tmp_path: Path, raw: object):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    code, payload = run_json([command, "--spec", str(path)])
    return error_of(code, payload), payload


def test_frame_and_stamp_specs_match_python(tmp_path: Path) -> None:
    from gxr_studio.finish.spec import factory_frame, factory_stamp, parse_frame, parse_stamp, stored_frame
    from gxr_studio.render.params import EditError

    frames = [
        None,
        {},
        {"style": "gallery", "color": "black", "weight": "thin", "output_ratio": "original"},
        {"style": "leica", "show_logo": False, "show_date": True, "text": "Hello"},
        {"style": "instant", "fmt": "instax_mini", "text": "上海"},
        {"style": "thin_keyline"},
        {"style": "postage_stamp", "logo": "my_logo", "logo_mode": "mono", "logo_size": "large", "model": False},
        {"style": "nope"},
        {"color": "red"},
        {"weight": "huge"},
        {"output_ratio": "2:1"},
        {"text": "字" * 30},
        {"version": 1, "style": "gallery"},
        {"style": "gallery", "show_logo": 1},
        {"style": "film", "fmt": "polaroid"},
        {"style": "gallery", "extra": 1},
    ]
    for raw in frames:
        err, got = _spec("frame-spec", tmp_path, raw)
        try:
            parsed = parse_frame(raw)
        except EditError as exc:
            assert err is not None and err.kind == exc.code and err.message == exc.message
        else:
            assert err is None
            assert got == (None if parsed is None else parsed.to_json())
        stored_err, stored_got = _spec("frame-stored", tmp_path, raw)
        assert stored_err is None
        kept = stored_frame(raw)
        assert stored_got == (None if kept is None else kept.to_json())
    assert factory_frame().to_json()["style"] == "none"

    stamps = [
        None,
        {},
        {"enabled": True, "placement": "border", "content": "date+text", "text": "Paris", "date_override": "2024-02-29"},
        {"content": "nope"},
        {"font": "comic"},
        {"date_override": "2026-99-99"},
        {"date_override": "2026-02-29"},
        {"date_format": "nope"},
        {"text": "W" * 25},
        {"glow": "yes"},
        {"date_override": 1},
    ]
    for raw in stamps:
        err, got = _spec("stamp-spec", tmp_path, raw)
        try:
            parsed = parse_stamp(raw)
        except EditError as exc:
            assert err is not None and err.kind == exc.code and err.message == exc.message, raw
        else:
            assert err is None, raw
            assert got == (None if parsed is None else parsed.to_json())
    assert "placement" not in factory_stamp().to_json()


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
