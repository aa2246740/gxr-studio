use super::ftcalc::FtVector;
use super::glyf::{
    FT_CURVE_TAG_CONIC, FT_CURVE_TAG_CUBIC, FT_CURVE_TAG_ON, FT_OUTLINE_EVEN_ODD_FILL,
    FT_OUTLINE_OVERLAP, Outline,
};
use super::gray::{render_glyph_bitmap, render_outline_gray};

fn rect_outline(x0: i64, y0: i64, x1: i64, y1: i64, flags: u32) -> Outline {
    Outline {
        points: vec![
            FtVector { x: x0, y: y0 },
            FtVector { x: x0, y: y1 },
            FtVector { x: x1, y: y1 },
            FtVector { x: x1, y: y0 },
        ],
        tags: vec![FT_CURVE_TAG_ON; 4],
        contours: vec![3],
        flags,
    }
}

#[test]
fn empty_outline_returns_empty_bitmap() {
    let bmp = render_glyph_bitmap(&Outline::default(), Some((16, 16))).unwrap();
    assert_eq!(bmp.width, 0);
    assert_eq!(bmp.height, 0);
    assert!(bmp.buffer.is_empty());
}

#[test]
fn exact_pixel_aligned_square_fills_255() {
    let o = rect_outline(64, 64, 192, 192, 0);
    let bmp = render_glyph_bitmap(&o, Some((16, 16))).unwrap();
    assert_eq!((bmp.left, bmp.top, bmp.width, bmp.height), (1, 3, 2, 2));
    assert_eq!(bmp.buffer, vec![255, 255, 255, 255]);
}

#[test]
fn sub_pixel_tiny_triangle_has_fractional_coverage() {
    let o = Outline {
        points: vec![
            FtVector { x: 16, y: 16 },
            FtVector { x: 48, y: 16 },
            FtVector { x: 32, y: 48 },
        ],
        tags: vec![FT_CURVE_TAG_ON; 3],
        contours: vec![2],
        flags: 0,
    };
    let bmp = render_glyph_bitmap(&o, Some((16, 16))).unwrap();
    assert_eq!((bmp.width, bmp.height), (1, 1));
    assert!(bmp.buffer[0] > 0 && bmp.buffer[0] < 128);
}

#[test]
fn self_intersecting_nested_squares_nonzero_vs_even_odd() {
    // Two nested CCW squares: nonzero fills inner core (winding 2 -> 255),
    // even-odd leaves inner core hollow (winding 2 % 2 == 0 -> 0).
    let mut points = Vec::new();
    points.extend([
        FtVector { x: 0, y: 0 },
        FtVector { x: 0, y: 384 },
        FtVector { x: 384, y: 384 },
        FtVector { x: 384, y: 0 },
    ]);
    points.extend([
        FtVector { x: 128, y: 128 },
        FtVector { x: 128, y: 256 },
        FtVector { x: 256, y: 256 },
        FtVector { x: 256, y: 128 },
    ]);
    let nonzero = Outline {
        points: points.clone(),
        tags: vec![FT_CURVE_TAG_ON; 8],
        contours: vec![3, 7],
        flags: 0,
    };
    let even_odd = Outline {
        points,
        tags: vec![FT_CURVE_TAG_ON; 8],
        contours: vec![3, 7],
        flags: FT_OUTLINE_EVEN_ODD_FILL,
    };
    let b_nz = render_glyph_bitmap(&nonzero, Some((16, 16))).unwrap();
    let b_eo = render_glyph_bitmap(&even_odd, Some((16, 16))).unwrap();
    assert_eq!(b_nz.width, 6);
    assert_eq!(b_eo.width, 6);
    // Center pixel (x=3, y=3)
    assert_eq!(b_nz.buffer[3 * 6 + 3], 255);
    assert_eq!(b_eo.buffer[3 * 6 + 3], 0);
}

#[test]
fn overlap_flag_keeps_non_saturated_union_for_double_covered_triangle() {
    // Two overlapping CW squares with sub-pixel edge; overlap mode computes 4x4 subsamples.
    let mut points = Vec::new();
    points.extend([
        FtVector { x: 32, y: 0 },
        FtVector { x: 32, y: 128 },
        FtVector { x: 160, y: 128 },
        FtVector { x: 160, y: 0 },
    ]);
    points.extend([
        FtVector { x: 32, y: 0 },
        FtVector { x: 32, y: 128 },
        FtVector { x: 160, y: 128 },
        FtVector { x: 160, y: 0 },
    ]);
    let o_overlap = Outline {
        points: points.clone(),
        tags: vec![FT_CURVE_TAG_ON; 8],
        contours: vec![3, 7],
        flags: FT_OUTLINE_OVERLAP,
    };
    let b = render_glyph_bitmap(&o_overlap, Some((16, 16))).unwrap();
    assert_eq!((b.width, b.height), (3, 2));
    // Left half-covered column at x=0 has 8/16 subsamples covered => 128 coverage (not saturated to 255).
    assert_eq!(b.buffer[0], 128);
    assert_eq!(b.buffer[1], 255);
}

#[test]
fn rejects_huge_coordinates_and_ppem_overflow() {
    let huge = rect_outline(0, 0, 0x1000001, 64, 0);
    assert!(render_outline_gray(&huge, 10, 10).is_err());

    let wide = rect_outline(0, 0, 64 * 200, 64 * 10, 0);
    assert!(render_glyph_bitmap(&wide, Some((16, 16))).is_err());
}

#[test]
fn conic_with_all_off_curve_points_and_cubic_pair_render() {
    let conic_diamond = Outline {
        points: vec![
            FtVector { x: 64, y: 64 },
            FtVector { x: 64, y: 320 },
            FtVector { x: 320, y: 320 },
            FtVector { x: 320, y: 64 },
        ],
        tags: vec![FT_CURVE_TAG_CONIC; 4],
        contours: vec![3],
        flags: 0,
    };
    let b_conic = render_glyph_bitmap(&conic_diamond, Some((16, 16))).unwrap();
    assert!(b_conic.buffer.iter().any(|&v| v > 200));

    let cubic = Outline {
        points: vec![
            FtVector { x: 64, y: 64 },
            FtVector { x: 64, y: 320 },
            FtVector { x: 320, y: 320 },
            FtVector { x: 320, y: 64 },
        ],
        tags: vec![
            FT_CURVE_TAG_ON,
            FT_CURVE_TAG_CUBIC,
            FT_CURVE_TAG_CUBIC,
            FT_CURVE_TAG_ON,
        ],
        contours: vec![3],
        flags: 0,
    };
    let b_cubic = render_glyph_bitmap(&cubic, Some((16, 16))).unwrap();
    assert!(b_cubic.buffer.iter().any(|&v| v > 100));
}

#[test]
fn tall_outline_exercises_band_splitting_cleanly() {
    // Height 200 > FT_MAX_GRAY_POOL / 8 (85) triggers multi-band rendering.
    let o = rect_outline(16, 0, 200 * 64 + 48, 200 * 64, 0);
    let bmp = render_glyph_bitmap(&o, Some((500, 500))).unwrap();
    assert_eq!(bmp.height, 200);
    assert_eq!(bmp.width, 201);
    assert_eq!(bmp.buffer[0], 192);
    assert_eq!(bmp.buffer[1], 255);
}
