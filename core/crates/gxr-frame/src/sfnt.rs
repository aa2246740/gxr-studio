//! TrueType metrics. Port of `gxr_studio/finish/sfnt.py`.
//!
//! OpenType CFF (no `loca` / `glyf`) fails the same way Python does: `KeyError` on the first
//! missing table. The phone passes font bytes in; nothing in this crate embeds a font file.

use std::collections::BTreeMap;

use gxr_raw::CoreError;

/// One glyph: id, advance width, and the ink box `(x_min, y_min, x_max, y_max)` in font units.
///
/// An empty outline (`loca` range of length 0, or a box with no area) has `ink == None`,
/// matching `Glyph`'s `Box | None` in `sfnt.py`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Glyph {
    pub id: u32,
    pub advance: u32,
    pub ink: Option<[i32; 4]>,
}

/// Port of `gxr_studio/finish/sfnt.py:24` `FontTable`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FontTable {
    pub upm: i64,
    pub ascender: i64,
    pub notdef: Glyph,
    /// Code point → glyph. Missing code points use [`FontTable::glyph`], which returns `notdef`.
    pub glyphs: BTreeMap<u32, Glyph>,
}

impl FontTable {
    /// Port of `gxr_studio/finish/sfnt.py:30` `FontTable.glyph`.
    pub fn glyph(&self, code: u32) -> &Glyph {
        self.glyphs.get(&code).unwrap_or(&self.notdef)
    }
}

/// Port of `gxr_studio/finish/sfnt.py:101` `read_font` (the bytes after `path.read_bytes()`).
pub fn read_font(data: &[u8]) -> Result<FontTable, CoreError> {
    let tables = table_directory(data)?;
    let head = table_at(&tables, "head")?;
    let hhea = table_at(&tables, "hhea")?;
    let maxp = table_at(&tables, "maxp")?;
    let hmtx = table_at(&tables, "hmtx")?;
    let loca = table_at(&tables, "loca")?;
    let glyf = table_at(&tables, "glyf")?;
    let cmap_at = table_at(&tables, "cmap")?;

    let upm = i64::from(be_u16(data, head + 18)?);
    let long_loca = be_i16(data, head + 50)? == 1;
    let ascender = i64::from(be_i16(data, hhea + 4)?);
    let metrics = u64::from(be_u16(data, hhea + 34)?);
    let glyph_count = u64::from(be_u16(data, maxp + 4)?);
    if glyph_count == 0 {
        return Err(CoreError::new("index_error", "list index out of range"));
    }
    if metrics == 0 {
        return Err(CoreError::new("value_error", "hmtx 没有度量"));
    }

    let mut advances = Vec::new();
    for g in 0..glyph_count {
        let slot = g.min(metrics - 1);
        advances.push(u32::from(be_u16(data, hmtx + slot * 4)?));
    }
    let offsets = loca_offsets(data, loca, glyph_count, long_loca)?;
    let mut by_glyph = Vec::with_capacity(usize_from(glyph_count)?);
    for g in 0..glyph_count {
        let advance = advances[usize_from(g)?];
        by_glyph.push(Glyph {
            id: u32_from(g)?,
            advance,
            ink: ink_box(data, glyf, &offsets, g)?,
        });
    }

    let mut glyphs = BTreeMap::new();
    for (code, gid) in cmap(data, cmap_at)? {
        if gid > 0 && gid < glyph_count {
            let glyph = by_glyph[usize_from(gid)?].clone();
            glyphs.insert(code, glyph);
        }
    }
    let notdef = by_glyph.first().cloned().ok_or_else(|| {
        CoreError::new("index_error", "list index out of range")
    })?;
    Ok(FontTable {
        upm,
        ascender,
        notdef,
        glyphs,
    })
}

/// Port of `gxr_studio/finish/sfnt.py:49` `_tables`.
fn table_directory(data: &[u8]) -> Result<BTreeMap<String, u64>, CoreError> {
    if data.len() >= 4 && &data[0..4] == b"ttcf" {
        return Err(CoreError::new("value_error", "字体集合（.ttc）不读"));
    }
    let count = u64::from(be_u16(data, 4)?);
    let mut out = BTreeMap::new();
    for i in 0..count {
        let at = 12 + 16 * i;
        let tag = read_exact(data, at, 4)?;
        let mut name = String::new();
        for byte in tag {
            name.push(char::from(*byte));
        }
        let offset = u64::from(be_u32(data, at + 8)?);
        out.insert(name, offset);
    }
    Ok(out)
}

fn table_at(tables: &BTreeMap<String, u64>, name: &str) -> Result<u64, CoreError> {
    tables
        .get(name)
        .copied()
        .ok_or_else(|| CoreError::new("key_error", format!("'{name}'")))
}

/// Port of `gxr_studio/finish/sfnt.py:60` `_cmap`.
///
/// Values are glyph ids, including 0. `read_font` drops id 0 and ids past `maxp`.
fn cmap(data: &[u8], offset: u64) -> Result<BTreeMap<u32, u64>, CoreError> {
    let count = u64::from(be_u16(data, offset + 2)?);
    let mut subtables = Vec::new();
    for i in 0..count {
        let rec = offset + 4 + 8 * i;
        let platform = be_u16(data, rec)?;
        let encoding = be_u16(data, rec + 2)?;
        let sub = u64::from(be_u32(data, rec + 4)?);
        let at = offset + sub;
        let fmt = be_u16(data, at)?;
        subtables.push((platform, encoding, at, fmt));
    }
    let chosen = subtables
        .iter()
        .find(|s| s.3 == 12 && matches!((s.0, s.1), (3, 10) | (0, 4) | (0, 6)))
        .or_else(|| {
            subtables
                .iter()
                .find(|s| s.3 == 4 && matches!((s.0, s.1), (3, 1) | (0, 3) | (0, 4)))
        });
    let Some((_platform, _encoding, at, fmt)) = chosen else {
        return Err(CoreError::new(
            "value_error",
            "cmap 里没有格式 4 或 12 的 Unicode 子表",
        ));
    };
    if *fmt == 12 {
        cmap12(data, *at)
    } else {
        cmap4(data, *at)
    }
}

fn cmap12(data: &[u8], at: u64) -> Result<BTreeMap<u32, u64>, CoreError> {
    let groups = u64::from(be_u32(data, at + 12)?);
    let mut out = BTreeMap::new();
    for i in 0..groups {
        let rec = at + 16 + 12 * i;
        let first = be_u32(data, rec)?;
        let last = be_u32(data, rec + 4)?;
        let glyph = u64::from(be_u32(data, rec + 8)?);
        if first > last {
            continue;
        }
        let span = u64::from(last - first) + 1;
        note_span(out.len(), span)?;
        let mut code = first;
        loop {
            let id = glyph + u64::from(code - first);
            out.insert(code, id);
            if code == last {
                break;
            }
            code += 1;
        }
    }
    Ok(out)
}

fn cmap4(data: &[u8], at: u64) -> Result<BTreeMap<u32, u64>, CoreError> {
    let seg2 = u64::from(be_u16(data, at + 6)?);
    let segs = seg2 / 2;
    let ends = be_u16s(data, at + 14, segs)?;
    let starts = be_u16s(data, at + 16 + seg2, segs)?;
    let deltas = be_i16s(data, at + 16 + 2 * seg2, segs)?;
    let ranges_at = at + 16 + 3 * seg2;
    let ranges = be_u16s(data, ranges_at, segs)?;
    let mut out = BTreeMap::new();
    for i in 0..usize_from(segs)? {
        let start = u32::from(starts[i]);
        let end = u32::from(ends[i]);
        if start > end {
            continue;
        }
        note_span(out.len(), u64::from(end - start) + 1)?;
        let mut code = start;
        loop {
            if code != 0xFFFF {
                let glyph = if ranges[i] == 0 {
                    low16(i64::from(code) + i64::from(deltas[i]))?
                } else {
                    let at_glyph = ranges_at
                        + 2 * u64_from_usize(i)?
                        + u64::from(ranges[i])
                        + 2 * u64::from(code - start);
                    let mut glyph = u64::from(be_u16(data, at_glyph)?);
                    if glyph != 0 {
                        glyph = u64::from(low16(i64::try_from(glyph).map_err(|_| {
                            CoreError::new("value_error", "glyph id 超出范围")
                        })? + i64::from(deltas[i]))?);
                    }
                    glyph
                };
                out.insert(code, glyph);
            }
            if code == end {
                break;
            }
            code += 1;
        }
    }
    Ok(out)
}

fn loca_offsets(data: &[u8], loca: u64, glyph_count: u64, long_loca: bool) -> Result<Vec<u64>, CoreError> {
    let n = glyph_count + 1;
    if long_loca {
        let words = be_u32s(data, loca, n)?;
        Ok(words.into_iter().map(u64::from).collect())
    } else {
        let words = be_u16s(data, loca, n)?;
        Ok(words.into_iter().map(|v| u64::from(v) * 2).collect())
    }
}

fn ink_box(data: &[u8], glyf: u64, offsets: &[u64], glyph: u64) -> Result<Option<[i32; 4]>, CoreError> {
    let g = usize_from(glyph)?;
    let start = offsets.get(g).copied().ok_or_else(|| {
        CoreError::new("index_error", "list index out of range")
    })?;
    let end = offsets.get(g + 1).copied().ok_or_else(|| {
        CoreError::new("index_error", "list index out of range")
    })?;
    if end == start {
        return Ok(None);
    }
    let at = glyf + start;
    let x0 = i32::from(be_i16(data, at + 2)?);
    let y0 = i32::from(be_i16(data, at + 4)?);
    let x1 = i32::from(be_i16(data, at + 6)?);
    let y1 = i32::from(be_i16(data, at + 8)?);
    if x1 > x0 && y1 > y0 {
        Ok(Some([x0, y0, x1, y1]))
    } else {
        Ok(None)
    }
}

/// A cmap that claims more than this many code points is refused.
/// Real bundled fonts are far smaller; the cap keeps a hostile file from allocating without bound.
const MAX_CODES: usize = 1_048_576;

fn note_span(have: usize, span: u64) -> Result<(), CoreError> {
    let extra = usize::try_from(span).map_err(|_| CoreError::new("value_error", "cmap 范围太大"))?;
    if have.saturating_add(extra) > MAX_CODES {
        return Err(CoreError::new("value_error", "cmap 范围太大"));
    }
    Ok(())
}

fn low16(sum: i64) -> Result<u64, CoreError> {
    let wrapped = sum.rem_euclid(65536);
    u64::try_from(wrapped).map_err(|_| CoreError::new("value_error", "glyph id 超出范围"))
}

fn struct_error(n: u64) -> CoreError {
    CoreError::new(
        "struct_error",
        format!("unpack requires a buffer of {n} bytes"),
    )
}

fn read_exact(data: &[u8], at: u64, n: u64) -> Result<&[u8], CoreError> {
    let start = usize::try_from(at).map_err(|_| struct_error(n))?;
    let len = usize::try_from(n).map_err(|_| struct_error(n))?;
    let end = start.checked_add(len).ok_or_else(|| struct_error(n))?;
    data.get(start..end).ok_or_else(|| struct_error(n))
}

fn be_u16(data: &[u8], at: u64) -> Result<u16, CoreError> {
    let b = read_exact(data, at, 2)?;
    Ok(u16::from_be_bytes([b[0], b[1]]))
}

fn be_i16(data: &[u8], at: u64) -> Result<i16, CoreError> {
    let b = read_exact(data, at, 2)?;
    Ok(i16::from_be_bytes([b[0], b[1]]))
}

fn be_u32(data: &[u8], at: u64) -> Result<u32, CoreError> {
    let b = read_exact(data, at, 4)?;
    Ok(u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
}

fn be_u16s(data: &[u8], at: u64, count: u64) -> Result<Vec<u16>, CoreError> {
    let n = count.checked_mul(2).ok_or_else(|| struct_error(u64::MAX))?;
    let bytes = read_exact(data, at, n)?;
    let mut out = Vec::with_capacity(usize_from(count)?);
    for chunk in bytes.chunks(2) {
        out.push(u16::from_be_bytes([chunk[0], chunk[1]]));
    }
    Ok(out)
}

fn be_i16s(data: &[u8], at: u64, count: u64) -> Result<Vec<i16>, CoreError> {
    let n = count.checked_mul(2).ok_or_else(|| struct_error(u64::MAX))?;
    let bytes = read_exact(data, at, n)?;
    let mut out = Vec::with_capacity(usize_from(count)?);
    for chunk in bytes.chunks(2) {
        out.push(i16::from_be_bytes([chunk[0], chunk[1]]));
    }
    Ok(out)
}

fn be_u32s(data: &[u8], at: u64, count: u64) -> Result<Vec<u32>, CoreError> {
    let n = count.checked_mul(4).ok_or_else(|| struct_error(u64::MAX))?;
    let bytes = read_exact(data, at, n)?;
    let mut out = Vec::with_capacity(usize_from(count)?);
    for chunk in bytes.chunks(4) {
        out.push(u32::from_be_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]));
    }
    Ok(out)
}

fn usize_from(v: u64) -> Result<usize, CoreError> {
    usize::try_from(v).map_err(|_| CoreError::new("value_error", "字形数量超出范围"))
}

fn u64_from_usize(v: usize) -> Result<u64, CoreError> {
    u64::try_from(v).map_err(|_| CoreError::new("value_error", "字形数量超出范围"))
}

fn u32_from(v: u64) -> Result<u32, CoreError> {
    u32::try_from(v).map_err(|_| CoreError::new("value_error", "glyph id 超出范围"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_a_collection_and_a_short_file() {
        let collection = read_font(b"ttcfrest");
        assert!(collection.is_err());
        assert_eq!(collection.unwrap_err().message, "字体集合（.ttc）不读");
        let short = read_font(b"OTTO");
        assert!(short.is_err());
        assert_eq!(
            short.unwrap_err().message,
            "unpack requires a buffer of 2 bytes"
        );
    }
}
