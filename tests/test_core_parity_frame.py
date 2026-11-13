"""Parity for gxr-frame: the Rust port of finish/sfnt, spec, layout, frame_core and the frame styles.

Python is the reference. Layout JSON is compared value by value with Python's types (int, float,
bool, str, key order); floats must be equal, not close. CJK metrics come from the Noto fixture
on both sides (Python through ``stamp._CJK_FONTS``, Rust through ``--font cjk=...``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gxr_studio.finish.fonts import ASSETS, FONT_FILES
from gxr_studio.finish.sfnt import _cmap, _tables, read_font
from support.core_cli import error_of, run_json
from support.frame_cases import (
    FIXTURE_CJK,
    cjk_fonts,
    font_args,
    font_pairs,
    frame_case,
    frame_cases,
    same,
    stamp_case,
    stamp_cases,
    write_json,
)
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
        {"style": "leica", "logo": "abc\n"},
        {"style": "leica", "logo": "abc\n\n"},
        {"style": "leica", "logo": "a" * 65},
        {"style": "leica", "logo": "ricoh", "show_logo": False},
        {"style": "leica", "logo": "none", "show_logo": True},
        {"version": True},
        {"version": 2.0},
        {"style": None},
        {"text": "😀" * 24},
        {"text": "😀" * 25},
        {"model": 1},
        {"logo_mode": "neon"},
        {"logo_size": 3},
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
        {"date_override": "2026-1-5"},
        {"date_override": "2026-01- 5"},
        {"date_override": "２０２６-01-05"},
        {"date_override": "2026-01-1٥"},
        {"date_override": "0000-01-01"},
        {"date_override": "2026-01-05 "},
        {"date_override": "2026-13-01"},
        {"content": []},
        {"font": {}},
        {"corner": ["br"]},
        {"size": 1},
        {"placement": []},
        {"text": None},
        {"show_time": 1},
    ]
    for raw in stamps:
        err, got = _spec("stamp-spec", tmp_path, raw)
        try:
            parsed = parse_stamp(raw)
        except EditError as exc:
            assert err is not None and err.kind == exc.code and err.message == exc.message, raw
        except TypeError as exc:
            assert err is not None and err.kind == "type_error" and err.message == str(exc), (raw, err)
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


CJK_CHARS = "上海北京H A0'é字"
CJK_PXS = [*range(1, 241), 255, 333, 512, 999, 1000, 1001, 2048, 4096]
# Manager amendment 2026-11-13 (UNIT-frame.md): Pillow measures CJK ink boxes after FreeType's
# Adobe CFF hinter; gxr-frame uses the unhinted control box (FreeType FT_LOAD_NO_HINTING, exactly).
# Example: 上 top 825 (Rust) vs 825.421875 hinted → 826 (Pillow). The hinter is not ported. Where a
# layout needs a CJK ink box, each box edge and each derived position may differ by at most 1 px;
# everything else stays exact. The desktop keeps Pillow's box; the phone accepts the 1-px difference.
CJK_BOX_PX = 1


def _cjk_python() -> tuple[list, list]:
    from gxr_studio.finish.fonts import FontMetrics

    with cjk_fonts((FIXTURE_CJK,)):
        m = FontMetrics()
        return [m.glyph("atkinson", ch, True) for ch in CJK_CHARS], [m.ascent("atkinson", px, True) for px in CJK_PXS]


def _cjk_rust() -> dict:
    code, got = run_json(["frame-cjk", "--font", str(FIXTURE_CJK), "--chars", CJK_CHARS, "--px", ",".join(map(str, CJK_PXS))])
    assert error_of(code, got) is None
    return got


def test_cjk_fallback_advances_and_ascents_match_pillow() -> None:
    """`FontMetrics` with the Noto fixture as the CJK face: advance at 1000 px and ascent per px, exact."""
    glyphs, ascents = _cjk_python()
    got = _cjk_rust()
    for ch, (adv, _box), (gadv, _gbox) in zip(CJK_CHARS, glyphs, got["glyphs"], strict=True):
        assert gadv == adv, (ch, gadv, adv)
    for px, want, value in zip(CJK_PXS, ascents, got["ascents"], strict=True):
        assert value == want, (px, value, want)


def test_cjk_fallback_ink_boxes_are_within_one_pixel_of_pillow() -> None:
    """Each CJK box edge: Rust (unhinted) and Pillow (hinted) differ by at most CJK_BOX_PX."""
    glyphs, _ascents = _cjk_python()
    got = _cjk_rust()
    differ = []
    for ch, (_adv, box), (_gadv, gbox) in zip(CJK_CHARS, glyphs, got["glyphs"], strict=True):
        assert (gbox is None) == (box is None), (ch, gbox, box)
        if box is None:
            continue
        for edge, (a, b) in enumerate(zip(box, gbox, strict=True)):
            assert abs(a - b) <= CJK_BOX_PX, (ch, edge, a, b)
        if list(box) != gbox:
            differ.append((ch, list(box), gbox))
    print(f"CJK ink boxes within {CJK_BOX_PX} px of Pillow: {len(differ)} of {len(CJK_CHARS)} glyph boxes differ: {differ}")


def _batch(tmp_path: Path, frames: list, stamps: list, *, cjk: bool = True) -> dict:
    cases = write_json(tmp_path / "cases.json", {"frames": frames, "stamps": stamps})
    code, got = run_json(["frame-batch", "--cases", str(cases), *font_args(cjk=cjk)], timeout=900)
    assert error_of(code, got) is None, got
    return got


def _compare(label: str, cases: list, want: list, got: list) -> None:
    assert len(got) == len(want) == len(cases)
    for i, (case, expected, value) in enumerate(zip(cases, want, got, strict=True)):
        same(expected, value, f"{label} #{i} {case['request'].get('w')}x{case['request'].get('h')} {case['spec'].get('style', '')}")


def test_frame_layouts_match_python_for_every_style(tmp_path: Path) -> None:
    from gxr_studio.finish.spec import FRAME_STYLES

    with cjk_fonts((FIXTURE_CJK,)):
        cases, want = frame_cases()
    styles = {c["spec"]["style"] for c in cases}
    assert styles == FRAME_STYLES
    assert len(cases) > 4000
    got = _batch(tmp_path, cases, [])
    _compare("frame", cases, want, got["frames"])


def _stamp_split() -> tuple[tuple[list, list], tuple[list, list]]:
    """Stamp cases whose text needs the CJK face (any code point > 127) measure CJK ink boxes."""
    with cjk_fonts((FIXTURE_CJK,)):
        cases, want = stamp_cases()
    plain: tuple[list, list] = ([], [])
    cjk: tuple[list, list] = ([], [])
    for case, expected in zip(cases, want, strict=True):
        layout = expected if isinstance(expected, dict) and "ops" in expected else None
        texts = [op["text"] for op in layout["ops"] if op["k"] == "text"] if layout else []
        group = cjk if any(ord(ch) > 127 for t in texts for ch in t) else plain
        group[0].append(case)
        group[1].append(expected)
    return plain, cjk


def test_stamp_layouts_match_python(tmp_path: Path) -> None:
    (cases, want), (cjk_cases, _cjk_want) = _stamp_split()
    assert len(cases) > 1000 and cjk_cases
    got = _batch(tmp_path, [], cases)
    _compare("stamp", cases, want, got["stamps"])


def _near(a: object, b: object, path: str) -> bool:
    """Like `same`, but ints may differ by CJK_BOX_PX. Returns whether anything differed."""
    if isinstance(a, bool) or isinstance(b, bool) or not (isinstance(a, int) and isinstance(b, int)):
        if isinstance(a, dict):
            assert isinstance(b, dict) and list(a) == list(b), (path, a, b)
            return any([_near(a[k], b[k], f"{path}.{k}") for k in a])
        if isinstance(a, list):
            assert isinstance(b, list) and len(a) == len(b), (path, a, b)
            return any([_near(x, y, f"{path}[{i}]") for i, (x, y) in enumerate(zip(a, b, strict=True))])
        same(a, b, path)
        return False
    assert abs(a - b) <= CJK_BOX_PX, (path, a, b)
    return a != b


def test_cjk_stamp_layouts_are_within_one_pixel(tmp_path: Path) -> None:
    """Stamps that need a CJK ink box: every int (box edge, rect, column, row, point) within 1 px;
    text, font, size, colours, glow and types exact. Counts the cases that differ at all."""
    _plain, (cases, want) = _stamp_split()
    got = _batch(tmp_path, [], cases)["stamps"]
    assert len(got) == len(want) == len(cases)
    differ = 0
    for i, (case, expected, value) in enumerate(zip(cases, want, got, strict=True)):
        if isinstance(expected, dict) and "ops" in expected:
            assert isinstance(value, dict) and value.get("px") == expected["px"], (i, case, value)
        differ += _near(expected, value, f"cjk stamp #{i} {case['request']['w']}x{case['request']['h']}")
    print(f"CJK stamp layouts: {differ} of {len(cases)} differ by 1 px, the rest are exact")
    assert differ < len(cases)


def test_missing_cjk_face_is_the_python_error(tmp_path: Path) -> None:
    """No CJK face configured: CJK text fails with `missing_cjk`, Latin text still lays out."""
    from gxr_studio.finish.spec import FrameSpec, StampSpec

    meta = {"lens_short": "A12 50mm", "date": "2026.10.23"}
    with cjk_fonts(()):
        frames = [
            frame_case(900, 600, FrameSpec(style="leica", text="上海"), meta),
            frame_case(900, 600, FrameSpec(style="cinema", text="北京"), meta),
            frame_case(900, 600, FrameSpec(style="leica", text="Café"), meta),
            frame_case(900, 600, FrameSpec(style="gallery"), meta),
        ]
        stamps = [
            stamp_case(900, 600, StampSpec(enabled=True, content="text", text="上海"), None),
            stamp_case(900, 600, StampSpec(enabled=True, content="text", text="é"), None),
            stamp_case(900, 600, StampSpec(enabled=True, content="text", text="Hello"), None),
        ]
    kinds = [w.get("error", {}).get("kind") if isinstance(w, dict) else None for _c, w in frames + stamps]
    assert kinds.count("missing_cjk") == 4
    got = _batch(tmp_path, [c for c, _w in frames], [c for c, _w in stamps], cjk=False)
    _compare("frame", [c for c, _ in frames], [w for _, w in frames], got["frames"])
    _compare("stamp", [c for c, _ in stamps], [w for _, w in stamps], got["stamps"])


def test_single_layout_commands_match_the_batch(tmp_path: Path) -> None:
    from datetime import datetime

    from gxr_studio.finish.spec import FrameSpec, StampSpec

    with cjk_fonts((FIXTURE_CJK,)):
        frame, frame_want = frame_case(1200, 800, FrameSpec(style="leica", text="上海", show_date=True), {"date": "2026.10.24"})
        stamp, stamp_want = stamp_case(
            800,
            1200,
            StampSpec(enabled=True, placement="border", corner="tl"),
            datetime(2026, 10, 23, 17, 41),
            FrameSpec(style="instant", text="Hi"),
        )
    for command, case, want in (("frame-layout", frame, frame_want), ("stamp-layout", stamp, stamp_want)):
        meta = write_json(tmp_path / f"{command}-meta.json", case["request"])
        spec = write_json(tmp_path / f"{command}-spec.json", case["spec"])
        code, got = run_json([command, "--meta", str(meta), "--spec", str(spec), *font_args()])
        assert error_of(code, got) is None, got
        same(want, got, command)


def test_wasm_layouts_match_native(tmp_path: Path) -> None:
    from gxr_studio.finish.spec import FRAME_STYLES, FrameSpec, StampSpec
    from support.wasm_check import run_area

    entries = []

    def add(label: str, kind: str, request: dict, spec: dict, *, cjk: bool = True) -> None:
        meta = write_json(tmp_path / f"{label}-meta.json", request)
        spec_path = write_json(tmp_path / f"{label}-spec.json", spec)
        entries.append({"label": label, "kind": kind, "meta": str(meta), "spec": str(spec_path), "fonts": font_pairs(cjk=cjk)})

    meta = {"lens_short": "A12 50mm", "aperture": "f/2.5", "shutter": "1/250s", "iso": "ISO 200", "date": "2026.10.23", "frame_no": "0102"}
    for style in sorted(FRAME_STYLES - {"none"}):
        add(f"{style}-wide", "frame", {"w": 1200, "h": 800, "meta": meta}, FrameSpec(style=style, text="上海").to_json())
        add(f"{style}-tall", "frame", {"w": 600, "h": 900, "meta": meta}, FrameSpec(style=style, output_ratio="original").to_json())
    add(
        "leica-logo",
        "frame",
        {
            "w": 900,
            "h": 600,
            "meta": meta,
            "logos": [{"id": "m", "name": "M", "w": 300, "h": 100, "coverage": 0.3, "opaque": False, "lum": 20.0}],
        },
        FrameSpec(style="leica", logo="m").to_json(),
    )
    add("stamp-photo", "stamp", {"w": 900, "h": 600, "taken": [2026, 10, 23, 17, 41]}, StampSpec(enabled=True, show_time=True).to_json())
    add(
        "stamp-border",
        "stamp",
        {"w": 900, "h": 600, "taken": [2026, 10, 23, 17, 41], "frame": FrameSpec(style="instant").to_json()},
        StampSpec(enabled=True, placement="border").to_json(),
    )
    add("stamp-cjk", "stamp", {"w": 900, "h": 600}, StampSpec(enabled=True, content="text", text="上海", font="sans").to_json())
    add("missing-cjk", "stamp", {"w": 900, "h": 600}, StampSpec(enabled=True, content="text", text="上海").to_json(), cjk=False)
    add("bad-spec", "frame", {"w": 900, "h": 600}, {"style": "nope"})
    add("bad-size", "frame", {"w": 0, "h": 600}, FrameSpec(style="gallery").to_json())
    # Size arithmetic (UNIT-COMMON amendment 2026-11-14): huge dimensions are one identical error.
    for w, h in ((4294967295, 4294967295), (2147483648, 1), (1, 2147483648), (65536, 1), (9007199254740993, 3)):
        add(f"huge-frame-{w}x{h}", "frame", {"w": w, "h": h}, FrameSpec(style="film").to_json())
        add(f"huge-stamp-{w}x{h}", "stamp", {"w": w, "h": h, "taken": [2026, 1, 2, 3, 4]}, StampSpec(enabled=True).to_json())
    manifest = write_json(tmp_path / "frame-wasm.json", entries)
    proc = run_area("frame", [manifest], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_crate_never_references_the_ricoh_wordmark() -> None:
    """PORT-WAVE-1 §A finish 4: the built-in mark is a box; the PNG is not loaded or named."""
    root = Path(__file__).resolve().parents[1] / "core" / "crates"
    for folder in ("gxr-frame", "gxr-wasm", "gxr-cli"):
        for path in (root / folder).rglob("*.rs"):
            assert "ricoh.png" not in path.read_text(encoding="utf-8"), path
