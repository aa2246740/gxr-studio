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
        || e.starts_with("struct.error:")
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

fn validate_dqt(mut rest: &[u8]) -> Result<(), String> {
    while !rest.is_empty() {
        let prec = if (rest[0] >> 4) == 0 { 1 } else { 2 };
        let qt_len = 1 + prec * 64;
        if rest.len() < qt_len {
            return Err("SyntaxError: bad quantization table marker".to_string());
        }
        rest = &rest[qt_len..];
    }
    Ok(())
}

fn validate_photoshop_app13(s: &[u8]) -> Result<(), String> {
    let mut offset = 14usize;
    while s.get(offset..offset + 4) == Some(b"8BIM".as_slice()) {
        offset += 4;
        let Some(code_bytes) = s.get(offset..offset + 2) else {
            // `i16(s, offset)` raises `struct.error`, caught by Pillow's `except struct.error`.
            break;
        };
        let code = u16::from_be_bytes([code_bytes[0], code_bytes[1]]);
        offset += 2;
        let Some(&name_len) = s.get(offset) else {
            // `s[offset]` raises `IndexError`, NOT caught by `except struct.error`.
            return Err("IndexError: index out of range".to_string());
        };
        offset += 1 + usize::from(name_len);
        offset += offset & 1;
        let Some(sz_bytes) = s.get(offset..offset + 4) else {
            // `i32(s, offset)` raises `struct.error`, caught cleanly.
            break;
        };
        let size = u32::from_be_bytes([sz_bytes[0], sz_bytes[1], sz_bytes[2], sz_bytes[3]]);
        offset += 4;
        let Ok(size_u) = usize::try_from(size) else {
            break;
        };
        if code == 0x03ED {
            let end = offset.saturating_add(size_u).min(s.len());
            if end.saturating_sub(offset) < 14 {
                break;
            }
        }
        let Some(next) = offset
            .checked_add(size_u)
            .and_then(|o| o.checked_add(o & 1))
        else {
            break;
        };
        offset = next;
    }
    Ok(())
}

fn validate_mpf(mp_bytes: &[u8]) -> Result<(), String> {
    // Mirror `JpegImagePlugin.jpeg_factory` + `_getmp`:
    // `SyntaxError`, `TypeError`, `IndexError` fall back to base JPEG, while
    // `struct.error` (short `0xB002` MP entries buffer) propagates to `Image.open`.
    let Some(hdr) = mp_bytes.get(..8) else {
        return Ok(());
    };
    let Ok((endian, bigtiff, ifd0_off)) = parse_tiff_header(hdr, hdr) else {
        return Ok(());
    };
    let Ok(ifd0) = load_ifd(mp_bytes, endian, bigtiff, ifd0_off, None, false) else {
        return Ok(());
    };
    let Some(quant_val) = ifd0.tags.get(&0xB001) else {
        return Ok(());
    };
    let Some(quant) = quant_val.as_i64() else {
        return Ok(());
    };
    if quant <= 0 {
        return Ok(());
    }
    let Some(ExifValue::Bytes(raw_entries)) = ifd0.tags.get(&0xB002) else {
        return Ok(());
    };
    let Ok(quant_u) = usize::try_from(quant) else {
        return Err("struct.error: unpack_from requires a buffer of at least 16 bytes".to_string());
    };
    for entry_idx in 0..quant_u {
        let Some(start) = entry_idx.checked_mul(16) else {
            return Err(
                "struct.error: unpack_from requires a buffer of at least 16 bytes".to_string(),
            );
        };
        let Some(entry) = start
            .checked_add(16)
            .and_then(|end| raw_entries.get(start..end))
        else {
            return Err(
                "struct.error: unpack_from requires a buffer of at least 16 bytes".to_string(),
            );
        };
        let attr = endian.u32([entry[0], entry[1], entry[2], entry[3]]);
        let image_data_format = (attr >> 24) & 7;
        if image_data_format != 0 {
            // `_getmp` raises `SyntaxError("unsupported picture format in MPO")`, caught by `jpeg_factory`.
            return Ok(());
        }
    }
    Ok(())
}

/// Port of `PIL.JpegImagePlugin.JpegImageFile._open` + `ImageFile.__init__` + `jpeg_factory`.
pub fn parse_jpeg(bytes: &[u8]) -> Result<JpegDoc, String> {
    if !bytes.starts_with(b"\xff\xd8\xff") {
        return Err("SyntaxError: not a JPEG file".to_string());
    }
    let mut pos = 2usize;
    let mut s_byte = bytes[pos];
    pos += 1;

    let mut width: Option<u32> = None;
    let mut height: Option<u32> = None;
    let mut has_mode = false;
    let mut exif_bytes: Option<Vec<u8>> = None;
    let mut xmp_bytes: Option<Vec<u8>> = None;
    let mut icclist: Vec<&[u8]> = Vec::new();
    let mut mp_bytes: Option<&[u8]> = None;
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
            // Markers with no payload length (JPG, RST0..RST7, SOI, EOI, JPG0..JPG13)
            0xFFC8 | 0xFFD0..=0xFFD9 | 0xFFF0..=0xFFFD => {}
            // APP0..APP15
            0xFFE0..=0xFFEF => {
                let Some(len_bytes) = bytes.get(pos..).and_then(|s| s.get(..2)) else {
                    break;
                };
                pos += 2;
                let seg_len = usize::from(u16::from_be_bytes([len_bytes[0], len_bytes[1]]));
                let payload: &[u8] = if seg_len < 2 {
                    &[]
                } else {
                    let payload_len = seg_len - 2;
                    let Some(slice) = bytes.get(pos..).and_then(|s| s.get(..payload_len)) else {
                        return Err("OSError: Truncated File Read".to_string());
                    };
                    pos += payload_len;
                    slice
                };

                if (marker == 0xFFE0 && payload.starts_with(b"JFIF"))
                    || (marker == 0xFFEE && payload.starts_with(b"Adobe"))
                {
                    if payload.len() < 7 {
                        return Err("IndexError: index out of range".to_string());
                    }
                } else if marker == 0xFFE1 {
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
                } else if marker == 0xFFE2 {
                    if payload.starts_with(b"ICC_PROFILE\x00") {
                        icclist.push(payload);
                    } else if let Some(rest) = payload.strip_prefix(b"MPF\x00") {
                        mp_bytes = Some(rest);
                    }
                } else if marker == 0xFFED && payload.starts_with(b"Photoshop 3.0\x00") {
                    validate_photoshop_app13(payload)?;
                }
            }
            // SOF markers (including DHP 0xFFDE; excluding DHT 0xFFC4, JPG 0xFFC8, DAC 0xFFCC)
            0xFFC0..=0xFFC3 | 0xFFC5..=0xFFC7 | 0xFFC9..=0xFFCB | 0xFFCD..=0xFFCF | 0xFFDE => {
                let Some(len_bytes) = bytes.get(pos..).and_then(|s| s.get(..2)) else {
                    break;
                };
                pos += 2;
                let seg_len = usize::from(u16::from_be_bytes([len_bytes[0], len_bytes[1]]));
                let payload: &[u8] = if seg_len < 2 {
                    &[]
                } else {
                    let payload_len = seg_len - 2;
                    let Some(slice) = bytes.get(pos..).and_then(|s| s.get(..payload_len)) else {
                        return Err("OSError: Truncated File Read".to_string());
                    };
                    pos += payload_len;
                    slice
                };
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
                if !icclist.is_empty() {
                    icclist.sort_unstable();
                    if icclist[0].len() < 14 {
                        return Err("IndexError: index out of range".to_string());
                    }
                    icclist.clear();
                }
                if !(payload.len() - 6).is_multiple_of(3) {
                    return Err("IndexError: index out of range".to_string());
                }
                width = Some(w);
                height = Some(h);
                has_mode = true;
            }
            // DQT
            0xFFDB => {
                let Some(len_bytes) = bytes.get(pos..).and_then(|s| s.get(..2)) else {
                    break;
                };
                pos += 2;
                let seg_len = usize::from(u16::from_be_bytes([len_bytes[0], len_bytes[1]]));
                let payload: &[u8] = if seg_len < 2 {
                    &[]
                } else {
                    let payload_len = seg_len - 2;
                    let Some(slice) = bytes.get(pos..).and_then(|s| s.get(..payload_len)) else {
                        return Err("OSError: Truncated File Read".to_string());
                    };
                    pos += payload_len;
                    slice
                };
                validate_dqt(payload)?;
            }
            // SOS
            0xFFDA => {
                let Some(len_bytes) = bytes.get(pos..).and_then(|s| s.get(..2)) else {
                    break;
                };
                pos += 2;
                let seg_len = usize::from(u16::from_be_bytes([len_bytes[0], len_bytes[1]]));
                if seg_len >= 2
                    && bytes
                        .get(pos..)
                        .and_then(|s| s.get(..seg_len - 2))
                        .is_none()
                {
                    return Err("OSError: Truncated File Read".to_string());
                }
                saw_sos = true;
                break;
            }
            // Other sized markers (DHT, DAC, DNL, DRI, EXP, COM)
            0xFFC4 | 0xFFCC | 0xFFDC | 0xFFDD | 0xFFDF | 0xFFFE => {
                let Some(len_bytes) = bytes.get(pos..).and_then(|s| s.get(..2)) else {
                    break;
                };
                pos += 2;
                let seg_len = usize::from(u16::from_be_bytes([len_bytes[0], len_bytes[1]]));
                if seg_len >= 2 {
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
    if !has_mode || w == 0 || h == 0 {
        return Err("SyntaxError: not identified by this driver".to_string());
    }
    if let Some(mp) = mp_bytes {
        validate_mpf(mp)?;
    }
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
