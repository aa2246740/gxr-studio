//! Port of `gxr_studio/lut/` (`.cube` parsing and tetrahedral lookup, colour spaces,
//! display/log GPU LUT tables, cube space identification, and scene XYZ glue over `gxr-raw`).
#![forbid(unsafe_code)]

pub mod cube;
pub mod identify;
mod identify_scan;
pub mod photo;
pub mod scene;
pub mod spaces;
pub mod tables;

pub use cube::{Cube, MAX_1D, MAX_3D, MAX_BYTES, Table3, comment_start};
pub use identify::{IDENTIFICATION_VERSION, IdentifyResult, guess_space, identify};
pub use photo::apply_u8;
pub use scene::{
    ANCHOR_F, ANCHOR_U_M, SceneAffine, SceneColour, SceneError, affine_of, apply_log_with_dng,
    scene_affine, scene_colour, scene_xyz,
};
pub use spaces::{
    DISPLAY, RenderSpec, RenderStats, SPACES, Target, decode, encode, matrix, render_token,
    to_srgb, xyz_to_encoded,
};
pub use tables::{
    LogLutMeta, LutMeta, LutTableResult, OUTPUT_SPACES, display_lut_u8, lut_bytes, space_id,
};
