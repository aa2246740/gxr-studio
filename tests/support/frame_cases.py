"""Frame and stamp layout cases for the gxr-frame parity tests.

The meta, spec and size builders come from the existing layout oracles
(``test_frame_reference`` and ``test_finish_layout_parity``); nothing here edits them.
Every case is synthetic: sizes, meta strings and logo descriptions only.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from gxr_studio.finish import stamp as stamp_mod
from gxr_studio.finish.fonts import ASSETS, FONT_FILES, FontMetrics
from gxr_studio.finish.layout import frame_layout, stamp_layout
from gxr_studio.finish.logo_math import LogoInfo
from gxr_studio.finish.spec import FRAME_STYLES, LOGO_STYLES, OUTPUT_RATIOS, FrameSpec, StampSpec, parse_frame
from gxr_studio.finish.stamp import format_stamp
from gxr_studio.render.params import EditError

FIXTURE_CJK = Path(__file__).resolve().parents[1] / "fixtures" / "NotoSansSC-Shanghai.otf"

SAMPLE_LOGOS = [
    LogoInfo("wordmark", "Wordmark", 734, 120, 0.34, False, 24.0),
    LogoInfo("round_badge", "Round", 360, 360, 0.58, False, 232.0),
    LogoInfo("tall_mark", "Tall", 180, 420, 0.28, False, 30.0),
    LogoInfo("script_sig", "Script", 620, 230, 0.09, False, 18.0),
    LogoInfo("opaque_card", "Opaque", 300, 180, 1.0, True, 200.0),
]
LOGO_JSON = [
    {"id": lg.id, "name": lg.name, "w": lg.w, "h": lg.h, "coverage": lg.coverage, "opaque": lg.opaque, "lum": lg.lum} for lg in SAMPLE_LOGOS
]


def font_args(*, cjk: bool = True) -> list[str]:
    args: list[str] = []
    for name, filename in FONT_FILES.items():
        args += ["--font", f"{name}={ASSETS / 'fonts' / filename}"]
    if cjk:
        args += ["--font", f"cjk={FIXTURE_CJK}"]
    return args


def font_pairs(*, cjk: bool = True) -> list[list[str]]:
    pairs = [[name, str(ASSETS / "fonts" / filename)] for name, filename in FONT_FILES.items()]
    if cjk:
        pairs.append(["cjk", str(FIXTURE_CJK)])
    return pairs


@contextmanager
def cjk_fonts(paths: tuple[Path, ...]) -> Iterator[None]:
    """Point Python's CJK fallback list at ``paths`` (the fixture, or nothing)."""
    saved = stamp_mod._CJK_FONTS
    stamp_mod._CJK_FONTS = paths
    try:
        yield
    finally:
        stamp_mod._CJK_FONTS = saved


def moment_json(moment: datetime | None) -> list[int] | None:
    if moment is None:
        return None
    return [moment.year, moment.month, moment.day, moment.hour, moment.minute]


def _kind(exc: Exception) -> str:
    """``ZeroDivisionError`` → ``zero_division_error``: the kind the Rust port reports."""
    name = type(exc).__name__
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")


def _outcome(fn: Any) -> Any:
    try:
        got = fn()
    except EditError as exc:
        return {"error": {"kind": exc.code, "message": exc.message}}
    except (ZeroDivisionError, ValueError, KeyError) as exc:
        return {"error": {"kind": _kind(exc), "message": str(exc)}}
    return None if got is None else got.to_json()


def frame_case(w: int, h: int, spec: FrameSpec, meta: dict[str, str], logos: bool = False) -> tuple[dict[str, Any], Any]:
    """One frame case and Python's answer (run inside ``cjk_fonts``)."""
    logo_map = {lg.id: lg for lg in SAMPLE_LOGOS} if logos else None
    request: dict[str, Any] = {"w": w, "h": h, "meta": meta}
    if logos:
        request["logos"] = LOGO_JSON
    want = _outcome(lambda: frame_layout(w, h, spec, meta, FontMetrics(), logo_map))
    return {"request": request, "spec": spec.to_json()}, want


def stamp_case(w: int, h: int, spec: StampSpec, taken: datetime | None, frame: FrameSpec | None = None) -> tuple[dict[str, Any], Any]:
    request: dict[str, Any] = {"w": w, "h": h, "taken": moment_json(taken), "file_time": None}
    m = FontMetrics()

    def run() -> Any:
        border = frame_layout(w, h, frame, {}, m) if frame is not None else None
        return stamp_layout(w, h, spec, format_stamp(spec, taken, None), m, border)

    if frame is not None:
        request["frame"] = frame.to_json()
    return {"request": request, "spec": spec.to_json()}, _outcome(run)


def frame_cases() -> tuple[list[dict[str, Any]], list[Any]]:
    """Every style in ``FRAME_STYLES`` on the approved reference grid, the preview-parity grid,
    CJK captions and every custom-logo option."""
    import test_finish_layout_parity as lp
    import test_frame_reference as ref

    cases: list[dict[str, Any]] = []
    want: list[Any] = []

    def add(pair: tuple[dict[str, Any], Any]) -> None:
        cases.append(pair[0])
        want.append(pair[1])

    for case in ref.cases():
        spec = parse_frame(case["spec"])
        assert spec is not None
        add(frame_case(*case["size"], spec, case["meta"]))
    for w, h in lp.SIZES:
        wide = w * h > 2_000_000
        for style in ("gallery", "leica", "hasselblad", "film", "instant"):
            for spec in lp.frame_specs(style, wide):
                for meta in lp.METAS[:1] if wide else lp.METAS:
                    add(frame_case(w, h, spec, meta))
        add(frame_case(w, h, FrameSpec(style="none"), lp.METAS[0]))
    for style, size, text in itertools.product(
        sorted(FRAME_STYLES - {"none"}), ((320, 240), (800, 1200), (1300, 480), (37, 61)), ("上海", "北京" * 12, "Café 42")
    ):
        add(frame_case(*size, FrameSpec(style=style, output_ratio="4:5", text=text), lp.METAS[0]))
        add(frame_case(*size, FrameSpec(style=style, output_ratio="original", text=text, show_date=True), lp.METAS[2]))
    for style in sorted(LOGO_STYLES):
        colors = ("cream", "black") if style == "slide_mount" else ("white", "black", "cream")
        for (w, h), logo_id, mode, lsize, color, meta in itertools.product(
            ((900, 600), (600, 900)),
            ("wordmark", "round_badge", "tall_mark", "script_sig", "opaque_card", "none", "missing_logo"),
            ("auto", "orig", "mono"),
            ("small", "medium", "large"),
            colors[:2],
            (lp.METAS[0], lp.METAS[1]),
        ):
            if logo_id in ("none", "missing_logo") and (mode != "auto" or lsize != "medium"):
                continue
            for model_on in (True, False) if style == "leica" else (True,):
                spec = FrameSpec(
                    style=style,
                    color=color,
                    output_ratio="4:5",
                    logo=logo_id,
                    logo_mode=mode,
                    logo_size=lsize,
                    model=model_on,
                    text="上海" if logo_id == "wordmark" else "",
                    show_params=logo_id != "tall_mark",
                )
                add(frame_case(w, h, spec, meta, logos=True))
    odd_meta = {"frame_no": "-7", "lens_short": "x 2.5 mm", "date": "", "aperture": "f/2", "shutter": "", "iso": ""}
    for style, ratio in itertools.product(sorted(FRAME_STYLES - {"none"}), ("original", "1:1", "16:9")):
        add(frame_case(1, 1, FrameSpec(style=style, output_ratio=ratio), {}))
        add(frame_case(4288, 1583, FrameSpec(style=style, output_ratio=ratio), odd_meta))
    return cases, want


def stamp_cases() -> tuple[list[dict[str, Any]], list[Any]]:
    import test_finish_layout_parity as lp

    cases: list[dict[str, Any]] = []
    want: list[Any] = []
    for w, h in lp.SIZES:
        wide = w * h > 2_000_000
        for spec, moment in lp.stamp_specs(wide):
            pair = stamp_case(w, h, spec, moment)
            cases.append(pair[0])
            want.append(pair[1])
        for caption, font, size, corner, glow in itertools.product(
            ("", "Paris"), ("seg7", "typewriter", "sans"), ("small", "large"), ("tl", "tr", "bl", "br"), (False, True)
        ):
            border = FrameSpec(style="instant", text=caption)
            spec = StampSpec(enabled=True, placement="border", font=font, size=size, corner=corner, glow=glow)
            pair = stamp_case(w, h, spec, lp.WHENS[0], border)
            cases.append(pair[0])
            want.append(pair[1])
    for w, h in ((1, 1), (12, 7), (40, 30)):
        for font in ("seg7", "typewriter", "sans"):
            spec = StampSpec(enabled=True, content="date+text", text="Hi 上", font=font)
            pair = stamp_case(w, h, spec, lp.WHENS[1])
            cases.append(pair[0])
            want.append(pair[1])
            pair = stamp_case(w, h, StampSpec(enabled=True, font=font, placement="border"), lp.WHENS[2], FrameSpec(style="instant"))
            cases.append(pair[0])
            want.append(pair[1])
    return cases, want


def write_json(path: Path, value: Any) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def same(a: Any, b: Any, path: str = "") -> None:
    """Exact equality with Python's types: bool, int, float (same value), str, list, dict order-free."""
    if isinstance(a, bool) or isinstance(b, bool):
        assert type(a) is type(b) and a == b, (path, a, b)
    elif isinstance(a, int) and isinstance(b, int):
        assert a == b, (path, a, b)
    elif isinstance(a, float) or isinstance(b, float):
        assert type(a) is float and type(b) is float and a == b, (path, a, b)
    elif isinstance(a, dict):
        assert isinstance(b, dict) and list(a) == list(b), (path, list(a), b if not isinstance(b, dict) else list(b))
        for k in a:
            same(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert isinstance(b, list) and len(a) == len(b), (path, a, b)
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            same(x, y, f"{path}[{i}]")
    else:
        assert a == b, (path, a, b)


__all__ = [
    "FIXTURE_CJK",
    "OUTPUT_RATIOS",
    "cjk_fonts",
    "font_args",
    "font_pairs",
    "frame_case",
    "frame_cases",
    "same",
    "stamp_case",
    "stamp_cases",
    "write_json",
]
