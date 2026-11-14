//! Source photo metadata extraction matching desktop Pillow 12.2.0 (`export.py`, `meta.py`,
//! `finish/meta_text.py`).

pub mod exif;
pub mod exif_value;
pub mod meta_text;
pub mod photo_meta;
pub mod preview;
pub mod tiff_exif;
pub mod tiff_ifd;

pub use exif::{SourceExif, clean_text, read_source_exif};
pub use meta_text::{
    PhotoTextResult, datetime_from_mtime_ms, format_py_g, frame_date, photo_meta_from_bytes, ratio,
};
pub use photo_meta::{
    DEFAULT_NOW_YEAR, MIN_YEAR, NaiveDateTime, PhotoMeta, extract_copyright_text,
    extract_recipe_tag, oriented, parse_exif_datetime, parse_orientation, plausible, read_dng_meta,
    read_jpeg_meta,
};
pub use preview::{PreviewRange, dng_preview_location};
pub use tiff_exif::ExifValue;
