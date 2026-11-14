//! Port of `gxr_studio/meta.py:181 dng_preview_image(embedded_only=True)` (location only).

use gxr_raw::Json;

use super::tiff_exif::{ExifValue, load_ifd, parse_tiff};

const MAX_PREVIEW_BYTES: usize = 32 * 1024 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PreviewRange {
    pub offset: usize,
    pub length: usize,
}

impl PreviewRange {
    pub fn to_json(&self) -> Json {
        Json::object([
            ("offset", Json::Int(self.offset as i64)),
            ("length", Json::Int(self.length as i64)),
        ])
    }
}

/// Port of `gxr_studio/meta.py:181 dng_preview_image(embedded_only=True)` (structural selection).
///
/// Finds `{offset, length}` of the largest-area embedded JPEG preview SubIFD (`Compression == 7`,
/// `0 < count <= 32 MiB`, all `count` bytes readable within bounds). Decoding and broken-JPEG
/// rejection stay with the caller (browser on mobile, Pillow in parity tests), matching Python's
/// single-candidate decode without fallback.
pub fn dng_preview_location(bytes: &[u8]) -> Option<PreviewRange> {
    let tiff = parse_tiff(bytes).ok()?;
    let sub_offs = tiff.tag_v2.tags.get(&330).and_then(ExifValue::as_ints)?;

    let mut best: Option<PreviewRange> = None;
    let mut best_area: i128 = 0;

    for off in sub_offs {
        let Ok(off_u64) = u64::try_from(off) else {
            continue;
        };
        let Ok(ifd) = load_ifd(bytes, tiff.endian, tiff.bigtiff, off_u64, None, false) else {
            continue;
        };
        if ifd.tags.get(&259) != Some(&ExifValue::Int(7)) {
            continue;
        }
        let Some(starts) = ifd.tags.get(&273).and_then(ExifValue::as_ints) else {
            continue;
        };
        let Some(counts) = ifd.tags.get(&279).and_then(ExifValue::as_ints) else {
            continue;
        };
        if starts.is_empty() || counts.is_empty() {
            continue;
        }
        let start = starts[0];
        let count = counts[0];
        if count <= 0 || count > (MAX_PREVIEW_BYTES as i64) || start < 0 {
            continue;
        }
        let w = ifd
            .tags
            .get(&256)
            .and_then(|v| v.to_python_int().ok().flatten())
            .and_then(|v| v.as_i128())
            .unwrap_or(0);
        let h = ifd
            .tags
            .get(&257)
            .and_then(|v| v.to_python_int().ok().flatten())
            .and_then(|v| v.as_i128())
            .unwrap_or(0);
        let area = w * h;
        if area <= best_area {
            continue;
        }
        let Ok(start_u) = usize::try_from(start) else {
            continue;
        };
        let Ok(count_u) = usize::try_from(count) else {
            continue;
        };
        let Some(end_u) = start_u.checked_add(count_u) else {
            continue;
        };
        if bytes.get(start_u..end_u).is_none() {
            continue;
        }
        best = Some(PreviewRange {
            offset: start_u,
            length: count_u,
        });
        best_area = area;
    }

    best
}
