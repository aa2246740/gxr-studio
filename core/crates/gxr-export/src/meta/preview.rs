//! Port of `gxr_studio/meta.py:181 dng_preview_image(embedded_only=True)` (location only).

use gxr_raw::Json;

use super::exif_value::PyInt;
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
    let mut best_area = PyInt::from_i64(0);

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
        let w = match ifd.tags.get(&256) {
            Some(v) => v.to_python_int().ok().flatten()?,
            None => PyInt::from_i64(0),
        };
        let h = match ifd.tags.get(&257) {
            Some(v) => v.to_python_int().ok().flatten()?,
            None => PyInt::from_i64(0),
        };
        let area = w.try_mul(&h)?;
        if count <= 0 || count > (MAX_PREVIEW_BYTES as i64) || area <= best_area || start < 0 {
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

#[cfg(test)]
mod tests {
    use super::*;

    fn build_double_dim_preview_dng(w: f64, h: f64) -> Vec<u8> {
        let jpeg = b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x06\x00\x08\x01\x01\x11\x00\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\xff\xd9";
        let mut out = Vec::new();
        // TIFF LE header: IFD0 at offset 8
        out.extend_from_slice(b"II*\x00\x08\x00\x00\x00");
        // IFD0: 5 entries: ImageWidth(256), ImageLength(257), StripOffsets(273), StripByteCounts(279), SubIFDs(330)
        // IFD0 ends at 8 + 2 + 5*12 + 4 = 74
        let subifd_off = 74u32;
        out.extend_from_slice(&5u16.to_le_bytes());
        for (tag, typ, cnt, val) in [
            (256u16, 4u16, 1u32, 16u32),
            (257u16, 4u16, 1u32, 12u32),
            (273u16, 4u16, 1u32, 8u32),
            (279u16, 4u16, 1u32, 16u32),
            (330u16, 4u16, 1u32, subifd_off),
        ] {
            out.extend_from_slice(&tag.to_le_bytes());
            out.extend_from_slice(&typ.to_le_bytes());
            out.extend_from_slice(&cnt.to_le_bytes());
            out.extend_from_slice(&val.to_le_bytes());
        }
        out.extend_from_slice(&0u32.to_le_bytes());

        // SubIFD at 74: 6 entries (254, 256(DOUBLE), 257(DOUBLE), 259, 273, 279) -> 2 + 6*12 + 4 = 78 bytes -> ends at 152
        let w_off = 152u32;
        let h_off = 160u32;
        let jpeg_off = 168u32;
        out.extend_from_slice(&6u16.to_le_bytes());
        for (tag, typ, cnt, val) in [
            (254u16, 4u16, 1u32, 1u32),
            (256u16, 12u16, 1u32, w_off),
            (257u16, 12u16, 1u32, h_off),
            (259u16, 3u16, 1u32, 7u32),
            (273u16, 4u16, 1u32, jpeg_off),
            (279u16, 4u16, 1u32, jpeg.len() as u32),
        ] {
            out.extend_from_slice(&tag.to_le_bytes());
            out.extend_from_slice(&typ.to_le_bytes());
            out.extend_from_slice(&cnt.to_le_bytes());
            out.extend_from_slice(&val.to_le_bytes());
        }
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&w.to_le_bytes());
        out.extend_from_slice(&h.to_le_bytes());
        out.extend_from_slice(jpeg);
        out
    }

    #[test]
    fn preview_area_uses_arbitrary_precision_for_large_double_dims() {
        // 2^64 * 2^64 = 2^128 (overflows i128) must be selected cleanly.
        let dng_2p64 = build_double_dim_preview_dng(
            18_446_744_073_709_551_616.0,
            18_446_744_073_709_551_616.0,
        );
        assert_eq!(
            dng_preview_location(&dng_2p64),
            Some(PreviewRange {
                offset: 168,
                length: 27,
            })
        );

        // 1e100 * 1.0 (exceeds i128::MAX) must be selected cleanly.
        let dng_1e100 = build_double_dim_preview_dng(1e100, 1.0);
        assert_eq!(
            dng_preview_location(&dng_1e100),
            Some(PreviewRange {
                offset: 168,
                length: 27,
            })
        );

        // (-2^64) * (-2^64) = +2^128 > 0 must be selected cleanly.
        let dng_neg_neg = build_double_dim_preview_dng(
            -18_446_744_073_709_551_616.0,
            -18_446_744_073_709_551_616.0,
        );
        assert_eq!(
            dng_preview_location(&dng_neg_neg),
            Some(PreviewRange {
                offset: 168,
                length: 27,
            })
        );

        // (-2^64) * 2 = negative area <= 0 -> skipped.
        let dng_neg_pos = build_double_dim_preview_dng(-18_446_744_073_709_551_616.0, 2.0);
        assert_eq!(dng_preview_location(&dng_neg_pos), None);
    }
}
