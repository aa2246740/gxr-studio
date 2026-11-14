//! Big-endian TIFF IFD serializer for EXIF APP1 payloads.

use std::collections::BTreeMap;

use gxr_raw::CoreError;

use crate::exif::ExifVal;

pub(crate) const TYPE_BYTE: u16 = 1;
pub(crate) const TYPE_ASCII: u16 = 2;
pub(crate) const TYPE_SHORT: u16 = 3;
pub(crate) const TYPE_LONG: u16 = 4;
pub(crate) const TYPE_RATIONAL: u16 = 5;
pub(crate) const TYPE_UNDEFINED: u16 = 7;
pub(crate) const TYPE_SSHORT: u16 = 8;
pub(crate) const TYPE_SLONG: u16 = 9;
pub(crate) const TYPE_SRATIONAL: u16 = 10;
pub(crate) const TYPE_DOUBLE: u16 = 12;

struct SerializedEntry {
    tag: u16,
    typ: u16,
    count: u32,
    inline_or_offset: [u8; 4],
    aux: Vec<u8>,
}

fn encode_ascii_replace(s: &str) -> Vec<u8> {
    s.chars()
        .map(|ch| {
            u8::try_from(u32::from(ch))
                .ok()
                .filter(|&b| b < 0x80)
                .unwrap_or(b'?')
        })
        .collect()
}

fn lookup_base_type(tag: u16) -> Option<u16> {
    match tag {
        269 | 270 | 271 | 272 | 285 | 305 | 306 | 315 | 316 | 333 | 337 | 33432 => Some(TYPE_ASCII),
        255
        | 259
        | 262..=266
        | 274
        | 277
        | 280
        | 281
        | 284
        | 290
        | 291
        | 296
        | 297
        | 301
        | 317
        | 320
        | 321
        | 332
        | 334
        | 336
        | 338
        | 339
        | 342
        | 512
        | 515
        | 517
        | 518
        | 530
        | 531
        | 32995
        | 50741 => Some(TYPE_SHORT),
        254
        | 256
        | 257
        | 273
        | 278
        | 279
        | 288
        | 289
        | 292
        | 293
        | 322..=325
        | 330
        | 513
        | 514
        | 519..=521
        | 34665
        | 34853
        | 40965 => Some(TYPE_LONG),
        282 | 283 | 286 | 287 | 318 | 319 | 529 | 532 | 50780 => Some(TYPE_RATIONAL),
        700 | 34377 => Some(TYPE_BYTE),
        347 | 33723 | 34675 | 36864 | 37724 | 40960 | 41730 => Some(TYPE_UNDEFINED),
        _ => None,
    }
}

fn lookup_exif_type(tag: u16) -> Option<u16> {
    match tag {
        36864 | 40960 | 41730 => Some(TYPE_UNDEFINED),
        40965 => Some(TYPE_LONG),
        _ => None,
    }
}

fn infer_type(val: &ExifVal) -> u16 {
    match val {
        ExifVal::Str(_) => TYPE_ASCII,
        ExifVal::Bytes(_) => TYPE_BYTE,
        ExifVal::Floats(_) => TYPE_DOUBLE,
        ExifVal::Rationals(rs) => {
            if rs.iter().any(|&(n, d)| (n < 0) ^ (d < 0)) {
                TYPE_SRATIONAL
            } else {
                TYPE_RATIONAL
            }
        }
        ExifVal::Ints(vs) => {
            if vs.iter().all(|&v| (0..=0xFFFF).contains(&v)) {
                TYPE_SHORT
            } else if vs.iter().all(|&v| v > -32768 && v < 32768) {
                TYPE_SSHORT
            } else if vs.iter().all(|&v| v >= 0 && v <= i64::from(u32::MAX)) {
                TYPE_LONG
            } else {
                TYPE_SLONG
            }
        }
    }
}

fn encode_tag_data(typ: u16, val: &ExifVal) -> Result<(u32, Vec<u8>), CoreError> {
    match typ {
        TYPE_ASCII => {
            let mut data = match val {
                ExifVal::Bytes(b) => b.clone(),
                ExifVal::Str(s) => encode_ascii_replace(s),
                ExifVal::Ints(vs) if vs.len() == 1 => vs[0].to_string().into_bytes(),
                _ => {
                    return Err(CoreError::new(
                        "encode_failed",
                        "invalid ASCII EXIF tag value",
                    ));
                }
            };
            data.push(0);
            let count = u32::try_from(data.len())
                .map_err(|_| CoreError::new("encode_failed", "EXIF ASCII tag too long"))?;
            Ok((count, data))
        }
        TYPE_BYTE => {
            let data = match val {
                ExifVal::Bytes(b) => b.clone(),
                ExifVal::Ints(vs) => {
                    let mut out = Vec::with_capacity(vs.len());
                    for &v in vs {
                        let b = u8::try_from(v).map_err(|_| {
                            CoreError::new("encode_failed", "BYTE tag out of range")
                        })?;
                        out.push(b);
                    }
                    out
                }
                _ => {
                    return Err(CoreError::new(
                        "encode_failed",
                        "invalid BYTE EXIF tag value",
                    ));
                }
            };
            let count = u32::try_from(data.len())
                .map_err(|_| CoreError::new("encode_failed", "EXIF BYTE tag too long"))?;
            Ok((count, data))
        }
        TYPE_UNDEFINED => {
            let data = match val {
                ExifVal::Bytes(b) => b.clone(),
                ExifVal::Str(s) => encode_ascii_replace(s),
                ExifVal::Ints(vs) if vs.len() == 1 => vs[0].to_string().into_bytes(),
                _ => {
                    return Err(CoreError::new(
                        "encode_failed",
                        "invalid UNDEFINED EXIF tag value",
                    ));
                }
            };
            let count = u32::try_from(data.len())
                .map_err(|_| CoreError::new("encode_failed", "EXIF UNDEFINED tag too long"))?;
            Ok((count, data))
        }
        TYPE_SHORT => {
            encode_ints_be::<u16, 2>(val, "SHORT", |v| u16::try_from(v).ok(), |u| u.to_be_bytes())
        }
        TYPE_SSHORT => encode_ints_be::<i16, 2>(
            val,
            "SSHORT",
            |v| i16::try_from(v).ok(),
            |u| u.to_be_bytes(),
        ),
        TYPE_LONG => {
            encode_ints_be::<u32, 4>(val, "LONG", |v| u32::try_from(v).ok(), |u| u.to_be_bytes())
        }
        TYPE_SLONG => {
            encode_ints_be::<i32, 4>(val, "SLONG", |v| i32::try_from(v).ok(), |u| u.to_be_bytes())
        }
        TYPE_RATIONAL => encode_rationals_unsigned(val),
        TYPE_SRATIONAL => encode_rationals_signed(val),
        TYPE_DOUBLE => {
            let fs: Vec<f64> = match val {
                ExifVal::Floats(fs) => fs.clone(),
                #[allow(clippy::cast_precision_loss)]
                ExifVal::Ints(vs) => vs.iter().map(|&v| v as f64).collect(),
                _ => {
                    return Err(CoreError::new(
                        "encode_failed",
                        "invalid DOUBLE EXIF tag value",
                    ));
                }
            };
            let mut data = Vec::with_capacity(fs.len() * 8);
            for f in &fs {
                data.extend_from_slice(&f.to_be_bytes());
            }
            let count = u32::try_from(fs.len())
                .map_err(|_| CoreError::new("encode_failed", "EXIF DOUBLE tag too long"))?;
            Ok((count, data))
        }
        _ => Err(CoreError::new("encode_failed", "unsupported TIFF tag type")),
    }
}

fn encode_ints_be<T, const N: usize>(
    val: &ExifVal,
    label: &str,
    conv: fn(i64) -> Option<T>,
    bytes: fn(T) -> [u8; N],
) -> Result<(u32, Vec<u8>), CoreError> {
    let ExifVal::Ints(vs) = val else {
        return Err(CoreError::new(
            "encode_failed",
            format!("invalid {label} EXIF tag value"),
        ));
    };
    let mut data = Vec::with_capacity(vs.len() * N);
    for &v in vs {
        let converted = conv(v)
            .ok_or_else(|| CoreError::new("encode_failed", format!("{label} tag out of range")))?;
        data.extend_from_slice(&bytes(converted));
    }
    let count = u32::try_from(vs.len())
        .map_err(|_| CoreError::new("encode_failed", format!("EXIF {label} tag too long")))?;
    Ok((count, data))
}

fn encode_rationals_unsigned(val: &ExifVal) -> Result<(u32, Vec<u8>), CoreError> {
    let rs: Vec<(i64, i64)> = match val {
        ExifVal::Rationals(rs) => rs.clone(),
        ExifVal::Ints(vs) => vs.iter().map(|&v| (v, 1)).collect(),
        _ => {
            return Err(CoreError::new(
                "encode_failed",
                "invalid RATIONAL EXIF tag value",
            ));
        }
    };
    let mut data = Vec::with_capacity(rs.len() * 8);
    for &(n, d) in &rs {
        let (nn, dd) = if n < 0 && d < 0 {
            (n.wrapping_neg(), d.wrapping_neg())
        } else {
            (n, d)
        };
        let nu = u32::try_from(nn)
            .map_err(|_| CoreError::new("encode_failed", "RATIONAL numerator out of range"))?;
        let du = u32::try_from(dd)
            .map_err(|_| CoreError::new("encode_failed", "RATIONAL denominator out of range"))?;
        data.extend_from_slice(&nu.to_be_bytes());
        data.extend_from_slice(&du.to_be_bytes());
    }
    let count = u32::try_from(rs.len())
        .map_err(|_| CoreError::new("encode_failed", "EXIF RATIONAL tag too long"))?;
    Ok((count, data))
}

fn encode_rationals_signed(val: &ExifVal) -> Result<(u32, Vec<u8>), CoreError> {
    let rs: Vec<(i64, i64)> = match val {
        ExifVal::Rationals(rs) => rs.clone(),
        ExifVal::Ints(vs) => vs.iter().map(|&v| (v, 1)).collect(),
        _ => {
            return Err(CoreError::new(
                "encode_failed",
                "invalid SRATIONAL EXIF tag value",
            ));
        }
    };
    let mut data = Vec::with_capacity(rs.len() * 8);
    for &(n, d) in &rs {
        let (nn, dd) = if d < 0 {
            (n.wrapping_neg(), d.wrapping_neg())
        } else {
            (n, d)
        };
        let ni = i32::try_from(nn)
            .map_err(|_| CoreError::new("encode_failed", "SRATIONAL numerator out of range"))?;
        let di = i32::try_from(dd)
            .map_err(|_| CoreError::new("encode_failed", "SRATIONAL denominator out of range"))?;
        data.extend_from_slice(&ni.to_be_bytes());
        data.extend_from_slice(&di.to_be_bytes());
    }
    let count = u32::try_from(rs.len())
        .map_err(|_| CoreError::new("encode_failed", "EXIF SRATIONAL tag too long"))?;
    Ok((count, data))
}

fn serialize_ifd(
    tags: &BTreeMap<u16, ExifVal>,
    start_offset: u32,
    lookup: fn(u16) -> Option<u16>,
) -> Result<Vec<u8>, CoreError> {
    let n_entries = u16::try_from(tags.len())
        .map_err(|_| CoreError::new("encode_failed", "too many EXIF tags"))?;
    let mut offset = start_offset
        .checked_add(2)
        .and_then(|v| v.checked_add(u32::from(n_entries) * 12))
        .and_then(|v| v.checked_add(4))
        .ok_or_else(|| CoreError::new("encode_failed", "EXIF offset overflow"))?;

    let mut entries = Vec::with_capacity(tags.len());
    for (&tag, val) in tags {
        let typ = lookup(tag).unwrap_or_else(|| infer_type(val));
        let (count, data) = encode_tag_data(typ, val)?;
        entries.push(pack_entry(tag, typ, count, data, &mut offset)?);
    }

    Ok(assemble_ifd(n_entries, &entries))
}

pub(crate) fn serialize_ifd0_with_exif(
    base: &BTreeMap<u16, ExifVal>,
    sub: &BTreeMap<u16, ExifVal>,
) -> Result<Vec<u8>, CoreError> {
    let n_entries = u16::try_from(base.len() + 1)
        .map_err(|_| CoreError::new("encode_failed", "too many EXIF IFD0 tags"))?;
    let mut offset: u32 = 8u32 + 2 + u32::from(n_entries) * 12 + 4;

    let mut entries = Vec::with_capacity(base.len() + 1);
    let mut sub_written = false;

    for (&tag, val) in base {
        if !sub_written && tag > 0x8769 {
            let sub_bytes = serialize_ifd(sub, offset, lookup_exif_type)?;
            entries.push(pack_ifd_pointer(0x8769, sub_bytes, &mut offset)?);
            sub_written = true;
        }
        let typ = lookup_base_type(tag).unwrap_or_else(|| infer_type(val));
        let (count, data) = encode_tag_data(typ, val)?;
        entries.push(pack_entry(tag, typ, count, data, &mut offset)?);
    }
    if !sub_written {
        let sub_bytes = serialize_ifd(sub, offset, lookup_exif_type)?;
        entries.push(pack_ifd_pointer(0x8769, sub_bytes, &mut offset)?);
    }

    Ok(assemble_ifd(n_entries, &entries))
}

fn pack_entry(
    tag: u16,
    typ: u16,
    count: u32,
    data: Vec<u8>,
    offset: &mut u32,
) -> Result<SerializedEntry, CoreError> {
    if data.len() <= 4 {
        let mut inline = [0u8; 4];
        inline[..data.len()].copy_from_slice(&data);
        Ok(SerializedEntry {
            tag,
            typ,
            count,
            inline_or_offset: inline,
            aux: Vec::new(),
        })
    } else {
        let inline_or_offset = offset.to_be_bytes();
        let padded_len = u32::try_from(data.len().div_ceil(2) * 2)
            .map_err(|_| CoreError::new("encode_failed", "EXIF data overflow"))?;
        *offset = offset
            .checked_add(padded_len)
            .ok_or_else(|| CoreError::new("encode_failed", "EXIF offset overflow"))?;
        Ok(SerializedEntry {
            tag,
            typ,
            count,
            inline_or_offset,
            aux: data,
        })
    }
}

fn pack_ifd_pointer(
    tag: u16,
    sub_bytes: Vec<u8>,
    offset: &mut u32,
) -> Result<SerializedEntry, CoreError> {
    let inline_or_offset = offset.to_be_bytes();
    let padded_len = u32::try_from(sub_bytes.len().div_ceil(2) * 2)
        .map_err(|_| CoreError::new("encode_failed", "EXIF sub-IFD overflow"))?;
    *offset = offset
        .checked_add(padded_len)
        .ok_or_else(|| CoreError::new("encode_failed", "EXIF offset overflow"))?;
    Ok(SerializedEntry {
        tag,
        typ: TYPE_LONG,
        count: 1,
        inline_or_offset,
        aux: sub_bytes,
    })
}

fn assemble_ifd(n_entries: u16, entries: &[SerializedEntry]) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(&n_entries.to_be_bytes());
    for e in entries {
        out.extend_from_slice(&e.tag.to_be_bytes());
        out.extend_from_slice(&e.typ.to_be_bytes());
        out.extend_from_slice(&e.count.to_be_bytes());
        out.extend_from_slice(&e.inline_or_offset);
    }
    out.extend_from_slice(&0u32.to_be_bytes());
    for e in entries {
        if !e.aux.is_empty() {
            out.extend_from_slice(&e.aux);
            if e.aux.len() & 1 == 1 {
                out.push(0);
            }
        }
    }
    out
}
