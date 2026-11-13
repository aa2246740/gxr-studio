//! Port of `gxr_studio/hdr/container.py`. The gain JPEG is an input, not encoded here.

use gxr_raw::CoreError;

use crate::error::container;
use crate::number::{fmt_g7, frac_65536};

const XMP_NS: &[u8] = b"http://ns.adobe.com/xap/1.0/\x00";
const ISO_NS: &[u8] = b"urn:iso:std:iso:ts:21496:-1\x00";
const MPF_SIG: &[u8] = b"MPF\x00";
const XMPTK: &str = "GXR Studio";
/// `4 + 4 + 8 + 2 + 36 + 4 + 32`.
pub const MPF_SEG_LEN: usize = 90;

/// Port of gxr_studio/hdr/container.py:46 GainMeta.
#[derive(Clone, Debug)]
pub struct GainMeta {
    pub gain_min: f64,
    pub gain_max: f64,
    pub gamma: f64,
    pub offset_sdr: f64,
    pub offset_hdr: f64,
    pub cap_min: f64,
    pub cap_max: f64,
}

impl Default for GainMeta {
    fn default() -> Self {
        Self {
            gain_min: 0.0,
            gain_max: 1.0,
            gamma: 1.0,
            offset_sdr: 1.0 / 64.0,
            offset_hdr: 1.0 / 64.0,
            cap_min: 0.0,
            cap_max: 1.0,
        }
    }
}

struct Segment {
    marker: u8,
    start: usize,
    end: usize,
}

fn u16_be(data: &[u8], pos: usize) -> Result<usize, CoreError> {
    if pos.checked_add(2).is_none_or(|end| end > data.len()) {
        return Err(container("JPEG 在段中间结束"));
    }
    Ok(usize::from(u16::from_be_bytes([data[pos], data[pos + 1]])))
}

/// Port of gxr_studio/hdr/container.py:64 jpeg_segments.
pub fn jpeg_segments(data: &[u8], start: usize) -> Result<(Vec<(u8, usize, usize)>, usize, usize), CoreError> {
    if start.checked_add(2).is_none_or(|end| end > data.len()) || data[start] != 0xff || data[start + 1] != 0xd8 {
        return Err(container("不是 JPEG（缺 SOI）"));
    }
    let mut i = start + 2;
    let mut segs = Vec::new();
    let sos = loop {
        while i + 1 < data.len() && data[i] == 0xff && data[i + 1] == 0xff {
            i += 1;
        }
        if i + 1 >= data.len() || data[i] != 0xff {
            return Err(container(&format!("段结构错误 @ {i}")));
        }
        let marker = data[i + 1];
        if marker == 0xda {
            break i;
        }
        let length = u16_be(data, i + 2)?;
        if length < 2 || i + 2 + length > data.len() {
            return Err(container(&format!("段长度错误 @ {i}")));
        }
        segs.push(Segment {
            marker,
            start: i,
            end: i + 2 + length,
        });
        i += 2 + length;
    };
    let mut j = sos + 2 + u16_be(data, sos + 2)?;
    loop {
        let found = data[j..].iter().position(|b| *b == 0xff).map(|p| j + p);
        let Some(j_at) = found else {
            return Err(container("JPEG 缺 EOI"));
        };
        j = j_at;
        if j + 1 >= data.len() {
            return Err(container("JPEG 缺 EOI"));
        }
        let nxt = data[j + 1];
        if nxt == 0xd9 {
            let out = segs.into_iter().map(|s| (s.marker, s.start, s.end)).collect();
            return Ok((out, sos, j + 2));
        }
        if nxt == 0x00 || (0xd0..=0xd7).contains(&nxt) {
            j += 2;
        } else if nxt == 0xff {
            j += 1;
        } else {
            j += 2 + u16_be(data, j + 2)?;
        }
    }
}

/// Port of gxr_studio/hdr/container.py:102 app_segment.
pub fn app_segment(marker: u8, payload: &[u8]) -> Result<Vec<u8>, CoreError> {
    if payload.len() + 2 > 0xffff {
        return Err(container("APP 段超过 64 KB"));
    }
    let mut out = vec![0xff, marker];
    let len = (payload.len() + 2) as u16;
    out.extend_from_slice(&len.to_be_bytes());
    out.extend_from_slice(payload);
    Ok(out)
}

/// Port of gxr_studio/hdr/container.py:112 xmp_primary.
pub fn xmp_primary(gain_len: usize) -> Vec<u8> {
    format!(
        "<x:xmpmeta xmlns:x=\"adobe:ns:meta/\" x:xmptk=\"{XMPTK}\">\n <rdf:RDF xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\">\n  <rdf:Description rdf:about=\"\"\n    xmlns:Container=\"http://ns.google.com/photos/1.0/container/\"\n    xmlns:Item=\"http://ns.google.com/photos/1.0/container/item/\"\n    xmlns:hdrgm=\"http://ns.adobe.com/hdr-gain-map/1.0/\"\n   hdrgm:Version=\"1.0\">\n   <Container:Directory>\n    <rdf:Seq>\n     <rdf:li rdf:parseType=\"Resource\">\n      <Container:Item Item:Semantic=\"Primary\" Item:Mime=\"image/jpeg\"/>\n     </rdf:li>\n     <rdf:li rdf:parseType=\"Resource\">\n      <Container:Item Item:Semantic=\"GainMap\" Item:Mime=\"image/jpeg\" Item:Length=\"{gain_len}\"/>\n     </rdf:li>\n    </rdf:Seq>\n   </Container:Directory>\n  </rdf:Description>\n </rdf:RDF>\n</x:xmpmeta>"
    )
    .into_bytes()
}

/// Port of gxr_studio/hdr/container.py:138 xmp_gainmap.
pub fn xmp_gainmap(m: &GainMeta) -> Result<Vec<u8>, CoreError> {
    let text = format!(
        "<x:xmpmeta xmlns:x=\"adobe:ns:meta/\" x:xmptk=\"{XMPTK}\">\n <rdf:RDF xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\">\n  <rdf:Description rdf:about=\"\"\n    xmlns:hdrgm=\"http://ns.adobe.com/hdr-gain-map/1.0/\"\n   hdrgm:Version=\"1.0\"\n   hdrgm:GainMapMin=\"{}\"\n   hdrgm:GainMapMax=\"{}\"\n   hdrgm:Gamma=\"{}\"\n   hdrgm:OffsetSDR=\"{}\"\n   hdrgm:OffsetHDR=\"{}\"\n   hdrgm:HDRCapacityMin=\"{}\"\n   hdrgm:HDRCapacityMax=\"{}\"\n   hdrgm:BaseRenditionIsHDR=\"False\"/>\n </rdf:RDF>\n</x:xmpmeta>",
        fmt_g7(m.gain_min)?,
        fmt_g7(m.gain_max)?,
        fmt_g7(m.gamma)?,
        fmt_g7(m.offset_sdr)?,
        fmt_g7(m.offset_hdr)?,
        fmt_g7(m.cap_min)?,
        fmt_g7(m.cap_max)?,
    );
    Ok(text.into_bytes())
}

fn pack_frac(v: f64, signed: bool) -> Result<(i32, u32), CoreError> {
    let (n, d) = frac_65536(v)?;
    if !signed && n < 0 {
        return Err(container("无符号字段不能为负"));
    }
    let numer = i32::try_from(n).map_err(|_| container("'i' format requires -2147483648 <= number <= 2147483647"))?;
    let denom = u32::try_from(d).map_err(|_| container("分母超出范围"))?;
    Ok((numer, denom))
}

/// Port of gxr_studio/hdr/container.py:166 iso_version_only.
pub fn iso_version_only() -> Vec<u8> {
    let mut out = ISO_NS.to_vec();
    out.extend_from_slice(&0u16.to_be_bytes());
    out.extend_from_slice(&0u16.to_be_bytes());
    out
}

/// Port of gxr_studio/hdr/container.py:171 iso_full.
pub fn iso_full(m: &GainMeta) -> Result<Vec<u8>, CoreError> {
    let mut out = ISO_NS.to_vec();
    out.extend_from_slice(&0u16.to_be_bytes());
    out.extend_from_slice(&0u16.to_be_bytes());
    out.push(0x40);
    for v in [m.cap_min, m.cap_max] {
        let (n, d) = pack_frac(v, false)?;
        out.extend_from_slice(&(n as u32).to_be_bytes());
        out.extend_from_slice(&d.to_be_bytes());
    }
    let gmin = pack_frac(m.gain_min, true)?;
    let gmax = pack_frac(m.gain_max, true)?;
    let gam = pack_frac(m.gamma, false)?;
    let bo = pack_frac(m.offset_sdr, true)?;
    let ao = pack_frac(m.offset_hdr, true)?;
    for (n, d, signed) in [
        (gmin.0, gmin.1, true),
        (gmax.0, gmax.1, true),
        (gam.0, gam.1, false),
        (bo.0, bo.1, true),
        (ao.0, ao.1, true),
    ] {
        if signed {
            out.extend_from_slice(&n.to_be_bytes());
        } else {
            out.extend_from_slice(&(n as u32).to_be_bytes());
        }
        out.extend_from_slice(&d.to_be_bytes());
    }
    Ok(out)
}

/// Port of gxr_studio/hdr/container.py:183 mpf_segment.
pub fn mpf_segment(primary_size: u32, gain_size: u32, gain_offset: u32) -> Result<Vec<u8>, CoreError> {
    let mut t = b"MM\x00\x2a".to_vec();
    t.extend_from_slice(&8u32.to_be_bytes());
    t.extend_from_slice(&3u16.to_be_bytes());
    t.extend_from_slice(&0xb000u16.to_be_bytes());
    t.extend_from_slice(&7u16.to_be_bytes());
    t.extend_from_slice(&4u32.to_be_bytes());
    t.extend_from_slice(b"0100");
    t.extend_from_slice(&0xb001u16.to_be_bytes());
    t.extend_from_slice(&4u16.to_be_bytes());
    t.extend_from_slice(&1u32.to_be_bytes());
    t.extend_from_slice(&2u32.to_be_bytes());
    let entry_off = 8u32 + 2 + 3 * 12 + 4;
    t.extend_from_slice(&0xb002u16.to_be_bytes());
    t.extend_from_slice(&7u16.to_be_bytes());
    t.extend_from_slice(&32u32.to_be_bytes());
    t.extend_from_slice(&entry_off.to_be_bytes());
    t.extend_from_slice(&0u32.to_be_bytes());
    t.extend_from_slice(&0x030000u32.to_be_bytes());
    t.extend_from_slice(&primary_size.to_be_bytes());
    t.extend_from_slice(&0u32.to_be_bytes());
    t.extend_from_slice(&0u16.to_be_bytes());
    t.extend_from_slice(&0u16.to_be_bytes());
    t.extend_from_slice(&0u32.to_be_bytes());
    t.extend_from_slice(&gain_size.to_be_bytes());
    t.extend_from_slice(&gain_offset.to_be_bytes());
    t.extend_from_slice(&0u16.to_be_bytes());
    t.extend_from_slice(&0u16.to_be_bytes());
    app_segment(0xe2, &[MPF_SIG, &t].concat())
}

fn replaced(marker: u8, payload: &[u8]) -> bool {
    (marker == 0xe2 && (payload.starts_with(MPF_SIG) || payload.starts_with(ISO_NS)))
        || (marker == 0xe1 && payload.starts_with(XMP_NS))
}

/// Port of gxr_studio/hdr/container.py:214 assemble.
pub fn assemble(jpg: &[u8], gain_jpeg: &[u8], meta: &GainMeta) -> Result<Vec<u8>, CoreError> {
    let (segs, sos, eoi) = jpeg_segments(jpg, 0)?;
    let mut keep_app = Vec::new();
    let mut keep_rest = Vec::new();
    for (marker, start, end) in segs {
        let payload = if end >= start + 4 { &jpg[start + 4..end] } else { &[] };
        if replaced(marker, payload) {
            continue;
        }
        if (0xe0..=0xef).contains(&marker) {
            keep_app.extend_from_slice(&jpg[start..end]);
        } else {
            keep_rest.extend_from_slice(&jpg[start..end]);
        }
    }
    let (gsegs, gsos, geoi) = jpeg_segments(gain_jpeg, 0)?;
    let mut gplain = Vec::new();
    for (marker, start, end) in gsegs {
        if marker != 0xe1 && marker != 0xe2 {
            gplain.extend_from_slice(&gain_jpeg[start..end]);
        }
    }
    gplain.extend_from_slice(&gain_jpeg[gsos..geoi]);
    let gain_xmp = xmp_gainmap(meta)?;
    let secondary = [
        b"\xff\xd8".as_slice(),
        &app_segment(0xe1, &[XMP_NS, &gain_xmp].concat())?,
        &app_segment(0xe2, &iso_full(meta)?)?,
        &gplain,
    ]
    .concat();
    let head = [
        b"\xff\xd8".as_slice(),
        &keep_app,
        &app_segment(0xe1, &[XMP_NS, &xmp_primary(secondary.len())].concat())?,
        &app_segment(0xe2, &iso_version_only())?,
    ]
    .concat();
    let primary_size = head
        .len()
        .checked_add(MPF_SEG_LEN)
        .and_then(|n| n.checked_add(keep_rest.len()))
        .and_then(|n| n.checked_add(eoi - sos))
        .ok_or_else(|| container("MPF 长度不一致"))?;
    let tiff_pos = head.len() + 8;
    let mpf = mpf_segment(
        u32::try_from(primary_size).map_err(|_| container("MPF 长度不一致"))?,
        u32::try_from(secondary.len()).map_err(|_| container("MPF 长度不一致"))?,
        u32::try_from(primary_size - tiff_pos).map_err(|_| container("MPF 长度不一致"))?,
    )?;
    let mut out = head;
    out.extend_from_slice(&mpf);
    out.extend_from_slice(&keep_rest);
    out.extend_from_slice(&jpg[sos..eoi]);
    out.extend_from_slice(&secondary);
    if mpf.len() != MPF_SEG_LEN || out.len() != primary_size + secondary.len() {
        return Err(container("MPF 长度不一致"));
    }
    Ok(out)
}
