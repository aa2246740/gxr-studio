//! Full-resolution export pipeline: HDR track ([`hdr_track`]), source photo metadata ([`meta`]), and
//! the export base picture ([`source`]) (`gxr_studio/export.py`, `finish/hdr_export.py`, `docs/mobile.md`).
//!
//! Provides [`hdr_track`] (`begin`/`warp`/`box`/`crop`/`stamp`/`frame`/`finish`), [`meta`] (source
//! EXIF extraction, photo display text, JPEG/DNG metadata, embedded DNG preview location), and [`source`].
#![forbid(unsafe_code)]

pub mod hdr_track;
pub mod meta;
pub mod source;

pub use hdr_track::{
    FinishOutput, HdrTrackState, NO_RAW, NOT_JPEG, ToGridOutput, begin, box_track,
    checked_box_dim_f64, checked_box_offset_f64, checked_dim_f64, checked_factor_f64,
    checked_nonneg_u64_f64, crop_track, finish, frame_track, parse_params_opt, parse_rect4_json,
    stamp_track, to_grid, warp_track,
};
