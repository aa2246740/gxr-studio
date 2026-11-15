//! Port of `gxr_studio/hdr/capture.py` (`from_dng`, `from_jpeg`) and `hdr/gain.py` (`check_capture`).

use std::collections::BTreeMap;

use gxr_hdr::{Field, MISMATCH, TagValue, capture_of, differs};
use gxr_raw::CoreError;
use gxr_raw::pyrepr::py_float_repr;

const TEXT_TAGS: &[u16] = &[271, 272, 50708, 36867, 42036, 42016, 37521];

fn raw_meta_err() -> CoreError {
    CoreError::new("metadata_unreadable", "无法核对 RAW 拍摄信息")
}

fn jpg_meta_err() -> CoreError {
    CoreError::new("metadata_unreadable", "无法核对 JPG 拍摄信息")
}

#[derive(Clone, Debug)]
enum TiffVal {
    Ascii(String),
    Bytes(Vec<u8>),
    Ints(Vec<i64>),
    Floats(Vec<f64>),
}

fn type_info(typ: u16) -> Option<(Option<char>, usize)> {
    Some(match typ {
        1 => (Some('B'), 1),
        2 | 7 => (None, 1),
        3 => (Some('H'), 2),
        4 => (Some('I'), 4),
        5 => (Some('R'), 8),
        6 => (Some('b'), 1),
        8 => (Some('h'), 2),
        9 => (Some('i'), 4),
        10 => (Some('r'), 8),
        11 => (Some('f'), 4),
        12 => (Some('d'), 8),
        _ => return None,
    })
}

fn u16_at(data: &[u8], at: usize, big: bool) -> u16 {
    let b = [data[at], data[at + 1]];
    if big {
        u16::from_be_bytes(b)
    } else {
        u16::from_le_bytes(b)
    }
}

fn u32_at(data: &[u8], at: usize, big: bool) -> u32 {
    let b = [data[at], data[at + 1], data[at + 2], data[at + 3]];
    if big {
        u32::from_be_bytes(b)
    } else {
        u32::from_le_bytes(b)
    }
}

fn u64_at(data: &[u8], at: usize, big: bool) -> u64 {
    let mut b = [0u8; 8];
    b.copy_from_slice(&data[at..at + 8]);
    if big {
        u64::from_be_bytes(b)
    } else {
        u64::from_le_bytes(b)
    }
}

fn ascii_value(raw: &[u8]) -> String {
    let mut end = raw.len();
    while end > 0 && (raw[end - 1] == 0 || raw[end - 1] == b' ') {
        end -= 1;
    }
    raw[..end]
        .iter()
        .map(|&b| if b < 0x80 { char::from(b) } else { '\u{fffd}' })
        .collect()
}

fn bytes_ascii_replace(raw: &[u8]) -> String {
    raw.iter()
        .map(|&b| if b < 0x80 { char::from(b) } else { '\u{fffd}' })
        .collect()
}

fn decode_values(raw: &[u8], fmt: char, n: usize, big: bool) -> TiffVal {
    match fmt {
        'B' => TiffVal::Ints(raw.iter().map(|&b| i64::from(b)).collect()),
        #[allow(clippy::cast_possible_wrap)]
        'b' => TiffVal::Ints(raw.iter().map(|&b| i64::from(b as i8)).collect()),
        'H' => TiffVal::Ints((0..n).map(|i| i64::from(u16_at(raw, 2 * i, big))).collect()),
        #[allow(clippy::cast_possible_wrap)]
        'h' => TiffVal::Ints(
            (0..n)
                .map(|i| i64::from(u16_at(raw, 2 * i, big) as i16))
                .collect(),
        ),
        'I' => TiffVal::Ints((0..n).map(|i| i64::from(u32_at(raw, 4 * i, big))).collect()),
        #[allow(clippy::cast_possible_wrap)]
        'i' => TiffVal::Ints(
            (0..n)
                .map(|i| i64::from(u32_at(raw, 4 * i, big) as i32))
                .collect(),
        ),
        'f' => TiffVal::Floats(
            (0..n)
                .map(|i| f64::from(f32::from_bits(u32_at(raw, 4 * i, big))))
                .collect(),
        ),
        'd' => TiffVal::Floats(
            (0..n)
                .map(|i| f64::from_bits(u64_at(raw, 8 * i, big)))
                .collect(),
        ),
        _ => TiffVal::Floats(
            (0..n)
                .map(|i| {
                    let (num, den) = if fmt == 'R' {
                        (
                            f64::from(u32_at(raw, 8 * i, big)),
                            f64::from(u32_at(raw, 8 * i + 4, big)),
                        )
                    } else {
                        #[allow(clippy::cast_possible_wrap)]
                        (
                            f64::from(u32_at(raw, 8 * i, big) as i32),
                            f64::from(u32_at(raw, 8 * i + 4, big) as i32),
                        )
                    };
                    if den == 0.0 { f64::NAN } else { num / den }
                })
                .collect(),
        ),
    }
}

struct TiffReader<'a> {
    data: &'a [u8],
    big_endian: bool,
    root: u32,
}

impl<'a> TiffReader<'a> {
    fn new(data: &'a [u8]) -> Result<Self, ()> {
        if data.len() < 8 || !(data[..2] == *b"II" || data[..2] == *b"MM") {
            return Err(());
        }
        let big_endian = data[..2] == *b"MM";
        if u16_at(data, 2, big_endian) != 42 {
            return Err(());
        }
        let root = u32_at(data, 4, big_endian);
        Ok(Self {
            data,
            big_endian,
            root,
        })
    }

    fn ifd(&self, offset: i64) -> Result<BTreeMap<u16, TiffVal>, ()> {
        let len_i64 = i64::try_from(self.data.len()).map_err(|_| ())?;
        if offset < 0 || offset > len_i64 - 2 {
            return Err(());
        }
        let off = usize::try_from(offset).map_err(|_| ())?;
        let count = usize::from(u16_at(self.data, off, self.big_endian));
        let ifd_end = count
            .checked_mul(12)
            .and_then(|n| n.checked_add(6))
            .and_then(|n| off.checked_add(n))
            .ok_or(())?;
        if ifd_end > self.data.len() {
            return Err(());
        }
        let mut out = BTreeMap::new();
        for i in 0..count {
            let p = off + 2 + i * 12;
            let tag = u16_at(self.data, p, self.big_endian);
            let typ = u16_at(self.data, p + 2, self.big_endian);
            let n = u32_at(self.data, p + 4, self.big_endian);
            let Some((fmt, size)) = type_info(typ) else {
                continue;
            };
            let length_u64 = u64::from(n)
                .checked_mul(u64::try_from(size).map_err(|_| ())?)
                .ok_or(())?;
            let inline = u64::try_from(p)
                .ok()
                .and_then(|v| v.checked_add(8))
                .ok_or(())?;
            let val_off = if length_u64 <= 4 {
                inline
            } else {
                u64::from(u32_at(self.data, p + 8, self.big_endian))
            };
            let val_end = val_off.checked_add(length_u64).ok_or(())?;
            let data_len = u64::try_from(self.data.len()).map_err(|_| ())?;
            if val_end > data_len {
                return Err(());
            }
            let vo = usize::try_from(val_off).map_err(|_| ())?;
            let vl = usize::try_from(length_u64).map_err(|_| ())?;
            let raw = &self.data[vo..vo + vl];
            let n_items = usize::try_from(n).map_err(|_| ())?;
            let val = match (typ, fmt) {
                (2, _) => TiffVal::Ascii(ascii_value(raw)),
                (_, None) => TiffVal::Bytes(raw.to_vec()),
                (_, Some(f)) => decode_values(raw, f, n_items, self.big_endian),
            };
            out.insert(tag, val);
        }
        Ok(out)
    }
}

fn tiff_val_to_tag_value(tag: u16, val: &TiffVal) -> TagValue {
    match val {
        TiffVal::Ascii(s) => TagValue::Text(s.clone()),
        TiffVal::Bytes(b) => TagValue::Text(bytes_ascii_replace(b)),
        TiffVal::Ints(v) => {
            if TEXT_TAGS.contains(&tag) {
                let items: Vec<String> = v.iter().map(ToString::to_string).collect();
                TagValue::Text(format!("[{}]", items.join(", ")))
            } else {
                TagValue::List(v.iter().map(|&n| TagValue::Int(n)).collect())
            }
        }
        TiffVal::Floats(v) => {
            if TEXT_TAGS.contains(&tag) {
                let items: Vec<String> = v.iter().map(|&f| py_float_repr(f)).collect();
                TagValue::Text(format!("[{}]", items.join(", ")))
            } else {
                TagValue::List(v.iter().map(|&f| TagValue::Float(f)).collect())
            }
        }
    }
}

fn capture_from_tiff(tiff: &TiffReader<'_>) -> Result<Vec<(String, Field)>, ()> {
    let root = tiff.ifd(i64::from(tiff.root))?;
    let mut merged = root.clone();
    if let Some(exif_ptr) = root.get(&34665) {
        let off = match exif_ptr {
            TiffVal::Ints(v) if !v.is_empty() => v[0],
            _ => return Err(()),
        };
        let exif_ifd = tiff.ifd(off)?;
        for (tag, val) in exif_ifd {
            merged.insert(tag, val);
        }
    }
    let tags: Vec<(u32, TagValue)> = merged
        .iter()
        .map(|(&tag, val)| (u32::from(tag), tiff_val_to_tag_value(tag, val)))
        .collect();
    capture_of(&tags).ok_or(())
}

use crate::meta::tiff_exif::{ExifValue, load_image_exif, open_image};

/// Port of `gxr_studio/hdr/capture.py:56 from_dng`.
pub fn from_dng(data: &[u8]) -> Result<Vec<(String, Field)>, CoreError> {
    let tiff = TiffReader::new(data).map_err(|()| raw_meta_err())?;
    capture_from_tiff(&tiff).map_err(|()| raw_meta_err())
}

#[allow(clippy::cast_precision_loss)]
fn exif_val_to_tag_value(tag: u16, val: &ExifValue) -> TagValue {
    match val {
        ExifValue::Str(s) => TagValue::Text(s.clone()),
        ExifValue::Bytes(b) => TagValue::Text(bytes_ascii_replace(b)),
        ExifValue::Int(n) => {
            if TEXT_TAGS.contains(&tag) {
                TagValue::Text(n.to_string())
            } else {
                TagValue::Int(*n)
            }
        }
        ExifValue::Ints(v) => {
            if TEXT_TAGS.contains(&tag) {
                TagValue::Text(val.to_python_str())
            } else {
                TagValue::List(v.iter().map(|&n| TagValue::Int(n)).collect())
            }
        }
        ExifValue::Float(f) => {
            if TEXT_TAGS.contains(&tag) {
                TagValue::Text(val.to_python_str())
            } else {
                TagValue::Float(*f)
            }
        }
        ExifValue::Floats(v) => {
            if TEXT_TAGS.contains(&tag) {
                TagValue::Text(val.to_python_str())
            } else {
                TagValue::List(v.iter().map(|&f| TagValue::Float(f)).collect())
            }
        }
        ExifValue::Rational(n, d) => {
            if TEXT_TAGS.contains(&tag) {
                TagValue::Text(val.to_python_str())
            } else {
                let f = if *d == 0 {
                    f64::NAN
                } else {
                    (*n as f64) / (*d as f64)
                };
                TagValue::Float(f)
            }
        }
        ExifValue::Rationals(v) => {
            if TEXT_TAGS.contains(&tag) {
                TagValue::Text(val.to_python_str())
            } else {
                TagValue::List(
                    v.iter()
                        .map(|&(n, d)| {
                            TagValue::Float(if d == 0 {
                                f64::NAN
                            } else {
                                (n as f64) / (d as f64)
                            })
                        })
                        .collect(),
                )
            }
        }
    }
}

/// Port of `gxr_studio/hdr/capture.py:65 from_jpeg` via shared `gxr-export::meta` reader.
pub fn from_jpeg(data: &[u8]) -> Result<Vec<(String, Field)>, CoreError> {
    let doc = open_image(data).map_err(|_| jpg_meta_err())?;
    let exif = load_image_exif(data, &doc);
    let mut tags = Vec::with_capacity(exif.base.len() + exif.sub.len());
    for (&tag, val) in &exif.base {
        tags.push((u32::from(tag), exif_val_to_tag_value(tag, val)));
    }
    for (&tag, val) in &exif.sub {
        tags.push((u32::from(tag), exif_val_to_tag_value(tag, val)));
    }
    capture_of(&tags).ok_or_else(jpg_meta_err)
}

/// Port of `gxr_studio/hdr/gain.py:119 check_capture`.
pub fn check_capture(jpeg: &[u8], raw_capture: &[(String, Field)]) -> Result<(), CoreError> {
    let jpeg_capture = from_jpeg(jpeg)?;
    if differs(raw_capture, &jpeg_capture) {
        return Err(CoreError::new("capture_mismatch", MISMATCH));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_tiff(make: &str, iso: u16) -> Vec<u8> {
        let mut d = b"II\x2a\x00\x08\x00\x00\x00".to_vec();
        // 2 entries in root IFD: 271 (Make), 34665 (ExifIFD)
        d.extend_from_slice(&2u16.to_le_bytes());
        let make_bytes = format!("{make}\0");
        let make_off = 8 + 2 + 2 * 12 + 4;
        // tag 271 ASCII
        d.extend_from_slice(&271u16.to_le_bytes());
        d.extend_from_slice(&2u16.to_le_bytes());
        d.extend_from_slice(&(make_bytes.len() as u32).to_le_bytes());
        if make_bytes.len() <= 4 {
            let mut pad = [0u8; 4];
            pad[..make_bytes.len()].copy_from_slice(make_bytes.as_bytes());
            d.extend_from_slice(&pad);
        } else {
            d.extend_from_slice(&(make_off as u32).to_le_bytes());
        }
        let exif_off = if make_bytes.len() <= 4 {
            make_off
        } else {
            make_off + make_bytes.len()
        };
        // tag 34665 LONG 1
        d.extend_from_slice(&34665u16.to_le_bytes());
        d.extend_from_slice(&4u16.to_le_bytes());
        d.extend_from_slice(&1u32.to_le_bytes());
        d.extend_from_slice(&(exif_off as u32).to_le_bytes());
        // next IFD = 0
        d.extend_from_slice(&0u32.to_le_bytes());
        if make_bytes.len() > 4 {
            d.extend_from_slice(make_bytes.as_bytes());
        }
        // Exif IFD: 1 entry (34855 ISO SHORT)
        d.extend_from_slice(&1u16.to_le_bytes());
        d.extend_from_slice(&34855u16.to_le_bytes());
        d.extend_from_slice(&3u16.to_le_bytes());
        d.extend_from_slice(&1u32.to_le_bytes());
        d.extend_from_slice(&u32::from(iso).to_le_bytes());
        d.extend_from_slice(&0u32.to_le_bytes());
        d
    }

    const SOF0_4X4: [u8; 13] = [0xff, 0xc0, 0, 11, 8, 0, 4, 0, 4, 1, 1, 0x11, 0];
    const SOS_1C: [u8; 10] = [0xff, 0xda, 0, 8, 1, 1, 0, 0, 63, 0];

    #[test]
    fn reads_dng_and_jpeg_capture_and_detects_mismatch() {
        let raw_tiff = sample_tiff("RICOH", 200);
        let raw_cap = from_dng(&raw_tiff).unwrap();
        assert_eq!(
            raw_cap,
            vec![
                ("Make".into(), Field::Text("ricoh".into())),
                ("ISO".into(), Field::Number(200.0)),
            ]
        );

        let exif_payload = [b"Exif\x00\x00".as_slice(), &sample_tiff("RICOH", 400)].concat();
        let app1 = gxr_hdr::app_segment(0xe1, &exif_payload).unwrap();
        let mut jpg = vec![0xff, 0xd8];
        jpg.extend_from_slice(&app1);
        jpg.extend_from_slice(&SOF0_4X4);
        jpg.extend_from_slice(&SOS_1C);
        jpg.extend_from_slice(&[0xff, 0xd9]);

        let err = check_capture(&jpg, &raw_cap).unwrap_err();
        assert_eq!(err.kind, "capture_mismatch");

        // Header-only JPEG missing EOI still succeeds like Pillow Image.open
        let no_eoi = &jpg[..jpg.len() - 2];
        assert_eq!(
            check_capture(no_eoi, &raw_cap).unwrap_err().kind,
            "capture_mismatch"
        );
    }

    #[test]
    fn rejects_invalid_dng_and_jpeg_as_metadata_unreadable() {
        assert_eq!(from_dng(b"bad").unwrap_err().kind, "metadata_unreadable");
        assert_eq!(from_jpeg(b"bad").unwrap_err().kind, "metadata_unreadable");

        // Missing SOF0 (SOI + SOS + EOI only) must be rejected like Pillow Image.open
        let minimal = [0xff, 0xd8, 0xff, 0xda, 0, 8, 1, 1, 0, 0, 63, 0, 0xff, 0xd9];
        assert_eq!(from_jpeg(&minimal).unwrap_err().kind, "metadata_unreadable");

        // Truncation inside SOS header or before SOS is rejected; exact end of SOS is accepted
        let mut hdr = vec![0xff, 0xd8];
        hdr.extend_from_slice(&SOF0_4X4);
        assert_eq!(from_jpeg(&hdr).unwrap_err().kind, "metadata_unreadable");
        hdr.extend_from_slice(&SOS_1C);
        assert_eq!(
            from_jpeg(&hdr[..hdr.len() - 1]).unwrap_err().kind,
            "metadata_unreadable"
        );
        assert!(from_jpeg(&hdr).is_ok());
    }
}
