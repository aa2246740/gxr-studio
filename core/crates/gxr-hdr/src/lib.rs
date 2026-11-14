//! Port of `gxr_studio/hdr/` for the phone: the gain map and the Ultra HDR container.
//!
//! `derived.py` (the library cache) and `finish/hdr_export.py` (the export track, wave 2)
//! are not in this crate. Encoding the gain JPEG is `gxr-jpeg`'s job; this crate produces
//! the uint8 plane that goes into it and wraps the bytes that come out.
#![forbid(unsafe_code)]

mod capture;
mod container;
mod curve;
mod elem;
mod error;
mod extend;
mod fft;
mod filter;
mod gain;
mod input;
mod label;
mod np;
mod number;
mod report;
mod residual;
mod sample;
mod size;
mod unicode_tables;

pub use capture::{Field, MISMATCH, TagValue, capture_of, differs, field_json};
pub use container::{
    GainMeta, MPF_SEG_LEN, Segment, app_segment, assemble, iso_full, iso_version_only,
    jpeg_segments, mpf_segment, xmp_gainmap, xmp_primary,
};
pub use curve::{GainResult, Params, compute_gain, fit_curve, quantise_gain};
pub use elem::{log2f, powf32};
pub use error::unsupported;
pub use fft::phase_shift;
pub use filter::{gaussian_filter, gaussian_kernel, minimum_filter, uniform_filter};
pub use gain::{
    Alignment, LUMA, Plane32, RAW_WEIGHTS, align, block_mean, check_alignment, grid_shape, hp32,
    is_unsupported, norm, raw_luma, register, sdr_luma_grid, srgb_to_linear, warp,
};
pub use input::{Value, meta_from_json, params_from_json, parse as parse_json, tags_from_json};
pub use label::{binary_dilation, label};
pub use number::{fmt_g7, limit_65536, py_round};
pub use report::{align_report, ensure_finite, gain_report};
pub use residual::local_residual;
pub use sample::map_coordinates;
pub use size::{ARRAY_TOO_BIG, count};
