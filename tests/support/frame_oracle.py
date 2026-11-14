"""The metric-substitution oracle for CJK stamps (manager decision after Sol review r4).

Python's real layout code (`stamp_layout`, the frame layout, the fit loop) runs unchanged, with one
substitution: the CJK glyph metrics `FontMetrics.glyph(font, ch, cjk=True)` returns, which are
Pillow's hinted 1000-px box, are replaced by gxr-frame's 1000-px values (the CFF outline with
FreeType's unhinted rounding, `gxr-cli frame-cjk`). Rust must equal this oracle exactly in every
field. The real Python differs only where hinted and unhinted metrics differ (δ ≤ 1 at 1000 px for
the fixture font), including discrete fit decisions such as a 14 px versus 11 px stamp.
Nothing in `src/` is touched; the swap lives here.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from gxr_studio.finish.fonts import FontMetrics
from gxr_studio.finish.layout import frame_layout, stamp_layout
from gxr_studio.finish.spec import FrameSpec, StampSpec
from support import frame_cases
from support.core_cli import error_of, run_json
from support.frame_cases import FIXTURE_CJK, cjk_fonts, stamp_case

_RUST_CJK: dict[str, tuple[int, tuple[int, int, int, int] | None]] = {}


def rust_cjk_glyphs(chars: str) -> dict[str, tuple[int, tuple[int, int, int, int] | None]]:
    """gxr-frame's CJK glyph metrics at 1000 px for `chars` (cached): (advance, box or None)."""
    missing = "".join(ch for ch in dict.fromkeys(chars) if ch not in _RUST_CJK)
    if missing:
        code, got = run_json(["frame-cjk", "--font", str(FIXTURE_CJK), "--chars", missing, "--px", "1000"])
        assert error_of(code, got) is None, got
        for ch, (adv, box) in zip(missing, got["glyphs"], strict=True):
            _RUST_CJK[ch] = (adv, tuple(box) if box is not None else None)
    return {ch: _RUST_CJK[ch] for ch in chars}


class OracleMetrics(FontMetrics):
    """FontMetrics with only the CJK glyph metrics replaced by gxr-frame's 1000-px values."""

    def glyph(self, font: str, ch: str, cjk: bool) -> Any:
        if not cjk:
            return super().glyph(font, ch, cjk)
        return rust_cjk_glyphs(ch)[ch]


@contextmanager
def oracle_metrics() -> Iterator[None]:
    """Run the `frame_cases` generators with the oracle instead of the real FontMetrics."""
    saved = frame_cases.METRICS_FACTORY
    frame_cases.METRICS_FACTORY = OracleMetrics
    try:
        yield
    finally:
        frame_cases.METRICS_FACTORY = saved


def oracle_and_real(generate: Any) -> tuple[list[dict[str, Any]], list[Any], list[Any]]:
    """Run a case generator under the oracle and under the real Python (fixture CJK font both
    times). Returns the cases, the oracle's answers and the real Python's answers."""
    with cjk_fonts((FIXTURE_CJK,)):
        with oracle_metrics():
            out = generate()
        cases, oracle = out[0], out[1]
        real_out = generate()
    assert [c["request"] for c in real_out[0]] == [c["request"] for c in cases]
    return cases, oracle, real_out[1]


# Sol r4's 30 confirmed fit-boundary inputs (sol-review-mobile-frame-r4/evidence/fit-boundaries-inputs.json):
# instant border stamps "北", seg7, large, glow, top-left, output ratio original, where the 1-px box
# difference flips the 0.85 shrink. (w, h, format, caption).
SOL_R4_FIT: list[tuple[int, int, str, str]] = [
    (282, 212, "instax_wide", "海"),
    (212, 282, "instax_wide", "海"),
    (282, 212, "instax_wide", "北京海上"),
    (212, 282, "instax_wide", "北京海上"),
    (284, 213, "instax_wide", "海"),
    (213, 284, "instax_wide", "海"),
    (284, 213, "instax_wide", "北京海上"),
    (213, 284, "instax_wide", "北京海上"),
    (285, 214, "instax_wide", "海"),
    (214, 285, "instax_wide", "海"),
    (285, 214, "instax_wide", "北京海上"),
    (214, 285, "instax_wide", "北京海上"),
    (290, 218, "instax_square", "海"),
    (218, 290, "instax_square", "海"),
    (290, 218, "instax_square", "北京海上"),
    (218, 290, "instax_square", "北京海上"),
    (292, 219, "instax_square", "海"),
    (219, 292, "instax_square", "海"),
    (292, 219, "instax_square", "北京海上"),
    (219, 292, "instax_square", "北京海上"),
    (293, 220, "instax_square", "海"),
    (220, 293, "instax_square", "海"),
    (293, 220, "instax_square", "北京海上"),
    (220, 293, "instax_square", "北京海上"),
    (294, 221, "instax_square", "海"),
    (221, 294, "instax_square", "海"),
    (294, 221, "instax_square", "北京海上"),
    (221, 294, "instax_square", "北京海上"),
    (296, 222, "instax_square", "海"),
    (222, 296, "instax_square", "海"),
]


def sol_r4_fit_cases() -> tuple[list[dict[str, Any]], list[Any]]:
    """Sol r4's 30 fit-boundary inputs plus every transpose (all 30 already come in pairs)."""
    seen: set[tuple[int, int, str, str]] = set()
    cases: list[dict[str, Any]] = []
    want: list[Any] = []
    for w, h, fmt, caption in [*SOL_R4_FIT, *((h, w, fmt, caption) for w, h, fmt, caption in SOL_R4_FIT)]:
        if (w, h, fmt, caption) in seen:
            continue
        seen.add((w, h, fmt, caption))
        spec = StampSpec(enabled=True, content="text", text="北", font="seg7", size="large", corner="tl", glow=True, placement="border")
        frame = FrameSpec(style="instant", fmt=fmt, text=caption, output_ratio="original")
        case, layout = stamp_case(w, h, spec, None, frame)
        cases.append(case)
        want.append(layout)
    return cases, want


INSTANT_FORMATS = ("polaroid", "instax_square", "instax_wide", "instax_mini")


def fit_threshold_inputs(shorts: range = range(12, 601)) -> list[tuple[int, int, str, str, str, str]]:
    """Inputs at the border fit thresholds, for every instant format with a caption and glow on.

    Walks the photo's short side (4:3, both orientations) and keeps every size where the stamp's
    pixel size changes from the previous size under either metric set, plus every size where the
    real Python and the oracle choose different sizes. Returns (w, h, format, caption, text, size)."""
    picked: list[tuple[int, int, str, str, str, str]] = []
    with cjk_fonts((FIXTURE_CJK,)):
        real, oracle = FontMetrics(), OracleMetrics()
        for fmt, caption, text, size in itertools.product(INSTANT_FORMATS, ("海", "北京海上"), ("北", "上海"), ("small", "large")):
            frame_spec = FrameSpec(style="instant", fmt=fmt, text=caption, output_ratio="original")
            spec = StampSpec(enabled=True, content="text", text=text, font="seg7", size=size, corner="tl", glow=True, placement="border")
            for portrait in (False, True):
                prev: tuple[Any, Any] | None = None
                for short in shorts:
                    w, h = (short, short * 4 // 3) if portrait else (short * 4 // 3, short)
                    frame = frame_layout(w, h, frame_spec, {}, real)
                    a = stamp_layout(w, h, spec, text, real, frame)
                    b = stamp_layout(w, h, spec, text, oracle, frame_layout(w, h, frame_spec, {}, oracle))
                    now = (None if a is None else a.px, None if b is None else b.px)
                    if (prev is not None and now != prev) or now[0] != now[1]:
                        picked.append((w, h, fmt, caption, text, size))
                    prev = now
    return picked


def fit_threshold_cases(inputs: list[tuple[int, int, str, str, str, str]]) -> tuple[list[dict[str, Any]], list[Any]]:
    cases: list[dict[str, Any]] = []
    want: list[Any] = []
    for w, h, fmt, caption, text, size in inputs:
        spec = StampSpec(enabled=True, content="text", text=text, font="seg7", size=size, corner="tl", glow=True, placement="border")
        frame = FrameSpec(style="instant", fmt=fmt, text=caption, output_ratio="original")
        case, layout = stamp_case(w, h, spec, None, frame)
        cases.append(case)
        want.append(layout)
    return cases, want
