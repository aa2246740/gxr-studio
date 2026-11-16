//! Rust/WASM port of the desktop's non-text Pillow / NumPy painting primitives:
//! pixel plumbing, ImageDraw primitives, supersampled shapes, GaussianBlur, backdrops
//! (blur + PCG64 paper grain), negative carrier contours, and custom logo painting/preparing.

pub mod backdrop;
pub mod blur;
pub mod carrier;
pub mod draw;
pub mod draw_arc;
pub mod draw_ellipse;
pub mod image;
pub mod logo;
pub mod ops;
pub mod paste;
pub mod rng;
pub mod rng_tables;
pub mod shapes;

pub use backdrop::{background, blurred_background, paper_grain, paper_grain_in_place};
pub use blur::{box_blur, gaussian_blur};
pub use carrier::{carrier_contours, paint_carrier, wobble};
pub use draw::{draw_line, draw_polygon, draw_rectangle};
pub use draw_arc::draw_rounded_rectangle;
pub use draw_ellipse::draw_ellipse;
pub use image::{InkImage, InkMode, mean_f32, mean_u8_as_f32, pairwise_sum_f32};
pub use logo::{
    ClogoItem, ClogoTrace, ERR_TOO_SMALL, LogoPrepareOptions, LogoPrepareResult,
    finish_logo_prepare, paint_clogo, paint_clogo_traced, parse_logo_prepare_json,
};
pub use ops::{
    InkOpsOutput, LogoInput, decode_logo_bundle, run_ink_ops, run_ink_ops_traced, run_ink_ops_vec,
};
pub use paste::{paste_color, paste_image};
pub use rng::{Pcg64, SeedSequence};
pub use shapes::{
    RectItem, ShadowItem, aa_paint, paint_ellipse, paint_hole, paint_poly, paint_rect, paint_shadow,
};
