//! Port of `gxr_studio/finish/geometry.py` and `gxr_studio/finish/spec.py` (Geometry).

pub mod bounds;
pub mod fit;
pub mod float_layer;
pub mod json;
pub mod spec;
pub mod warp;

pub use fit::{
    clamp_norm_rect, crop_box, crop_valid, fit_preset_in_valid, fit_ratio_in_valid, fitted_crop,
    header_value, max_area_rect, normalized_valid_rect, window_full,
};
pub use float_layer::{
    FloatLayer, checked_float_layer_dims, checked_float_layer_slice, crop_valid_f32,
    crop_valid_f32_u64, try_vec_f32, warp_float, warp_float_u64,
};
pub use spec::{Geometry, parse_geometry};
pub use warp::{
    FILL, Warped, needs_valid_crop, output_size, output_size_u64, warp, warp_u64, warp_valid,
    warp_valid_u64,
};
