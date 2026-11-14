use gxr_raw::CoreError;

use super::super::bounds::{ARRAY_TOO_BIG, bad_geometry};
use super::{
    clamp_norm_rect, crop_valid, fit_preset_in_valid, fit_ratio_in_valid, max_area_rect,
    nearest_window, normalized_valid_rect, valid_integral, window_full,
};

#[test]
fn crop_valid_rejects_short_zero_and_oversized_inputs() {
    assert_eq!(
        crop_valid(&[0; 3], 2, 2, [0., 0., 1., 1.]).unwrap_err(),
        bad_geometry()
    );
    assert_eq!(
        crop_valid(&[], 0, 2, [0., 0., 1., 1.]).unwrap_err(),
        bad_geometry()
    );
    assert_eq!(
        crop_valid(&[0; 3], 2_147_483_648, 1, [0., 0., 0.0000000004, 1.]).unwrap_err(),
        CoreError::new("bad_geometry", ARRAY_TOO_BIG)
    );
    assert_eq!(
        crop_valid(&[0; 3], 800_000_000, 1, [0., 0., 1., 1.]).unwrap_err(),
        CoreError::new("bad_geometry", ARRAY_TOO_BIG)
    );
}

#[test]
fn mask_helpers_reject_short_zero_and_oversized_shapes() {
    assert_eq!(max_area_rect(&[1], 2, 2).unwrap_err(), bad_geometry());
    assert_eq!(max_area_rect(&[], 0, 2).unwrap_err(), bad_geometry());
    assert_eq!(
        max_area_rect(&[1], 2_147_483_648, 1).unwrap_err(),
        CoreError::new("bad_geometry", ARRAY_TOO_BIG)
    );

    assert_eq!(
        normalized_valid_rect(&[1], 2, 2).unwrap_err(),
        bad_geometry()
    );
    assert_eq!(
        normalized_valid_rect(&[], 0, 2).unwrap_err(),
        bad_geometry()
    );
    assert_eq!(
        normalized_valid_rect(&[1], 2_147_483_648, 1).unwrap_err(),
        CoreError::new("bad_geometry", ARRAY_TOO_BIG)
    );

    assert_eq!(valid_integral(&[1], 2, 2).unwrap_err(), bad_geometry());
    assert_eq!(
        fit_ratio_in_valid(&[1], 2, 2, [0., 0., 1., 1.], 1, 1).unwrap_err(),
        bad_geometry()
    );
    assert_eq!(
        fit_preset_in_valid(&[1], 2, 2, None, [0., 0., 1., 1.]).unwrap_err(),
        bad_geometry()
    );
    assert_eq!(
        window_full(&[1], 2, 2, 0, 0, 1, 1).unwrap_err(),
        bad_geometry()
    );
    assert_eq!(
        clamp_norm_rect(&[1], 2, 2, [0., 0., 1., 1.], None).unwrap_err(),
        bad_geometry()
    );
    assert_eq!(
        nearest_window(&[0; 4], 2, 2, 1, 1, 1.0, 1.0).unwrap_err(),
        bad_geometry()
    );
}

#[test]
fn fit_ratio_clamps_oversized_rect_before_usize_conversion() {
    let valid = vec![1_u8; 32 * 24];
    for px in [4_294_967_295_u64, 4_294_967_296, 4_294_967_297] {
        assert_eq!(
            fit_ratio_in_valid(&valid, 32, 24, [0.0, 0.0, px as f64 / 32.0, 0.5], 16, 9).unwrap(),
            [16, 2, 16, 9]
        );
        assert_eq!(
            fit_ratio_in_valid(&valid, 32, 24, [0.0, 0.0, 0.5, px as f64 / 24.0], 9, 16).unwrap(),
            [4, 8, 9, 16]
        );
    }
}
