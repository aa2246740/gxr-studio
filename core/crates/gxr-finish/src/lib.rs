//! Port of `gxr_studio/crop.py`, `gxr_studio/finish/geometry.py`, and `gxr_studio/gpu/box.py`.
//!
//! Frame and stamp layout is `gxr-frame`. Raster comes later.
#![forbid(unsafe_code)]

pub mod crop;
pub mod geometry;
#[path = "box.rs"]
pub mod gpu_box;

pub use geometry::{Geometry, parse_geometry, warp, warp_valid};
pub use gpu_box::{final_box, final_box_json};
