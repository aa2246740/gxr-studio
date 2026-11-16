//! SFNT table directory & big-endian byte reading helpers for `ink_text::glyf`.

use std::collections::BTreeMap;

use gxr_raw::CoreError;

const OS2_FLAG_USE_TYPO_METRICS: u16 = 128;

pub(super) fn select_asc_desc(
    data: &[u8],
    dir: &BTreeMap<String, (u64, u64)>,
    hhea_asc: i16,
    hhea_desc: i16,
) -> Result<(i64, i64), CoreError> {
    if let Some(&(os2_at, os2_len)) = dir.get("OS/2")
        && os2_len >= 78
    {
        let fs_sel = be_u16(data, os2_at + 62)?;
        let typo_asc = be_i16(data, os2_at + 68)?;
        let typo_desc = be_i16(data, os2_at + 70)?;
        if (fs_sel & OS2_FLAG_USE_TYPO_METRICS) != 0 {
            return Ok((i64::from(typo_asc), i64::from(typo_desc)));
        }
        if hhea_asc != 0 || hhea_desc != 0 {
            return Ok((i64::from(hhea_asc), i64::from(hhea_desc)));
        }
        if typo_asc != 0 || typo_desc != 0 {
            return Ok((i64::from(typo_asc), i64::from(typo_desc)));
        }
        return Ok((
            i64::from(be_u16(data, os2_at + 74)?),
            -i64::from(be_u16(data, os2_at + 76)?),
        ));
    }
    Ok((i64::from(hhea_asc), i64::from(hhea_desc)))
}

pub(super) fn table_directory(data: &[u8]) -> Result<BTreeMap<String, (u64, u64)>, CoreError> {
    let count = u64::from(be_u16(data, 4)?);
    let mut out = BTreeMap::new();
    for i in 0..count {
        let at = 12 + 16 * i;
        let tag = read_exact(data, at, 4)?;
        let name: String = tag.iter().map(|&b| char::from(b)).collect();
        out.insert(
            name,
            (
                u64::from(be_u32(data, at + 8)?),
                u64::from(be_u32(data, at + 12)?),
            ),
        );
    }
    Ok(out)
}

pub(super) fn table_span(
    dir: &BTreeMap<String, (u64, u64)>,
    name: &str,
) -> Result<(u64, u64), CoreError> {
    dir.get(name)
        .copied()
        .ok_or_else(|| CoreError::new("key_error", format!("'{name}'")))
}

pub(super) fn invalid_outline() -> CoreError {
    CoreError::new("value_error", "invalid TrueType glyph outline")
}

pub(super) fn invalid_composite() -> CoreError {
    CoreError::new("value_error", "invalid TrueType composite glyph")
}

pub(super) fn read_exact(data: &[u8], at: u64, n: u64) -> Result<&[u8], CoreError> {
    let start = usize::try_from(at).map_err(|_| invalid_outline())?;
    let len = usize::try_from(n).map_err(|_| invalid_outline())?;
    let end = start.checked_add(len).ok_or_else(invalid_outline)?;
    data.get(start..end).ok_or_else(invalid_outline)
}

pub(super) fn be_u16(data: &[u8], at: u64) -> Result<u16, CoreError> {
    let b = read_exact(data, at, 2)?;
    Ok(u16::from_be_bytes([b[0], b[1]]))
}

pub(super) fn be_i16(data: &[u8], at: u64) -> Result<i16, CoreError> {
    let b = read_exact(data, at, 2)?;
    Ok(i16::from_be_bytes([b[0], b[1]]))
}

pub(super) fn be_u32(data: &[u8], at: u64) -> Result<u32, CoreError> {
    let b = read_exact(data, at, 4)?;
    Ok(u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
}
