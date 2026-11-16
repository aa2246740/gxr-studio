use gxr_finish::ink::{
    InkImage, InkMode, RectItem, ShadowItem, box_blur, draw_ellipse, draw_line, draw_polygon,
    draw_rectangle, draw_rounded_rectangle, gaussian_blur, paint_ellipse, paint_hole, paint_poly,
    paint_rect, paint_shadow, paper_grain, paper_grain_in_place, run_ink_ops, run_ink_ops_traced,
    run_ink_ops_vec, wobble,
};

#[test]
fn ink_image_accessors_from_vec_and_paper_grain_in_place_parity() {
    let raw: Vec<u8> = (0..(16 * 12 * 3)).map(|i| ((i * 37) % 256) as u8).collect();
    let img_ref = InkImage::from_bytes(InkMode::Rgb, 16, 12, &raw).expect("from_bytes");
    let mut img_vec = InkImage::from_vec(InkMode::Rgb, 16, 12, raw).expect("from_vec");

    assert_eq!(img_vec.width(), 16);
    assert_eq!(img_vec.height(), 12);
    assert_eq!(img_vec.mode(), InkMode::Rgb);
    assert_eq!(img_vec.as_slice(), img_ref.as_slice());

    let expected = paper_grain(&img_ref, 0.025).expect("paper_grain");
    paper_grain_in_place(&mut img_vec, 0.025).expect("paper_grain_in_place");
    assert_eq!(img_vec.as_slice(), expected.as_slice());

    let owned = img_vec.into_bytes();
    assert_eq!(owned, expected.as_slice());
}

#[test]
fn run_ink_ops_vec_and_traced_match() {
    let canvas = vec![40_u8; 8 * 8 * 3];
    let ops = r#"[{"op":"draw_rectangle","box":[1,1,6,6],"fill":[200,120,30]}]"#;
    let out_ref = run_ink_ops(&canvas, 8, 8, InkMode::Rgb, ops, &[]).expect("run_ink_ops");
    let out_vec =
        run_ink_ops_vec(canvas.clone(), 8, 8, InkMode::Rgb, ops, &[]).expect("run_ink_ops_vec");
    let out_traced =
        run_ink_ops_traced(&canvas, 8, 8, InkMode::Rgb, ops, &[]).expect("run_ink_ops_traced");

    assert_eq!(out_vec.as_slice(), out_ref.as_slice());
    assert_eq!(out_traced.image.as_slice(), out_ref.as_slice());
    assert!(out_traced.clogo_traces.is_empty());
}

#[test]
fn rejects_hostile_overflow_and_non_finite_inputs_cleanly() {
    assert!(InkImage::point_table_mul(f64::NAN).is_err());
    assert!(InkImage::point_table_mul(f64::INFINITY).is_err());
    assert!(InkImage::point_table_mul(1e30).is_err());

    let empty = InkImage::new_filled(InkMode::Rgb, 0, 0, [0, 0, 0, 255]).expect("empty");
    assert!(box_blur(&empty, 2_147_483_648.0_f32, 0.0_f32, 1).is_err());
    assert!(gaussian_blur(&empty, 1e300, 1e300).is_err());
    assert!(gaussian_blur(&empty, f64::NAN, 1.0).is_err());

    let mut im = InkImage::new_filled(InkMode::Rgb, 12, 12, [0, 0, 0, 255]).expect("im");
    assert!(draw_ellipse(&mut im, [0.0, 0.0, 65536.0, 65536.0], [1, 2, 3]).is_err());
    assert!(
        draw_rounded_rectangle(
            &mut im,
            [1.0, 1.0, 10.0, 10.0],
            1e300,
            Some([1, 2, 3]),
            None,
            1,
            [true, false, false, false],
        )
        .is_err()
    );
    assert!(
        paint_shadow(
            &mut im,
            &ShadowItem {
                x: 2000,
                y: 2000,
                w: 10,
                h: 10,
                blur: 1e300,
                dx: 0,
                dy: 0,
                radius: 0.0,
                alpha: 0.5,
                color: [0, 0, 0],
            },
        )
        .is_err()
    );
    assert!(
        paint_rect(
            &mut im,
            &RectItem {
                x: 0,
                y: 0,
                w: 10,
                h: 10,
                radius: 0.0,
                fill: Some([1, 2, 3]),
                outline: None,
                stroke_w: f64::NAN,
                alpha: 1.0,
            },
        )
        .is_err()
    );
    assert!(paint_poly(&mut im, &[[1e30, 0.0], [0.0, 1.0]], [1, 2, 3], 1.0).is_err());
    assert!(paint_ellipse(&mut im, 1e30, 0.0, 5.0, [1, 2, 3], 1.0).is_err());
    let base = im.clone();
    assert!(paint_hole(&mut im, 0, 0, 4, 4, f64::NAN, &base).is_err());
    assert!(draw_line(&mut im, &[[0.0, 0.0], [0.0, 1e9]], [1, 2, 3], 1).is_ok());
    assert!(draw_rectangle(&mut im, [0.0, 0.0, 10.0, 10.0], None, Some([1, 2, 3]), 1).is_ok());
    assert!(draw_polygon(&mut im, &[[0.0, 0.0], [5.0, 5.0], [10.0, 0.0]], [1, 2, 3]).is_ok());

    let mut rng = gxr_finish::ink::Pcg64::from_seed(42);
    assert!(wobble(8, &mut rng, 0.0, 2, 1.0).is_err());
    assert!(wobble(8, &mut rng, 2.0, 2, f64::INFINITY).is_err());
}
