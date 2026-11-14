//! Full-resolution export pipeline and source photo metadata reader (`docs/mobile.md`).
//!
//! Currently provides [`meta`]: source EXIF extraction (`write_with_exif`), photo display text
//! (`finish/meta_text.py`), JPEG/DNG metadata (`meta.py`), and embedded DNG preview location.
#![forbid(unsafe_code)]

pub mod meta;
