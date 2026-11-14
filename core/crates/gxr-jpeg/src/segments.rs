//! Port of `gxr_studio/hdr/container.py` JPEG segment parsing and metadata container helpers.

use gxr_raw::{CoreError, Json};

pub const EXIF_SIG: &[u8] = b"Exif\x00\x00";
pub const ICC_SIG: &[u8] = b"ICC_PROFILE\x00";
pub const MAX_MARKER_PAYLOAD: usize = 65533; // 0xFFFF - 2

/// Port of `gxr_studio/hdr/container.py:34 Segment`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Segment {
    pub marker: u8,
    /// Index of `0xFF` in the source buffer.
    pub start: usize,
    /// Exclusive end of the segment (`start + 2 + length`).
    pub end: usize,
}

impl Segment {
    /// Port of `gxr_studio/hdr/container.py:40 Segment.payload`.
    pub fn payload<'a>(&self, data: &'a [u8]) -> &'a [u8] {
        data.get(self.start + 4..self.end).unwrap_or(&[])
    }

    /// Length field as stored in the JPEG header (`end - start - 2`).
    pub fn length(&self) -> usize {
        self.end.saturating_sub(self.start + 2)
    }
}

/// Port of `gxr_studio/hdr/container.py:55 _u16`.
fn u16_at(data: &[u8], pos: usize) -> Result<u16, CoreError> {
    let end = pos
        .checked_add(2)
        .ok_or_else(|| CoreError::new("container", "JPEG 在段中间结束"))?;
    let slice = data
        .get(pos..end)
        .ok_or_else(|| CoreError::new("container", "JPEG 在段中间结束"))?;
    Ok(u16::from_be_bytes([slice[0], slice[1]]))
}

/// Port of `gxr_studio/hdr/container.py:61 jpeg_segments`.
///
/// Returns `(segments_before_sos, sos_offset, eoi_end_offset)`.
pub fn jpeg_segments(data: &[u8], start: usize) -> Result<(Vec<Segment>, usize, usize), CoreError> {
    if start > data.len() || data.get(start..start + 2) != Some(&[0xFF, 0xD8]) {
        return Err(CoreError::new("container", "不是 JPEG（缺 SOI）"));
    }
    let mut i = start + 2;
    let mut segs = Vec::new();
    let sos;
    loop {
        while i + 1 < data.len() && data[i] == 0xFF && data[i + 1] == 0xFF {
            i += 1;
        }
        if i + 1 >= data.len() || data[i] != 0xFF {
            return Err(CoreError::new("container", format!("段结构错误 @ {i}")));
        }
        let marker = data[i + 1];
        if marker == 0xDA {
            sos = i;
            break;
        }
        let length = usize::from(u16_at(data, i + 2)?);
        let end = i
            .checked_add(2)
            .and_then(|v| v.checked_add(length))
            .ok_or_else(|| CoreError::new("container", format!("段长度错误 @ {i}")))?;
        if length < 2 || end > data.len() {
            return Err(CoreError::new("container", format!("段长度错误 @ {i}")));
        }
        segs.push(Segment {
            marker,
            start: i,
            end,
        });
        i = end;
    }

    let sos_len = usize::from(u16_at(data, sos + 2)?);
    let mut j = sos
        .checked_add(2)
        .and_then(|v| v.checked_add(sos_len))
        .ok_or_else(|| CoreError::new("container", "JPEG 缺 EOI"))?;
    loop {
        let Some(rel) = data
            .get(j..)
            .and_then(|s| s.iter().position(|&b| b == 0xFF))
        else {
            return Err(CoreError::new("container", "JPEG 缺 EOI"));
        };
        j += rel;
        let Some(&nxt) = data.get(j + 1) else {
            return Err(CoreError::new("container", "JPEG 缺 EOI"));
        };
        if nxt == 0xD9 {
            return Ok((segs, sos, j + 2));
        }
        if nxt == 0x00 || (0xD0..=0xD7).contains(&nxt) {
            j += 2;
        } else if nxt == 0xFF {
            j += 1;
        } else {
            let skip = usize::from(u16_at(data, j + 2)?);
            j = j
                .checked_add(2)
                .and_then(|v| v.checked_add(skip))
                .ok_or_else(|| CoreError::new("container", "JPEG 缺 EOI"))?;
        }
    }
}

/// Port of `gxr_studio/hdr/container.py:93 app_segment`.
pub fn app_segment(marker: u8, payload: &[u8]) -> Result<Vec<u8>, CoreError> {
    let total_len = payload
        .len()
        .checked_add(2)
        .ok_or_else(|| CoreError::new("container", "APP 段超过 64 KB"))?;
    let len_u16 =
        u16::try_from(total_len).map_err(|_| CoreError::new("container", "APP 段超过 64 KB"))?;
    let mut out = Vec::with_capacity(2 + total_len);
    out.push(0xFF);
    out.push(marker);
    out.extend_from_slice(&len_u16.to_be_bytes());
    out.extend_from_slice(payload);
    Ok(out)
}

/// Human-readable JPEG marker name.
pub fn marker_name(marker: u8) -> String {
    match marker {
        0xC0 => "SOF0".into(),
        0xC1 => "SOF1".into(),
        0xC2 => "SOF2".into(),
        0xC3 => "SOF3".into(),
        0xC4 => "DHT".into(),
        0xC5 => "SOF5".into(),
        0xC6 => "SOF6".into(),
        0xC7 => "SOF7".into(),
        0xC9 => "SOF9".into(),
        0xCA => "SOF10".into(),
        0xCB => "SOF11".into(),
        0xCC => "DAC".into(),
        0xCD => "SOF13".into(),
        0xCE => "SOF14".into(),
        0xCF => "SOF15".into(),
        0xDA => "SOS".into(),
        0xDB => "DQT".into(),
        0xDC => "DNL".into(),
        0xDD => "DRI".into(),
        0xDE => "DHP".into(),
        0xDF => "EXP".into(),
        0xE0..=0xEF => format!("APP{}", marker - 0xE0),
        0xFE => "COM".into(),
        other => format!("0x{other:02X}"),
    }
}

/// Extract image `(width, height)` from the first SOF marker before SOS.
pub fn sof_dimensions(data: &[u8]) -> Result<(u32, u32), CoreError> {
    let (segs, _, _) = jpeg_segments(data, 0)?;
    for seg in segs {
        let is_sof = matches!(
            seg.marker,
            0xC0..=0xC3 | 0xC5..=0xC7 | 0xC9..=0xCB | 0xCD..=0xCF
        );
        if is_sof {
            let p = seg.payload(data);
            if p.len() >= 5 {
                let h = u32::from(u16::from_be_bytes([p[1], p[2]]));
                let w = u32::from(u16::from_be_bytes([p[3], p[4]]));
                return Ok((w, h));
            }
        }
    }
    Err(CoreError::new("container", "JPEG 缺 SOF 段"))
}

/// Build `APP2` `ICC_PROFILE` segments (chunked at 65519 bytes like Pillow's `JpegImagePlugin`).
pub fn icc_segments(icc_profile: &[u8]) -> Result<Vec<u8>, CoreError> {
    if icc_profile.is_empty() {
        return Ok(Vec::new());
    }
    let chunk_cap = MAX_MARKER_PAYLOAD - (ICC_SIG.len() + 2);
    let chunks: Vec<&[u8]> = icc_profile.chunks(chunk_cap).collect();
    let total = u8::try_from(chunks.len())
        .map_err(|_| CoreError::new("container", "ICC profile too large"))?;
    let mut out = Vec::new();
    for (idx, chunk) in chunks.iter().enumerate() {
        let seq = u8::try_from(idx + 1)
            .map_err(|_| CoreError::new("container", "ICC profile too large"))?;
        let mut payload = Vec::with_capacity(ICC_SIG.len() + 2 + chunk.len());
        payload.extend_from_slice(ICC_SIG);
        payload.push(seq);
        payload.push(total);
        payload.extend_from_slice(chunk);
        out.extend_from_slice(&app_segment(0xE2, &payload)?);
    }
    Ok(out)
}

/// Insert or replace `APP1` EXIF and/or `APP2` ICC in an existing JPEG without re-encoding.
///
/// Preserves `APP0` immediately after `SOI`, writes the new `APP1` EXIF (if `Some`) and `APP2`
/// ICC (if `Some`), keeps all other `APP` segments (not replaced) and all non-`APP` segments
/// before `SOS`, and copies `data[sos..eoi]` verbatim.
pub fn set_meta(
    data: &[u8],
    exif_payload: Option<&[u8]>,
    icc_payload: Option<&[u8]>,
) -> Result<Vec<u8>, CoreError> {
    let (segs, sos, eoi) = jpeg_segments(data, 0)?;
    let mut keep_app0 = Vec::new();
    let mut keep_app_other = Vec::new();
    let mut keep_rest = Vec::new();

    for seg in &segs {
        let payload = seg.payload(data);
        if exif_payload.is_some() && seg.marker == 0xE1 && payload.starts_with(EXIF_SIG) {
            continue;
        }
        if icc_payload.is_some() && seg.marker == 0xE2 && payload.starts_with(ICC_SIG) {
            continue;
        }
        let raw = &data[seg.start..seg.end];
        if seg.marker == 0xE0 {
            keep_app0.extend_from_slice(raw);
        } else if (0xE1..=0xEF).contains(&seg.marker) {
            keep_app_other.extend_from_slice(raw);
        } else {
            keep_rest.extend_from_slice(raw);
        }
    }

    let exif_seg = match exif_payload {
        Some(p) => app_segment(0xE1, p)?,
        None => Vec::new(),
    };
    let icc_seg = match icc_payload {
        Some(p) => icc_segments(p)?,
        None => Vec::new(),
    };

    let scan = &data[sos..eoi];
    let cap = 2
        + keep_app0.len()
        + exif_seg.len()
        + icc_seg.len()
        + keep_app_other.len()
        + keep_rest.len()
        + scan.len();
    let mut out = Vec::with_capacity(cap);
    out.extend_from_slice(&[0xFF, 0xD8]);
    out.extend_from_slice(&keep_app0);
    out.extend_from_slice(&exif_seg);
    out.extend_from_slice(&icc_seg);
    out.extend_from_slice(&keep_app_other);
    out.extend_from_slice(&keep_rest);
    out.extend_from_slice(scan);
    Ok(out)
}

/// JSON summary of segments before SOS, plus `sos` and `eoi` offsets (for `jpeg-segments`).
pub fn segments_json(data: &[u8]) -> Result<Json, CoreError> {
    let (segs, sos, eoi) = jpeg_segments(data, 0)?;
    let mut items = Vec::with_capacity(segs.len());
    for seg in segs {
        let offset = i64::try_from(seg.start)
            .map_err(|_| CoreError::new("container", "segment offset overflow"))?;
        let length = i64::try_from(seg.length())
            .map_err(|_| CoreError::new("container", "segment length overflow"))?;
        items.push(Json::object([
            ("marker", Json::Int(i64::from(seg.marker))),
            ("name", Json::str(marker_name(seg.marker))),
            ("offset", Json::Int(offset)),
            ("length", Json::Int(length)),
        ]));
    }
    let sos_len = usize::from(u16_at(data, sos + 2)?);
    items.push(Json::object([
        ("marker", Json::Int(0xDA)),
        ("name", Json::str("SOS")),
        (
            "offset",
            Json::Int(
                i64::try_from(sos)
                    .map_err(|_| CoreError::new("container", "sos offset overflow"))?,
            ),
        ),
        (
            "length",
            Json::Int(
                i64::try_from(sos_len)
                    .map_err(|_| CoreError::new("container", "sos length overflow"))?,
            ),
        ),
    ]));
    Ok(Json::object([
        ("segments", Json::Array(items)),
        (
            "sos",
            Json::Int(
                i64::try_from(sos)
                    .map_err(|_| CoreError::new("container", "sos offset overflow"))?,
            ),
        ),
        (
            "eoi",
            Json::Int(
                i64::try_from(eoi)
                    .map_err(|_| CoreError::new("container", "eoi offset overflow"))?,
            ),
        ),
    ]))
}
