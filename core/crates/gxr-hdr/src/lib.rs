//! Port of `gxr_studio/hdr/` for the phone: the gain map and the Ultra HDR container.
//!
//! `derived.py` and `finish/hdr_export.py` are not in this crate. The gain JPEG
//! encoder is wave 2 (`gxr-jpeg`); this crate quantises the plane and wraps bytes.
#![forbid(unsafe_code)]

mod capture;
mod container;
mod error;
mod extend;
mod fft;
mod filter;
mod gain;
mod label;
mod number;
mod residual;
mod sample;

pub use capture::{Field, MISMATCH, capture_of, differs, field_json};
pub use container::{
    GainMeta, MPF_SEG_LEN, Segment, app_segment, assemble, iso_full, iso_version_only, jpeg_segments,
    mpf_segment, xmp_gainmap, xmp_primary,
};
pub use filter::{Plane, gaussian_filter, minimum_filter, uniform_filter};
pub use gain::{
    Alignment, GainPlane, Params, align, block_mean, check_alignment, compute_gain, fit_curve, grid_shape,
    quantise_gain, sdr_luma_grid, srgb_to_linear, warp,
};
pub use label::{binary_dilation, label_f64, label_u8};
pub use number::{frac_65536, py_round};
pub use sample::map_coordinates;
