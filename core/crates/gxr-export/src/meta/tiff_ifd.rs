//! Low-level TIFF/BigTIFF header, IFD directory loading, and `PIL.TiffTags` open validation.

use std::collections::BTreeMap;

use super::exif_value::{Endian, ExifValue};

pub const EXIF_IFD_TAG: u16 = 0x8769;

#[derive(Clone, Debug)]
pub struct Ifd {
    pub tags: BTreeMap<u16, ExifValue>,
    pub next: u64,
}

pub fn parse_tiff_header(hdr: &[u8], full_bytes: &[u8]) -> Result<(Endian, bool, u64), String> {
    if hdr.len() < 8 {
        return Err("SyntaxError: not a TIFF file".to_string());
    }
    let endian = match &hdr[..2] {
        b"II" => Endian::Little,
        b"MM" => Endian::Big,
        _ => return Err("SyntaxError: not a TIFF file".to_string()),
    };
    let magic = endian.u16([hdr[2], hdr[3]]);
    match magic {
        42 => {
            let off = u64::from(endian.u32([hdr[4], hdr[5], hdr[6], hdr[7]]));
            Ok((endian, false, off))
        }
        43 => {
            let hdr16 = full_bytes
                .get(..16)
                .ok_or_else(|| "SyntaxError: truncated BigTIFF header".to_string())?;
            let off_size = endian.u16([hdr16[4], hdr16[5]]);
            let zero = endian.u16([hdr16[6], hdr16[7]]);
            if off_size != 8 || zero != 0 {
                return Err("SyntaxError: invalid BigTIFF header".to_string());
            }
            let off = endian.u64([
                hdr16[8], hdr16[9], hdr16[10], hdr16[11], hdr16[12], hdr16[13], hdr16[14],
                hdr16[15],
            ]);
            Ok((endian, true, off))
        }
        _ => Err("SyntaxError: not a TIFF file".to_string()),
    }
}

/// Port of `PIL.TiffImagePlugin.ImageFileDirectory_v2.load`.
pub fn load_ifd(
    bytes: &[u8],
    endian: Endian,
    bigtiff: bool,
    offset: u64,
    group: Option<u16>,
    fixup: bool,
) -> Result<Ifd, String> {
    let mut tags = BTreeMap::new();
    let Ok(mut pos) = usize::try_from(offset) else {
        return Ok(Ifd { tags, next: 0 });
    };

    let num_entries: u64 = if bigtiff {
        let Some(b) = bytes.get(pos..pos + 8) else {
            return Ok(Ifd { tags, next: 0 });
        };
        pos += 8;
        endian.u64(b.try_into().unwrap_or([0; 8]))
    } else {
        let Some(b) = bytes.get(pos..pos + 2) else {
            return Ok(Ifd { tags, next: 0 });
        };
        pos += 2;
        u64::from(endian.u16([b[0], b[1]]))
    };

    let entry_size = if bigtiff { 20usize } else { 12usize };
    let inline_limit = if bigtiff { 8u64 } else { 4u64 };

    for _ in 0..num_entries {
        let Some(entry) = bytes.get(pos..pos + entry_size) else {
            return Ok(Ifd { tags, next: 0 });
        };
        pos += entry_size;

        let tag = endian.u16([entry[0], entry[1]]);
        let typ = endian.u16([entry[2], entry[3]]);
        let count: u64 = if bigtiff {
            endian.u64(entry[4..12].try_into().unwrap_or([0; 8]))
        } else {
            u64::from(endian.u32([entry[4], entry[5], entry[6], entry[7]]))
        };
        let inline = if bigtiff {
            &entry[12..20]
        } else {
            &entry[8..12]
        };

        let Some(unit_size) = tiff_unit_size(typ) else {
            continue;
        };
        let Some(size) = count.checked_mul(unit_size) else {
            // Overflow in count * unit_size -> Pillow _safe_read hits EOF / OSError and returns.
            return Ok(Ifd { tags, next: 0 });
        };
        if size == 0 {
            continue;
        }

        let data: &[u8] = if size > inline_limit {
            let val_off: u64 = if bigtiff {
                endian.u64(inline.try_into().unwrap_or([0; 8]))
            } else {
                u64::from(endian.u32([inline[0], inline[1], inline[2], inline[3]]))
            };
            let Some(end) = val_off.checked_add(size) else {
                return Ok(Ifd { tags, next: 0 });
            };
            let (Ok(start_u), Ok(end_u)) = (usize::try_from(val_off), usize::try_from(end)) else {
                return Ok(Ifd { tags, next: 0 });
            };
            let Some(slice) = bytes.get(start_u..end_u) else {
                return Ok(Ifd { tags, next: 0 });
            };
            slice
        } else {
            &inline[..size as usize]
        };

        if let Some(val) = decode_tag_value(tag, typ, data, endian, group, fixup) {
            tags.insert(tag, val);
        }
    }

    let next = if bigtiff {
        bytes
            .get(pos..pos + 8)
            .and_then(|b| b.try_into().ok())
            .map_or(0, |b| endian.u64(b))
    } else {
        bytes
            .get(pos..pos + 4)
            .and_then(|b| b.try_into().ok())
            .map_or(0, |b| u64::from(endian.u32(b)))
    };

    Ok(Ifd { tags, next })
}

fn tiff_unit_size(typ: u16) -> Option<u64> {
    match typ {
        1 | 2 | 6 | 7 => Some(1),
        3 | 8 => Some(2),
        4 | 9 | 11 | 13 => Some(4),
        5 | 10 | 12 | 16 => Some(8),
        _ => None,
    }
}

fn decode_tag_value(
    tag: u16,
    typ: u16,
    data: &[u8],
    endian: Endian,
    group: Option<u16>,
    fixup: bool,
) -> Option<ExifValue> {
    match typ {
        // BYTE (1) / UNDEFINED (7)
        1 | 7 => Some(ExifValue::Bytes(data.to_vec())),
        // ASCII (2): strip at most one trailing NUL and decode as latin-1
        2 => {
            let trimmed = data.strip_suffix(b"\0").unwrap_or(data);
            let s: String = trimmed.iter().map(|&b| char::from(b)).collect();
            Some(ExifValue::Str(s))
        }
        // SHORT (3)
        3 => decode_ints(
            data.chunks_exact(2)
                .map(|c| i64::from(endian.u16([c[0], c[1]])))
                .collect(),
            tag,
            group,
            fixup,
        ),
        // LONG (4) / IFD (13)
        4 | 13 => decode_ints(
            data.chunks_exact(4)
                .map(|c| i64::from(endian.u32([c[0], c[1], c[2], c[3]])))
                .collect(),
            tag,
            group,
            fixup,
        ),
        // SBYTE (6)
        6 => decode_ints(
            data.iter().map(|&b| i64::from(b as i8)).collect(),
            tag,
            group,
            fixup,
        ),
        // SSHORT (8)
        8 => decode_ints(
            data.chunks_exact(2)
                .map(|c| i64::from(endian.i16([c[0], c[1]])))
                .collect(),
            tag,
            group,
            fixup,
        ),
        // SLONG (9)
        9 => decode_ints(
            data.chunks_exact(4)
                .map(|c| i64::from(endian.i32([c[0], c[1], c[2], c[3]])))
                .collect(),
            tag,
            group,
            fixup,
        ),
        // LONG8 (16)
        16 => decode_ints(
            data.chunks_exact(8)
                .map(|c| {
                    let v = endian.u64(c.try_into().unwrap_or([0; 8]));
                    i64::try_from(v).unwrap_or(i64::MAX)
                })
                .collect(),
            tag,
            group,
            fixup,
        ),
        // RATIONAL (5)
        5 => decode_rationals(
            data.chunks_exact(8)
                .map(|c| {
                    let n = i64::from(endian.u32([c[0], c[1], c[2], c[3]]));
                    let d = i64::from(endian.u32([c[4], c[5], c[6], c[7]]));
                    (n, d)
                })
                .collect(),
            tag,
            group,
            fixup,
        ),
        // SRATIONAL (10)
        10 => decode_rationals(
            data.chunks_exact(8)
                .map(|c| {
                    let n = i64::from(endian.i32([c[0], c[1], c[2], c[3]]));
                    let d = i64::from(endian.i32([c[4], c[5], c[6], c[7]]));
                    (n, d)
                })
                .collect(),
            tag,
            group,
            fixup,
        ),
        // FLOAT (11)
        11 => decode_floats(
            data.chunks_exact(4)
                .map(|c| f64::from(endian.f32([c[0], c[1], c[2], c[3]])))
                .collect(),
            tag,
            group,
            fixup,
        ),
        // DOUBLE (12)
        12 => decode_floats(
            data.chunks_exact(8)
                .map(|c| endian.f64(c.try_into().unwrap_or([0; 8])))
                .collect(),
            tag,
            group,
            fixup,
        ),
        _ => None,
    }
}

fn should_unwrap_scalar(len: usize, tag: u16, group: Option<u16>, fixup: bool) -> bool {
    if len == 0 {
        return false;
    }
    let spec_len = tiff_tag_length(tag, group);
    if spec_len == Some(1) || (spec_len.is_none() && len == 1) {
        return true;
    }
    fixup && len == 1
}

fn decode_ints(vals: Vec<i64>, tag: u16, group: Option<u16>, fixup: bool) -> Option<ExifValue> {
    if vals.is_empty() {
        return None;
    }
    if should_unwrap_scalar(vals.len(), tag, group, fixup) {
        Some(ExifValue::Int(vals[0]))
    } else {
        Some(ExifValue::Ints(vals))
    }
}

fn decode_rationals(
    vals: Vec<(i64, i64)>,
    tag: u16,
    group: Option<u16>,
    fixup: bool,
) -> Option<ExifValue> {
    if vals.is_empty() {
        return None;
    }
    if should_unwrap_scalar(vals.len(), tag, group, fixup) {
        Some(ExifValue::Rational(vals[0].0, vals[0].1))
    } else {
        Some(ExifValue::Rationals(vals))
    }
}

fn decode_floats(vals: Vec<f64>, tag: u16, group: Option<u16>, fixup: bool) -> Option<ExifValue> {
    if vals.is_empty() {
        return None;
    }
    if should_unwrap_scalar(vals.len(), tag, group, fixup) {
        Some(ExifValue::Float(vals[0]))
    } else {
        Some(ExifValue::Floats(vals))
    }
}

/// Port of `PIL.TiffTags.lookup(tag, group).length`.
fn tiff_tag_length(tag: u16, group: Option<u16>) -> Option<usize> {
    if group == Some(EXIF_IFD_TAG) {
        return match tag {
            36864 | 40960 | 40965 | 41730 => Some(1),
            _ => None,
        };
    }
    if group.is_some() {
        return None;
    }
    match tag {
        258 | 273 | 279 | 280 | 281 | 291 | 301 | 320 | 324 | 325 | 330 | 336 | 338 | 339 | 340
        | 341 | 517 | 518 | 519 | 520 | 521 | 700 | 32996 | 34377 | 45059 | 50838 => Some(0),
        297 | 318 | 321 | 530 => Some(2),
        529 => Some(3),
        319 | 342 | 532 => Some(6),
        254 | 255 | 256 | 257 | 259 | 262 | 263 | 264 | 265 | 266 | 269 | 270 | 271 | 272 | 274
        | 277 | 278 | 282 | 283 | 284 | 285 | 286 | 287 | 288 | 289 | 290 | 292 | 293 | 296
        | 305 | 306 | 315 | 316 | 317 | 322 | 323 | 326 | 327 | 332 | 333 | 334 | 347 | 512
        | 513 | 514 | 515 | 531 | 33432 | 34665 | 34675 | 34853 | 37724 | 40965 => Some(1),
        _ => None,
    }
}

pub fn is_known_compression(c: i64) -> bool {
    matches!(
        c,
        1 | 2
            | 3
            | 4
            | 5
            | 6
            | 7
            | 8
            | 32771
            | 32773
            | 32809
            | 32946
            | 34676
            | 34677
            | 34925
            | 50000
            | 50001
    )
}

pub fn is_known_open_info(
    endian: &[u8; 2],
    photo: i64,
    sample_format: &[i64],
    fillorder: i64,
    bps: &[i64],
    extra: &[i64],
) -> bool {
    matches!(
        (endian, photo, sample_format, fillorder, bps, extra),
        (_, 0, [1], 1, [1 | 2 | 4 | 8], [])
            | (b"II", 0, [1], 1, [16], [])
            | (_, 0, [1], 2, [1 | 2 | 4 | 8], [])
            | (_, 0, [3], 1, [32], [])
            | (_, 1, [1], 1, [1 | 2 | 4 | 8 | 16], [])
            | (b"II", 1, [1], 1, [12 | 32], [])
            | (_, 1, [1], 1, [8, 8], [2])
            | (_, 1, [1], 2, [1 | 2 | 4 | 8 | 16], [])
            | (_, 1, [2], 1, [8 | 16 | 32], [])
            | (_, 1, [3], 1, [32], [])
            | (_, 2, [1], 1, [8, 8, 8], [])
            | (_, 2, [1], 1, [8, 8, 8, 8], [])
            | (_, 2, [1], 1, [8, 8, 8, 8], [0 | 1 | 2 | 999])
            | (_, 2, [1], 1, [8, 8, 8, 8, 8], [0..=2, 0])
            | (_, 2, [1], 1, [8, 8, 8, 8, 8, 8], [0..=2, 0, 0])
            | (_, 2, [1], 1, [16, 16, 16], [])
            | (_, 2, [1], 1, [16, 16, 16, 16], [])
            | (_, 2, [1], 1, [16, 16, 16, 16], [0..=2])
            | (_, 2, [1], 2, [8, 8, 8], [])
            | (_, 3, [1], 1, [1 | 2 | 4 | 8], [])
            | (_, 3, [1], 1, [8, 8], [0 | 2])
            | (_, 3, [1], 2, [1 | 2 | 4 | 8], [])
            | (_, 5, [1], 1, [8, 8, 8, 8], [])
            | (_, 5, [1], 1, [8, 8, 8, 8, 8], [0])
            | (_, 5, [1], 1, [8, 8, 8, 8, 8, 8], [0, 0])
            | (_, 5, [1], 1, [16, 16, 16, 16], [])
            | (_, 6, [1], 1, [8], [])
            | (_, 6, [1], 1, [8, 8, 8], [])
            | (_, 8, [1], 1, [8, 8, 8], [])
    )
}
