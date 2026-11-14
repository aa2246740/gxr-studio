use crate::geometry::bounds::ARRAY_TOO_BIG;

use super::filters::{
    bicubic_filter, bilinear_filter, box_filter, lanczos_filter, normalize_coeffs_8bpc,
    precompute_coeffs, sinc_filter,
};
use super::passes::{clip8, mul_div_255, premul_to_rgba, rgba_to_premul};
use super::{Filter, Mode, resample_f32, resample_f32_u64, resample_u8, resample_u8_u64};

#[test]
fn filters_match_c_definitions() {
    assert_eq!(box_filter(-0.5), 0.0);
    assert_eq!(box_filter(-0.49), 1.0);
    assert_eq!(box_filter(0.5), 1.0);
    assert_eq!(box_filter(0.51), 0.0);

    assert_eq!(bilinear_filter(0.0), 1.0);
    assert_eq!(bilinear_filter(-0.25), 0.75);
    assert_eq!(bilinear_filter(1.0), 0.0);

    assert_eq!(bicubic_filter(0.0), 1.0);
    assert_eq!(bicubic_filter(1.0), 0.0);
    assert_eq!(bicubic_filter(-2.0), 0.0);

    assert_eq!(sinc_filter(0.0), 1.0);
    assert_eq!(lanczos_filter(0.0), 1.0);
    assert_eq!(lanczos_filter(-3.0), sinc_filter(-3.0) * sinc_filter(-1.0));
    assert_eq!(lanczos_filter(3.0), 0.0);
}

#[test]
fn normalize_rounds_negative_coeffs_away_from_zero() {
    let scale = f64::from(1_i32 << 22);
    // Exact negative half-LSB and positive half-LSB
    let neg = -1.25 / scale;
    let pos = 1.25 / scale;
    let out = normalize_coeffs_8bpc(1, 2, &[neg, pos]).unwrap();
    assert_eq!(out[0], (-0.5 - 1.25) as i32); // -1
    assert_eq!(out[1], (0.5 + 1.25) as i32); // 1

    let neg2 = -0.6 / scale;
    let out2 = normalize_coeffs_8bpc(1, 1, &[neg2]).unwrap();
    assert_eq!(out2[0], -1);
}

#[test]
fn clip8_clamps_range() {
    assert_eq!(clip8(-100_i64 << 22), 0);
    assert_eq!(clip8(0), 0);
    assert_eq!(clip8(128_i64 << 22), 128);
    assert_eq!(clip8(255_i64 << 22), 255);
    assert_eq!(clip8(300_i64 << 22), 255);
}

#[test]
fn rgba_premul_and_unpremul_match_convert_c() {
    assert_eq!(mul_div_255(255, 255), 255);
    assert_eq!(mul_div_255(200, 0), 0);
    assert_eq!(mul_div_255(200, 128), 100);

    let rgba = [200_u8, 100, 50, 0, 200, 100, 50, 255, 200, 100, 50, 128];
    let premul = rgba_to_premul(&rgba).unwrap();
    assert_eq!(&premul[0..4], &[0, 0, 0, 0]);
    assert_eq!(&premul[4..8], &[200, 100, 50, 255]);
    assert_eq!(&premul[8..12], &[100, 50, 25, 128]);

    let back = premul_to_rgba(&premul).unwrap();
    assert_eq!(&back[0..4], &[0, 0, 0, 0]);
    assert_eq!(&back[4..8], &[200, 100, 50, 255]);
    // 255 * 100 / 128 = 199, 255 * 50 / 128 = 99, 255 * 25 / 128 = 49
    assert_eq!(&back[8..12], &[199, 99, 49, 128]);
}

#[test]
fn identity_resize_copies_without_rgba_premultiply() {
    let rgba = [123_u8, 45, 67, 0, 200, 100, 50, 128];
    let out = resample_u8(&rgba, 2, 1, Mode::Rgba, Filter::Lanczos, 2, 1).unwrap();
    assert_eq!(out, rgba);
}

#[test]
fn rejects_zero_mismatch_and_oversized_dims() {
    let err0 = resample_u8(&[1, 2, 3], 1, 1, Mode::Rgb, Filter::Lanczos, 0, 1).unwrap_err();
    assert_eq!(err0.kind, "value_error");

    let err_len = resample_u8(&[1, 2], 1, 1, Mode::Rgb, Filter::Lanczos, 2, 2).unwrap_err();
    assert_eq!(err_len.kind, "value_error");

    for (ow, oh) in [(4_294_967_295, 4_294_967_295), (2_147_483_648, 1)] {
        let e = resample_u8_u64(&[1, 2, 3], 1, 1, Mode::Rgb, Filter::Box, ow, oh).unwrap_err();
        assert_eq!(e.kind, "bad_geometry");
        assert_eq!(e.message, ARRAY_TOO_BIG);

        let ef = resample_f32_u64(&[1.0], 1, 1, Filter::Box, ow, oh).unwrap_err();
        assert_eq!(ef.kind, "bad_geometry");
        assert_eq!(ef.message, ARRAY_TOO_BIG);
    }

    let e_in = resample_u8_u64(&[], 2_147_483_648, 1, Mode::L, Filter::Box, 1, 1).unwrap_err();
    assert_eq!(e_in.kind, "bad_geometry");
    assert_eq!(e_in.message, ARRAY_TOO_BIG);
}

#[test]
fn f32_identity_preserves_nan_and_neg_zero_bits() {
    let nan_bits: u32 = 0x7fc0_1234;
    let src = [
        f32::from_bits(nan_bits),
        -0.0_f32,
        f32::INFINITY,
        f32::NEG_INFINITY,
    ];
    let out = resample_f32(&src, 2, 2, Filter::Box, 2, 2).unwrap();
    assert_eq!(out[0].to_bits(), nan_bits);
    assert_eq!(out[1].to_bits(), (-0.0_f32).to_bits());
    assert_eq!(out[2].to_bits(), f32::INFINITY.to_bits());
    assert_eq!(out[3].to_bits(), f32::NEG_INFINITY.to_bits());

    let c = precompute_coeffs(4, 0.0, 4.0, 2, Filter::Box).unwrap();
    assert_eq!(c.ksize, 3);
    assert_eq!(c.bounds, vec![0, 2, 2, 2]);
}
