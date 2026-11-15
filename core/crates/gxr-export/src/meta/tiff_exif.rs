//! Pillow 12.2.0-compatible TIFF/DNG and JPEG container opening and EXIF extraction.
//!
//! Mirrors `PIL.TiffImagePlugin.TiffImageFile`, `PIL.JpegImagePlugin.JpegImageFile`, and
//! `PIL.Image.Exif`.

use std::collections::BTreeMap;

pub use super::exif_value::{Endian, ExifValue, bytes_to_hex};
pub use super::tiff_ifd::{
    EXIF_IFD_TAG, Ifd, is_known_compression, is_known_open_info, load_ifd, parse_tiff_header,
};

#[derive(Clone, Debug)]
pub struct TiffDoc {
    pub endian: Endian,
    pub bigtiff: bool,
    pub ifd0_offset: u64,
    pub width: u32,
    pub height: u32,
    pub tag_v2: Ifd,
}

#[derive(Clone, Debug)]
pub struct JpegDoc {
    pub width: u32,
    pub height: u32,
    pub exif_bytes: Option<Vec<u8>>,
    pub xmp_bytes: Option<Vec<u8>>,
}

#[derive(Clone, Debug)]
pub enum ImageDoc {
    Jpeg(JpegDoc),
    Tiff(TiffDoc),
}

pub const HARD_MAX_IMAGE_PIXELS: u64 = 178_956_970;

pub(crate) fn is_open_fallback_err(e: &str) -> bool {
    e.starts_with("SyntaxError:")
        || e.starts_with("IndexError:")
        || e.starts_with("TypeError:")
        || e.starts_with("UnidentifiedImageError:")
}

fn check_decompression_bomb(w: u32, h: u32) -> Result<(), String> {
    let pixels = u64::from(w) * u64::from(h);
    if pixels > HARD_MAX_IMAGE_PIXELS {
        return Err(format!(
            "DecompressionBombError: Image size ({pixels} pixels) exceeds limit of {HARD_MAX_IMAGE_PIXELS} pixels, could be decompression bomb DOS attack."
        ));
    }
    Ok(())
}

pub fn open_image(bytes: &[u8]) -> Result<ImageDoc, String> {
    if bytes.starts_with(b"\xff\xd8\xff") {
        match parse_jpeg(bytes) {
            Ok(jpg) => return Ok(ImageDoc::Jpeg(jpg)),
            Err(e) if is_open_fallback_err(&e) => {}
            Err(e) => return Err(e),
        }
    }
    match parse_tiff(bytes) {
        Ok(tiff) => Ok(ImageDoc::Tiff(tiff)),
        Err(e) if is_open_fallback_err(&e) => {
            Err("UnidentifiedImageError: cannot identify image file".to_string())
        }
        Err(e) => Err(e),
    }
}

/// Port of `PIL.JpegImagePlugin.JpegImageFile._open`.
pub fn parse_jpeg(bytes: &[u8]) -> Result<JpegDoc, String> {
    if !bytes.starts_with(b"\xff\xd8\xff") {
        return Err("SyntaxError: not a JPEG file".to_string());
    }
    let mut pos = 2usize;
    let mut s_byte = bytes[pos];
    pos += 1;

    let mut width: Option<u32> = None;
    let mut height: Option<u32> = None;
    let mut exif_bytes: Option<Vec<u8>> = None;
    let mut xmp_bytes: Option<Vec<u8>> = None;
    let mut saw_sos = false;

    loop {
        let marker: u16 = if s_byte == 0xFF {
            let Some(&next_b) = bytes.get(pos) else {
                break;
            };
            pos += 1;
            0xFF00 | u16::from(next_b)
        } else {
            let Some(&next_b) = bytes.get(pos) else {
                break;
            };
            pos += 1;
            s_byte = next_b;
            continue;
        };

        if marker == 0 || marker == 0xFFFF {
            s_byte = 0xFF;
            continue;
        }
        if marker == 0xFF00 {
            let Some(&next_b) = bytes.get(pos) else {
                break;
            };
            pos += 1;
            s_byte = next_b;
            continue;
        }

        match marker {
            // Markers with no payload (RST0..RST7, SOI, EOI, TEM)
            0xFFD0..=0xFFD9 | 0xFF01 => {
                if marker == 0xFFD9 {
                    // EOI reached without SOS
                    break;
                }
            }
            // APP0..APP15
            0xFFE0..=0xFFEF => {
                let Some(len_bytes) = bytes.get(pos..).and_then(|s| s.get(..2)) else {
                    break;
                };
                pos += 2;
                let seg_len = usize::from(u16::from_be_bytes([len_bytes[0], len_bytes[1]]));
                if seg_len < 2 {
                    break;
                }
                let payload_len = seg_len - 2;
                let Some(payload) = bytes.get(pos..).and_then(|s| s.get(..payload_len)) else {
                    return Err("OSError: Truncated File Read".to_string());
                };
                pos += payload_len;
                if marker == 0xFFE1 {
                    if let Some(rest) = payload.strip_prefix(b"Exif\x00\x00") {
                        match &mut exif_bytes {
                            Some(buf) => {
                                if buf.try_reserve_exact(rest.len()).is_err() {
                                    return Err("MemoryError: out of memory".to_string());
                                }
                                buf.extend_from_slice(rest);
                            }
                            None => {
                                let mut buf = Vec::new();
                                if buf.try_reserve_exact(payload.len()).is_err() {
                                    return Err("MemoryError: out of memory".to_string());
                                }
                                buf.extend_from_slice(payload);
                                exif_bytes = Some(buf);
                            }
                        }
                    } else if let Some(rest) =
                        payload.strip_prefix(b"http://ns.adobe.com/xap/1.0/\x00")
                    {
                        let mut buf = Vec::new();
                        if buf.try_reserve_exact(rest.len()).is_err() {
                            return Err("MemoryError: out of memory".to_string());
                        }
                        buf.extend_from_slice(rest);
                        xmp_bytes = Some(buf);
                    }
                }
            }
            // SOF markers (excluding DHT 0xFFC4, JPG 0xFFC8, DAC 0xFFCC)
            0xFFC0..=0xFFC3 | 0xFFC5..=0xFFC7 | 0xFFC9..=0xFFCB | 0xFFCD..=0xFFCF => {
                let Some(len_bytes) = bytes.get(pos..).and_then(|s| s.get(..2)) else {
                    break;
                };
                pos += 2;
                let seg_len = usize::from(u16::from_be_bytes([len_bytes[0], len_bytes[1]]));
                if seg_len < 2 {
                    break;
                }
                let payload_len = seg_len - 2;
                let Some(payload) = bytes.get(pos..).and_then(|s| s.get(..payload_len)) else {
                    return Err("OSError: Truncated File Read".to_string());
                };
                pos += payload_len;
                if payload.len() < 6 {
                    return Err("SyntaxError: truncated SOF".to_string());
                }
                let bits = payload[0];
                if bits != 8 {
                    return Err(format!("SyntaxError: cannot handle {bits}-bit layers"));
                }
                let h = u32::from(u16::from_be_bytes([payload[1], payload[2]]));
                let w = u32::from(u16::from_be_bytes([payload[3], payload[4]]));
                let layers = payload[5];
                if !matches!(layers, 1 | 3 | 4) {
                    return Err(format!("SyntaxError: cannot handle {layers}-layer images"));
                }
                width = Some(w);
                height = Some(h);
            }
            // SOS
            0xFFDA => {
                let Some(len_bytes) = bytes.get(pos..).and_then(|s| s.get(..2)) else {
                    break;
                };
                pos += 2;
                let seg_len = usize::from(u16::from_be_bytes([len_bytes[0], len_bytes[1]]));
                if seg_len < 2 {
                    break;
                }
                if bytes
                    .get(pos..)
                    .and_then(|s| s.get(..seg_len - 2))
                    .is_none()
                {
                    return Err("OSError: Truncated File Read".to_string());
                }
                saw_sos = true;
                break;
            }
            // Other sized markers (DHT, DQT, DRI, COM, etc.)
            0xFFC4 | 0xFFC8 | 0xFFCC | 0xFFDB..=0xFFFE => {
                let Some(len_bytes) = bytes.get(pos..).and_then(|s| s.get(..2)) else {
                    break;
                };
                pos += 2;
                let seg_len = usize::from(u16::from_be_bytes([len_bytes[0], len_bytes[1]]));
                if seg_len < 2 {
                    break;
                }
                let payload_len = seg_len - 2;
                if bytes
                    .get(pos..)
                    .and_then(|s| s.get(..payload_len))
                    .is_none()
                {
                    return Err("OSError: Truncated File Read".to_string());
                }
                pos += payload_len;
            }
            _ => {
                return Err("SyntaxError: no marker found".to_string());
            }
        }

        let Some(&next_b) = bytes.get(pos) else {
            break;
        };
        pos += 1;
        s_byte = next_b;
    }

    if !saw_sos {
        return Err("UnidentifiedImageError: no SOS marker".to_string());
    }
    let (Some(w), Some(h)) = (width, height) else {
        return Err("UnidentifiedImageError: missing SOF".to_string());
    };
    check_decompression_bomb(w, h)?;
    Ok(JpegDoc {
        width: w,
        height: h,
        exif_bytes,
        xmp_bytes,
    })
}

/// Port of `PIL.TiffImagePlugin.TiffImageFile._open` + `_setup`.
pub fn parse_tiff(bytes: &[u8]) -> Result<TiffDoc, String> {
    let hdr = bytes
        .get(..8)
        .ok_or_else(|| "SyntaxError: truncated TIFF header".to_string())?;
    let (endian, bigtiff, ifd0_offset) = parse_tiff_header(hdr, bytes)?;
    let tag_v2 = load_ifd(bytes, endian, bigtiff, ifd0_offset, None, false)?;

    // TiffImageFile._setup checks:
    if tag_v2.tags.contains_key(&0xBC01) {
        return Err("OSError: Windows Media Photo files not yet supported".to_string());
    }
    let compression = tag_v2
        .tags
        .get(&259)
        .and_then(ExifValue::as_i64)
        .unwrap_or(1);
    if !is_known_compression(compression) {
        return Err("SyntaxError: unknown compression".to_string());
    }
    let planar = tag_v2
        .tags
        .get(&284)
        .and_then(ExifValue::as_i64)
        .unwrap_or(1);
    let mut photo = tag_v2
        .tags
        .get(&262)
        .and_then(ExifValue::as_i64)
        .unwrap_or(0);
    if compression == 6 {
        photo = 6;
    }
    let fillorder = tag_v2
        .tags
        .get(&266)
        .and_then(ExifValue::as_i64)
        .unwrap_or(1);

    let xsize_i64 = tag_v2
        .tags
        .get(&256)
        .and_then(ExifValue::as_i64)
        .ok_or_else(|| "TypeError: Missing dimensions".to_string())?;
    let ysize_i64 = tag_v2
        .tags
        .get(&257)
        .and_then(ExifValue::as_i64)
        .ok_or_else(|| "TypeError: Missing dimensions".to_string())?;
    let xsize =
        u32::try_from(xsize_i64).map_err(|_| "ValueError: Invalid dimensions".to_string())?;
    let ysize =
        u32::try_from(ysize_i64).map_err(|_| "ValueError: Invalid dimensions".to_string())?;

    let mut sample_format = tag_v2
        .tags
        .get(&339)
        .and_then(ExifValue::as_ints)
        .unwrap_or_else(|| vec![1]);
    if sample_format.len() > 1 {
        let min = sample_format.iter().copied().min();
        let max = sample_format.iter().copied().max();
        if min == max
            && let Some(v) = sample_format.first().copied()
        {
            sample_format = vec![v];
        }
    }

    let mut bps = tag_v2
        .tags
        .get(&258)
        .and_then(ExifValue::as_ints)
        .unwrap_or_else(|| vec![1]);
    let mut extra = tag_v2
        .tags
        .get(&338)
        .and_then(ExifValue::as_ints)
        .unwrap_or_default();
    let default_spp = if compression == 6 && (photo == 2 || photo == 6) {
        3
    } else {
        1
    };
    let mut spp = tag_v2
        .tags
        .get(&277)
        .and_then(ExifValue::as_i64)
        .unwrap_or(default_spp);
    if planar == 2 && !extra.is_empty() && extra.iter().copied().max() == Some(0) {
        let drop_n = extra.len();
        if bps.len() >= drop_n {
            bps.truncate(bps.len() - drop_n);
        }
        spp -= i64::try_from(drop_n).unwrap_or(0);
        extra.clear();
    }
    if spp > 4 {
        return Err("SyntaxError: Invalid value for samples per pixel".to_string());
    }
    let Ok(spp_usize) = usize::try_from(spp) else {
        return Err("SyntaxError: unknown data organization".to_string());
    };
    if spp_usize < bps.len() {
        bps.truncate(spp_usize);
    } else if spp_usize > bps.len() && bps.len() == 1 {
        bps = vec![bps[0]; spp_usize];
    }
    if bps.len() != spp_usize {
        return Err("SyntaxError: unknown data organization".to_string());
    }

    if !is_known_open_info(
        endian.prefix(),
        photo,
        &sample_format,
        fillorder,
        &bps,
        &extra,
    ) {
        return Err("SyntaxError: unknown pixel mode".to_string());
    }

    // For compression == 1 ("raw") without libtiff, Pillow checks StripOffsets (273) or TileOffsets (324).
    if compression == 1 && !tag_v2.tags.contains_key(&273) && !tag_v2.tags.contains_key(&324) {
        return Err("SyntaxError: unknown data organization".to_string());
    }

    let orient = tag_v2
        .tags
        .get(&274)
        .and_then(ExifValue::as_i64)
        .unwrap_or(1);
    let (width, height) = if matches!(orient, 5..=8) {
        (ysize, xsize)
    } else {
        (xsize, ysize)
    };
    check_decompression_bomb(width, height)?;

    Ok(TiffDoc {
        endian,
        bigtiff,
        ifd0_offset,
        width,
        height,
        tag_v2,
    })
}

/// Result of `im.getexif()` + `exif.get_ifd(0x8769)`: top-level IFD0 + EXIF sub-IFD.
#[derive(Clone, Debug, Default)]
pub struct LoadedExif {
    pub base: BTreeMap<u16, ExifValue>,
    pub sub: BTreeMap<u16, ExifValue>,
}

/// Port of `Image.getexif()` + `get_ifd(0x8769)` on an already-opened JPEG or TIFF.
pub fn load_image_exif(bytes: &[u8], doc: &ImageDoc) -> LoadedExif {
    match doc {
        ImageDoc::Jpeg(jpg) => load_jpeg_exif(jpg),
        ImageDoc::Tiff(tiff) => load_tiff_exif(bytes, tiff),
    }
}

fn load_jpeg_exif(jpg: &JpegDoc) -> LoadedExif {
    let mut out = LoadedExif::default();
    if let Some(raw) = jpg.exif_bytes.as_deref() {
        let mut slice = raw;
        while let Some(rest) = slice.strip_prefix(b"Exif\x00\x00") {
            slice = rest;
        }
        if !slice.is_empty()
            && let Some(hdr) = slice.get(..8)
            && let Ok((endian, bigtiff, ifd0_off)) = parse_tiff_header(hdr, slice)
            && let Ok(ifd0) = load_ifd(slice, endian, bigtiff, ifd0_off, None, true)
        {
            out.base = ifd0.tags;
            if let Some(sub_off_val) = out.base.get(&EXIF_IFD_TAG)
                && let Ok(Some(sub_off)) = sub_off_val.to_python_int()
                && let Some(sub_off_i64) = sub_off.as_i64()
                && let Ok(sub_off_u64) = u64::try_from(sub_off_i64)
                && let Ok(sub_ifd) = load_ifd(
                    slice,
                    endian,
                    bigtiff,
                    sub_off_u64,
                    Some(EXIF_IFD_TAG),
                    true,
                )
            {
                out.sub = sub_ifd.tags;
            }
        }
    }
    if !out.base.contains_key(&274)
        && let Some(xmp) = jpg.xmp_bytes.as_deref()
        && let Some(orient) = parse_xmp_orientation(xmp)
    {
        out.base.insert(274, ExifValue::Int(orient));
    }
    out
}

fn load_tiff_exif(bytes: &[u8], tiff: &TiffDoc) -> LoadedExif {
    let mut out = LoadedExif::default();
    if let Ok(ifd0) = load_ifd(
        bytes,
        tiff.endian,
        tiff.bigtiff,
        tiff.ifd0_offset,
        None,
        true,
    ) {
        out.base = ifd0.tags;
        if let Some(sub_off_val) = out.base.get(&EXIF_IFD_TAG)
            && let Ok(Some(sub_off)) = sub_off_val.to_python_int()
            && let Some(sub_off_i64) = sub_off.as_i64()
            && let Ok(sub_off_u64) = u64::try_from(sub_off_i64)
            && let Ok(sub_ifd) = load_ifd(
                bytes,
                tiff.endian,
                tiff.bigtiff,
                sub_off_u64,
                Some(EXIF_IFD_TAG),
                true,
            )
        {
            out.sub = sub_ifd.tags;
        }
    }
    // Check XMP tag 700 if 274 absent (Pillow sets self.info["xmp"] from tag 700 in TIFF if present)
    if !out.base.contains_key(&274)
        && let Some(ExifValue::Bytes(xmp)) = tiff.tag_v2.tags.get(&700)
        && let Some(orient) = parse_xmp_orientation(xmp)
    {
        out.base.insert(274, ExifValue::Int(orient));
    }
    out
}

fn parse_xmp_orientation(xmp: &[u8]) -> Option<i64> {
    let needle = b"tiff:Orientation";
    let mut pos = 0usize;
    while let Some(rel) = xmp[pos..].windows(needle.len()).position(|w| w == needle) {
        let after = pos + rel + needle.len();
        if let Some(&sep) = xmp.get(after) {
            if sep == b'>'
                && let Some(&digit) = xmp.get(after + 1)
                && digit.is_ascii_digit()
            {
                return Some(i64::from(digit - b'0'));
            }
            if sep == b'='
                && xmp.get(after + 1) == Some(&b'"')
                && let Some(&digit) = xmp.get(after + 2)
                && digit.is_ascii_digit()
            {
                return Some(i64::from(digit - b'0'));
            }
        }
        pos = after;
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn open_image_preserves_truncated_oserror_and_decompression_bomb() {
        let truncated_app1 = b"\xff\xd8\xff\xe1\x00\x80Exif\x00\x00II*\x00";
        let err = open_image(truncated_app1).unwrap_err();
        assert_eq!(err, "OSError: Truncated File Read");

        let huge_jpg = b"\xff\xd8\xff\xc0\x00\x0b\x08\x27\x10\x4e\x20\x01\x01\x11\x00\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xff\xd9";
        let err = open_image(huge_jpg).unwrap_err();
        assert!(err.starts_with("DecompressionBombError:"), "{err}");

        let jfif_no_sof = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00";
        let err = open_image(jfif_no_sof).unwrap_err();
        assert!(err.starts_with("UnidentifiedImageError:"), "{err}");
    }
}
