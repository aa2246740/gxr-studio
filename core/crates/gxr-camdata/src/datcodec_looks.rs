//! Port of gxr_studio/camdata/datcodec.py:482 _valid_look_row and :492 _parse_looks.

use std::collections::BTreeSet;

use gxr_raw::CoreError;

use crate::crc32::{align4, crc, u16_be, u32_be};
use crate::datcodec::{
    CRC_FIELDS, Dat, ENTRY_BYTES, KINDS, Look, OFFSETS, POOL_LENGTHS, ROW_BYTES, dat_error,
};

/// Port of gxr_studio/camdata/datcodec.py:482 _valid_look_row.
pub fn valid_look_row(row: &[u8], ordinal: usize, flags: u8) -> bool {
    let kind = KINDS[ordinal];
    if kind == 0 {
        let mut values = [0u16; 31];
        for (i, v) in values.iter_mut().enumerate() {
            *v = u16_be(row, 2 * i);
        }
        let max_last = if ordinal == 0 || ordinal == 8 {
            4094
        } else {
            4095
        };
        if values[0] == 0 || values.windows(2).any(|w| w[0] >= w[1]) || values[30] > max_last {
            return false;
        }
    } else if kind == 1 {
        if row.windows(2).any(|w| w[0] > w[1]) {
            return false;
        }
    } else if kind == 3 {
        for i in 0..28 {
            let value = u16_be(row, 2 * i);
            let j = i % 14;
            let limit = if j < 3 { 2047 } else { 1279 };
            if value > 4095 || (j < 9 && (value & 2047) > limit) {
                return false;
            }
            if ((j == 10 || j == 11) && !(120..=136).contains(&value))
                || (j >= 9 && j != 10 && j != 11 && value != 0)
            {
                return false;
            }
        }
    }
    !((ordinal == 6 || ordinal == 7) && (flags & 2) != 0 && row[32] < 128)
}

/// Port of gxr_studio/camdata/datcodec.py:492 _parse_looks.
pub fn parse_looks(
    dat: &Dat,
    raw: &[u8],
    pools: &[(usize, usize, usize); 4],
) -> Result<Vec<Look>, CoreError> {
    let area = &dat.effect_area;
    if area.len() < 32 {
        return Err(dat_error("效果区头、尺寸或 CRC 不合格"));
    }
    let capacity = usize::from(u16_be(area, 8));
    let count = usize::from(u16_be(area, 10));
    let caps = &area[16..20];
    let used = &area[20..24];
    let expected_len: usize = 32
        + 32 * capacity
        + (0..4)
            .map(|k| align4(usize::from(caps[k]) * POOL_LENGTHS[k]))
            .sum::<usize>();
    if &area[..8] != b"GXFX\x00\x01\x00 "
        || area[12..16] != [0, 32, 0, 0]
        || count > capacity
        || capacity > 48
        || (0..4).any(|k| used[k] > caps[k] || caps[k] > 127)
        || area.len() != expected_len
        || u32_be(area, 28) != crc(&area[..28])
        || u32_be(area, 24) != crc(&area[32..])
        || area[32 + 32 * count..32 + 32 * capacity]
            .iter()
            .any(|&b| b != 0)
    {
        return Err(dat_error("效果区头、尺寸或 CRC 不合格"));
    }

    let mut row_starts = [0usize; 4];
    let mut cursor = 32 + 32 * capacity;
    for k in 0..4 {
        row_starts[k] = cursor;
        cursor += align4(usize::from(caps[k]) * POOL_LENGTHS[k]);
    }

    let mut seen: [BTreeSet<usize>; 4] = Default::default();
    let mut out = Vec::with_capacity(count);
    for i in 0..count {
        let record = &area[32 + 32 * i..64 + 32 * i];
        let base = record[0];
        let flags = record[1];
        let mask = record[18];
        let name = record[20..].split(|&b| b == 0).next().unwrap_or(&[]);
        let name_padded_ok = record[20..20 + name.len()] == *name
            && record[20 + name.len()..].iter().all(|&b| b == 0);
        if !matches!(base, 1 | 2 | 3 | 5 | 6 | 7 | 8)
            || !(1..=7).contains(&mask)
            || record[19] != 0
            || u16_be(record, 2) != if flags & 4 != 0 { 0 } else { 2 }
            || name.is_empty()
            || name
                .iter()
                .any(|&c| !(32..=126).contains(&c) || c == 42 || c == 92)
            || !name_padded_ok
        {
            return Err(dat_error("效果条目或名称不合格"));
        }
        for unit in 0..3 {
            if (mask >> unit) & 1 != 0 && dat.entry_of(unit, base)?[..2] != [base, flags] {
                return Err(dat_error("效果与单元基底不一致"));
            }
        }

        let mut entry = [0u8; ENTRY_BYTES];
        entry[..2].copy_from_slice(&record[..2]);
        entry[4..8].copy_from_slice(&record[4..8]);
        entry[445..447].copy_from_slice(&record[2..4]);

        let mut crc_buf = Vec::with_capacity(5 + 62 * 3 + 33 * 5 + 96 + 56);
        crc_buf.extend_from_slice(&record[..4]);
        crc_buf.push(mask);
        for ordinal in 0..10 {
            let kind = KINDS[ordinal];
            let off = OFFSETS[ordinal];
            let size = ROW_BYTES[ordinal];
            let raw_idx = record[8 + ordinal];
            let row = if raw_idx & 128 != 0 {
                let index = usize::from(raw_idx & 127);
                if index >= usize::from(used[kind]) {
                    return Err(dat_error("效果行索引越界"));
                }
                let at = row_starts[kind] + index * size;
                seen[kind].insert(index);
                &area[at..at + size]
            } else {
                let index = usize::from(raw_idx);
                if index >= pools[kind].0 {
                    return Err(dat_error("效果引用的内置行索引越界"));
                }
                let at = pools[kind].2 + index * size;
                &raw[at..at + size]
            };
            if !valid_look_row(row, ordinal, flags) {
                return Err(dat_error("效果行数值不合格"));
            }
            crc_buf.extend_from_slice(row);
            entry[off..off + size].copy_from_slice(row);
        }

        if u32_be(record, 4) != crc(&crc_buf) {
            return Err(dat_error("效果内容 CRC 对不上"));
        }
        for &(off, size, crc_off) in &CRC_FIELDS {
            let sum = crc(&entry[off..off + size]).to_be_bytes();
            entry[crc_off..crc_off + 4].copy_from_slice(&sum);
        }
        let name_str = std::str::from_utf8(name)
            .map_err(|_| dat_error("效果条目或名称不合格"))?
            .to_string();
        out.push(Look {
            name: name_str,
            base,
            unit_mask: mask,
            entry,
        });
    }

    for kind in 0..4 {
        let at = row_starts[kind];
        let n = usize::from(used[kind]);
        let c = usize::from(caps[kind]);
        let size = POOL_LENGTHS[kind];
        let mut distinct = BTreeSet::new();
        for j in 0..n {
            distinct.insert(&area[at + j * size..at + (j + 1) * size]);
        }
        if seen[kind].len() != n
            || distinct.len() != n
            || area[at + n * size..at + align4(c * size)]
                .iter()
                .any(|&b| b != 0)
        {
            return Err(dat_error("效果行引用、去重或填充不合格"));
        }
    }

    Ok(out)
}
