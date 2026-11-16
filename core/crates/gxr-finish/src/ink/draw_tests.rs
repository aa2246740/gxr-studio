use super::*;

fn round_down_mul_f64_unfused(a: f64, b: f64) -> Result<i32, CoreError> {
    let prod = a * b;
    let rounded = if prod >= 0.0_f64 {
        (prod - 0.5_f64).ceil()
    } else {
        -((prod.abs() - 0.5_f64).ceil())
    };
    if !rounded.is_finite() || rounded < f64::from(i32::MIN) || rounded > f64::from(i32::MAX) {
        return Err(bad_coords("wide line coordinate out of range"));
    }
    Ok(rounded as i32)
}

fn edge_x_f32_unfused(y: i32, offset: i32, e: &Edge) -> f32 {
    let dy = (i64::from(y) + i64::from(offset) - i64::from(e.y0)) as f32;
    (e.x0 as f32) + dy * e.dx
}

#[test]
fn wide_line_and_polygon_fma_and_bounds() {
    // Sol r2 wide-line FMA counterexample: `16x16` L, width 3, `[[2,1],[416721299,240594154]]`.
    let mut im_fused = InkImage::new_filled(InkMode::L, 16, 16, [0, 0, 0, 255]).expect("im");
    draw_line(
        &mut im_fused,
        &[[2.0, 1.0], [416_721_299.0, 240_594_154.0]],
        [255, 0, 0],
        3,
    )
    .expect("fused");
    assert_eq!(im_fused.as_slice()[2], 0);

    let mut im_unfused = InkImage::new_filled(InkMode::L, 16, 16, [0, 0, 0, 255]).expect("im");
    draw_wide_line_with(
        &mut im_unfused,
        2,
        1,
        416_721_299,
        240_594_154,
        [255, 0, 0],
        3,
        round_down_mul_f64_unfused,
    )
    .expect("unfused");
    assert_eq!(im_unfused.as_slice()[2], 255);
    assert_eq!(
        im_fused
            .as_slice()
            .iter()
            .zip(im_unfused.as_slice())
            .filter(|(a, b)| a != b)
            .count(),
        10
    );

    // Sol r1 `_polygon_generic` FMA counterexample: fused `edge_x_f32` differs from unfused.
    let mut poly_fused = InkImage::new_filled(InkMode::L, 48, 40, [17, 0, 0, 255]).expect("im");
    draw_polygon(
        &mut poly_fused,
        &[[38.0, -13.0], [35.0, 9.0], [-12.0, 15.0]],
        [211, 0, 0],
    )
    .expect("fused poly");
    let mut poly_unfused = InkImage::new_filled(InkMode::L, 48, 40, [17, 0, 0, 255]).expect("im");
    let poly_edges = [
        make_edge(38, -13, 35, 9).expect("p0"),
        make_edge(35, 9, -12, 15).expect("p1"),
        make_edge(-12, 15, 38, -13).expect("p2"),
    ];
    polygon_generic_with(
        &mut poly_unfused,
        &poly_edges,
        [211, 0, 0],
        edge_x_f32_unfused,
    );
    assert_ne!(poly_fused.as_slice(), poly_unfused.as_slice());

    let e = make_edge(-10, -10, 10, 10).expect("edge");
    assert_eq!(edge_x_f32(0, 0, &e), 0.0_f32);

    let mut im = InkImage::new_filled(InkMode::Rgb, 16, 16, [0, 0, 0, 255]).expect("im");
    assert!(
        draw_line(
            &mut im,
            &[[f64::from(i32::MIN), 0.0], [f64::from(i32::MAX), 1.0]],
            [255, 0, 0],
            3,
        )
        .is_err()
    );
    draw_rectangle(
        &mut im,
        [0.0, 0.0, 15.0, 15.0],
        None,
        Some([1, 2, 3]),
        i32::MAX,
    )
    .expect("ok");
    assert_eq!(im.as_slice()[0..3], [1, 2, 3]);
}
