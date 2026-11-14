//! Port of `gxr_studio/crop.py`, `gxr_studio/finish/geometry.py`, and `gxr_studio/gpu/box.py`.
//!
//! Frame and stamp layout is `gxr-frame`. Raster comes later.
#![forbid(unsafe_code)]

pub mod crop;
pub mod geometry;
#[path = "box.rs"]
pub mod gpu_box;
pub mod raster;

pub use geometry::{Geometry, parse_geometry, warp, warp_u64, warp_valid, warp_valid_u64};
pub use gpu_box::{final_box, final_box_json, final_box_json_u64, final_box_u64};
pub use raster::{
    FinishAdjust, FinishOptions, FinishUnframed, FrameCanvas, LogoKeyResult, apply_base,
    apply_extra, background, draw_frame_canvas, draw_frame_canvas_u64, finish_array_u64,
    finish_unframed, fit_logo_json, has_white_background, key_white, linear_to_srgb,
    prepare_logo_key, srgb_to_linear, stats_of, trim_ink,
};
