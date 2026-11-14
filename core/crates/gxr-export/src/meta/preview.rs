//! Port of `gxr_studio/meta.py:181 dng_preview_image(embedded_only=True)` (location only).

use gxr_jpeg::jpeg_segments;
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

/// Port of `gxr_studio/meta.py:181 dng_preview_image(embedded_only=True)`.
///
/// Finds `{offset, length}` of the largest embedded JPEG preview SubIFD (`Compression == 7`,
/// `<= 32 MiB`) that Pillow's JPEG decoder can open (`jpeg_segments` valid + 8-bit 1/3/4-layer
/// baseline/extended/progressive DCT SOF). Returns `None` if no valid embedded preview exists.
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
            .unwrap_or(0);
        let h = ifd
            .tags
            .get(&257)
            .and_then(|v| v.to_python_int().ok().flatten())
            .unwrap_or(0);
        let area = i128::from(w) * i128::from(h);
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

    let candidate = best?;
    let slice = bytes.get(candidate.offset..candidate.offset + candidate.length)?;
    if is_pillow_decodable_jpeg(slice) {
        Some(candidate)
    } else {
        None
    }
}

/// Check that `slice` is a Pillow-openable & loadable JPEG preview (filters out 12/14-bit
/// lossless SOF3 `0xFFC3` previews embedded in some GR/GR3/GR4 DNGs).
fn is_pillow_decodable_jpeg(slice: &[u8]) -> bool {
    let Ok((segs, _, _)) = jpeg_segments(slice, 0) else {
        return false;
    };
    for seg in &segs {
        // Baseline DCT (0xC0), Extended Sequential DCT (0xC1), Progressive DCT (0xC2).
        if matches!(seg.marker, 0xC0..=0xC2) {
            let payload = seg.payload(slice);
            if payload.len() < 6 {
                return false;
            }
            let bits = payload[0];
            let h = u16::from_be_bytes([payload[1], payload[2]]);
            let w = u16::from_be_bytes([payload[3], payload[4]]);
            let layers = payload[5];
            return bits == 8 && w > 0 && h > 0 && matches!(layers, 1 | 3 | 4);
        }
    }
    false
}
