//! Port of `gxr_studio/finish/{pixels,compose,frame_paint,frame,logo_store,logo_math}.py`
//! raster operations for mobile export.
//!
//! Call existing ports in `crate::geometry`, `gxr_color`, `gxr_frame`, and `gxr_hdr`; never copy
//! them. Note that `gxr_color::apply_table` and `gxr_frame::{aspect_mult, coverage_comp,
//! fit_logo, pick_logo_mode, clogo_item}` are not currently re-exported by their crate roots.

pub mod canvas;
pub mod compose;
pub mod logo;
pub mod pixels;

pub use canvas::{FrameCanvas, background, draw_frame_canvas, draw_frame_canvas_u64};
pub use compose::{FinishOptions, FinishUnframed, finish_array_u64, finish_unframed};
pub use logo::{
    LogoKeyResult, alpha_luma, fit_logo_json, has_white_background, key_white, prepare_logo_key,
    stats_of, trim_ink,
};
pub use pixels::{
    FinishAdjust, apply_base, apply_extra, black, chroma, linear_to_srgb, parse_curves_value,
    parse_finish_adjust_value, srgb_to_linear, vignette,
};
