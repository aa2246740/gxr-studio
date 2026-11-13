//! Port of `gxr_studio/finish/geometry.py` and `gxr_studio/finish/spec.py` (Geometry).

pub mod fit;
pub mod json;
pub mod spec;
pub mod warp;

pub use fit::{
    clamp_norm_rect, crop_box, crop_valid, fit_preset_in_valid, fit_ratio_in_valid, fitted_crop,
    header_value, max_area_rect, normalized_valid_rect, window_full,
};
pub use spec::{Geometry, parse_geometry};
pub use warp::{FILL, Warped, needs_valid_crop, output_size, warp, warp_valid};
