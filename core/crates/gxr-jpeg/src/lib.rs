//! JPEG encoding with EXIF and sRGB ICC profile, plus JPEG container segment helpers.
//!
//! Port of the single-encode JPEG path in `gxr_studio/export.py` (`srgb_icc`, `build_exif`,
//! `_finish`) and the segment helpers modeled on `gxr_studio/hdr/container.py` (`jpeg_segments`,
//! `app_segment`).
#![forbid(unsafe_code)]

pub mod encode;
pub mod exif;
mod exif_tiff;
pub mod icc;
mod jpeg_enc;
pub mod json_parse;
pub mod segments;

pub use encode::encode_jpeg;
pub use exif::{ExifSpec, ExifVal, SOFTWARE, build_exif, parse_exif_json};
pub use icc::{SRGB_ICC, srgb_icc};
pub use segments::{
    EXIF_SIG, ICC_SIG, MAX_MARKER_PAYLOAD, Segment, app_segment, icc_segments, jpeg_segments,
    marker_name, segments_json, set_meta, sof_dimensions,
};
