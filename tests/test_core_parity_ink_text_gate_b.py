"""Gate B attribution and frozen-bound tests for all 6 real fonts (Item 3).

Asserts for every ``(font, px, ch)`` across the 5 shipped hinted fonts (Atkinson, Inter,
Inter SemiBold, Courier Prime, Patrick Hand) plus real unmodified DSEG7 (auto-hinted by
FreeType 2.14.3 due to empty ``fpgm``/``prep``), per the frozen bounds in
``M4-PLAN-NOTES.md`` ("Manager decision on ink-text items 1-3, 2026-11-16"):
1. Attribution: whenever Rust (unhinted native loader) differs from real Pillow,
   Gate A (de-hinted ``fpgm=b'\\x00'`` Rust vs de-hinted Pillow) is 100% exact for that
   ``(font, px, ch)`` (0 unattributed differences).
2. Ink presence matches (``(box_rs is None) == (box_py is None)``).
3. Horizontal ink-bbox edges stay within 1 px (``|d_left| <= 1, |d_right| <= 1``).
4. Vertical ink-bbox edges stay within ``<= 2 px`` (Atkinson, Inter, Inter SemiBold,
   Courier Prime), ``<= 3 px`` (Patrick Hand), ``<= 4 px`` (DSEG7).
5. Coverage-sum ratio ``rs_sum / py_sum`` at ``px >= 16`` stays within ``[0.83, 1.15]``
   (Atkinson/Inter/Inter SemiBold/Courier Prime), ``[0.52, 2.10]`` (Patrick Hand), and
   ``[0.77, 1.37]`` (DSEG7); below 16 px it is unrestricted when ``min(sum) < 512``, and
   bounded within ``[0.45, 1.80]`` when ``min(sum) >= 512``.
"""

from __future__ import annotations

from pathlib import Path

from gxr_studio.finish.fonts import FONT_FILES
from support.export_pipeline_cases import check
from support.ink_text_cases import (
    DRAWABLE_CHARS,
    GATE_B_COVERAGE_RATIO_GE_16,
    GATE_B_HORIZ_EDGE_BOUND,
    GATE_B_SMALL_PX_COVERAGE_RATIO,
    GATE_B_SMALL_PX_MIN_SUM,
    GATE_B_VERT_EDGE_BOUNDS,
    DiffRecord,
    compare_masks_common_space,
    dehinted_font_paths,
    font_path,
    large_px_char_chunks,
    open_pil_face,
    pillow_mask,
    run_rust_batch,
    sample_layout_pxs,
)


def measure_gate_b_for_font(
    font_name: str,
    dehinted_path: Path,
    pxs: list[int],
    chars: tuple[str, ...],
    work_dir: Path,
    tag: str,
) -> tuple[int, list[DiffRecord]]:
    real_path = font_path(font_name)
    rs_items = run_rust_batch(real_path, pxs, chars, work_dir, tag=f"gb-real-{tag}")
    dehint_rs_items = run_rust_batch(dehinted_path, pxs, chars, work_dir, tag=f"gb-dh-{tag}")
    pil_real_faces = {px: open_pil_face(real_path, px) for px in pxs}
    pil_dh_faces = {px: open_pil_face(dehinted_path, px) for px in pxs}

    max_vert_edge = GATE_B_VERT_EDGE_BOUNDS[font_name]
    ratio_lo, ratio_hi = GATE_B_COVERAGE_RATIO_GE_16[font_name]
    sm_lo, sm_hi = GATE_B_SMALL_PX_COVERAGE_RATIO

    cases = 0
    diffs: list[DiffRecord] = []
    for rs_it, dh_it in zip(rs_items, dehint_rs_items, strict=True):
        cases += 1
        px, cp, rw, rh, rx, ry, rbuf = rs_it
        _dpx, _dcp, dhw, dhh, dhx, dhy, dhbuf = dh_it
        ch = chr(cp)
        pil_real = pillow_mask(pil_real_faces[px], ch)
        rs_mask = (rw, rh, rx, ry, rbuf)
        if rs_mask == pil_real:
            continue

        # 1. Attribution: Gate A must be exact for this exact (font, px, ch)
        pil_dh = pillow_mask(pil_dh_faces[px], ch)
        check((dhw, dhh, dhx, dhy, dhbuf) == pil_dh, "gate B unattributed difference")

        diff_px, max_d, box_rs, box_py, rs_sum, py_sum = compare_masks_common_space(rs_mask, pil_real)
        # 2. Exact ink presence
        check((box_rs is None) == (box_py is None), "gate B ink presence mismatch")
        if box_rs is not None and box_py is not None:
            d_l = box_rs[0] - box_py[0]
            d_t = box_rs[1] - box_py[1]
            d_r = box_rs[2] - box_py[2]
            d_b = box_rs[3] - box_py[3]
            # 3. Horizontal edges <= 1 px
            check(max(abs(d_l), abs(d_r)) <= GATE_B_HORIZ_EDGE_BOUND, "gate B horiz bbox edge exceeded")
            # 4. Vertical edges <= per-font bound
            check(max(abs(d_t), abs(d_b)) <= max_vert_edge, "gate B vert bbox edge exceeded")
        else:
            d_l = d_t = d_r = d_b = 0

        # 5. Coverage-sum ratio
        if py_sum > 0:
            ratio = rs_sum / py_sum
            if px >= 16:
                check(ratio_lo <= ratio <= ratio_hi, "gate B coverage ratio ge 16 out of bounds")
            elif min(rs_sum, py_sum) >= GATE_B_SMALL_PX_MIN_SUM:
                check(sm_lo <= ratio <= sm_hi, "gate B small-px coverage ratio out of bounds")

        diffs.append(
            DiffRecord(
                font=font_name,
                px=px,
                ch=ch,
                diff_pixels=diff_px,
                max_abs_delta=max_d,
                d_left=d_l,
                d_top=d_t,
                d_right=d_r,
                d_bottom=d_b,
                rs_sum=rs_sum,
                py_sum=py_sum,
            ),
        )
    return cases, diffs


def test_gate_b_real_fonts_attributed_and_bounded(tmp_path: Path) -> None:
    """Gate B across px 1..=160 and all layout px values (all 99 chars), attributing 100% of diffs."""
    dehinted = dehinted_font_paths(tmp_path, zero_maxp=False)
    layout_pxs = sample_layout_pxs()
    pxs_1_to_160 = list(range(1, 161))

    total_cases = 0
    total_diffs = 0
    for font_name in FONT_FILES:
        for idx, start in enumerate(range(0, len(pxs_1_to_160), 40)):
            chunk = pxs_1_to_160[start : start + 40]
            c_cnt, diffs = measure_gate_b_for_font(
                font_name,
                dehinted[font_name],
                chunk,
                DRAWABLE_CHARS,
                tmp_path,
                f"{font_name}-lo-{idx}",
            )
            total_cases += c_cnt
            total_diffs += len(diffs)

        extra_pxs = sorted(p for p in layout_pxs[font_name] if p > 160)
        small_extra = [p for p in extra_pxs if p <= 400]
        large_extra = [p for p in extra_pxs if p > 400]
        if small_extra:
            c_cnt, diffs = measure_gate_b_for_font(
                font_name,
                dehinted[font_name],
                small_extra,
                DRAWABLE_CHARS,
                tmp_path,
                f"{font_name}-sm",
            )
            total_cases += c_cnt
            total_diffs += len(diffs)
        for px in large_extra:
            for cidx, cchunk in enumerate(large_px_char_chunks(px)):
                c_cnt, diffs = measure_gate_b_for_font(
                    font_name,
                    dehinted[font_name],
                    [px],
                    cchunk,
                    tmp_path,
                    f"{font_name}-lg-{px}-{cidx}",
                )
                total_cases += c_cnt
                total_diffs += len(diffs)

    check(total_cases == 102_960, "expected full 102,960 Gate B cases across all 99 DRAWABLE_CHARS")
    check(total_diffs > 0, "gate B total diffs must be positive")
