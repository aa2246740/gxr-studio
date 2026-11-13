//! Port of `gxr_studio/raw/` (TIFF/DNG reading, mosaic, white-balanced codes), the GPU RAW
//! layout of `gxr_studio/gpu/source.py`, and the colour parts of `gxr_studio/lut/` it needs.
//!
//! Every function mirrors one Python function (see its `Port of …` line). Errors are
//! `CoreError`s whose `kind` and `message` match what Python raises; nothing panics on bad input.
//! `json` is the writer `gxr-cli` and `gxr-wasm` share, so both print the same bytes.
#![forbid(unsafe_code)]

pub mod bounds;
pub mod codes;
pub mod colorimetry;
pub mod demosaic;
pub mod dng;
pub mod error;
pub mod gpu_source;
pub mod json;
pub mod linalg;
pub mod mosaic;
pub mod pyrepr;
pub mod render;
pub mod scene;
pub mod sha256;
pub mod summary;
pub mod tiff;

pub use codes::{CODE_MAX, Image, half_res_codes, orient_array, wb_gains, wb_mosaic_block};
pub use demosaic::{PAD, demosaic_block, demosaic_full, demosaic_strip, demosaic_strips};
pub use dng::Dng;
pub use error::CoreError;
pub use gpu_source::{RawSource, describe, describe_json, error_json};
pub use json::Json;
pub use render::{neutral_display, render_full, srgb_encode};
