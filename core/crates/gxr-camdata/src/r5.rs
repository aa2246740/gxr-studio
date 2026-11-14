//! Port of gxr_studio/camdata/r5.py: dual-file read-only codec (GXRCE.DAT + GXRFX.BIN).

use std::collections::BTreeMap;

use gxr_raw::CoreError;
use gxr_raw::sha256::sha256_hex;

use crate::bounds::{add_u64, fit_count, mul_u64, try_vec_with_capacity};
use crate::crc32::{crc, u16_be, u32_be, zero_crc};
use crate::datcodec::{
    CRC_FIELDS, Dat, ENTRY_BYTES, FullRecipe, HEADER, KINDS, KV3_BLOCK_BYTES, KV3_BLOCK_SHA256,
    MAX_RAM, Module, NAMES, OFFSETS, PIN_CRC32, POOL_LENGTHS, ROW_BYTES, STOCK_ENTRY_CRC,
    dat_error, peek_generation, profile, unsupported_error,
};
use crate::engine_bindings::check_engine_binding;

pub const LIBRARY_NAME: &str = "GXRFX.BIN";
pub const MAX_LIBRARY_BYTES: usize = 65_528;

pub fn library_sha256(library_generation: u32) -> Option<&'static str> {
    match library_generation {
        176 => Some("e81659225e74cc5a59823e921a25092bd02b068f98396ecbd111921a67821c48"),
        178 => Some("0af19a0cca12e58d96fbc783491624696dc477db0f6aade0b220df7821b07b50"),
        _ => None,
    }
}

/// Port of gxr_studio/camdata/r5.py:18 _sections.
fn sections(raw: &[u8]) -> Result<[(usize, usize); 7], CoreError> {
    let raw_len_u64 = raw.len() as u64;
    let mut cursor_u64 = HEADER as u64;
    let mut out = [(0usize, 0usize); 7];
    for (i, slot) in out.iter_mut().enumerate() {
        let off = 128 + 12 * i;
        let kind = u16_be(raw, off);
        let flags = u16_be(raw, off + 2);
        let start_u64 = u64::from(u32_be(raw, off + 4));
        let size_u64 = u64::from(u32_be(raw, off + 8));
        if usize::from(kind) != i + 1
            || flags != 0
            || start_u64 != cursor_u64
            || !start_u64.is_multiple_of(4)
            || !size_u64.is_multiple_of(4)
            || size_u64 > raw_len_u64 - start_u64
        {
            return Err(dat_error("r5 区段布局不合格"));
        }
        *slot = (
            fit_count(start_u64, "r5 区段布局不合格")?,
            fit_count(size_u64, "r5 区段布局不合格")?,
        );
        cursor_u64 = add_u64(cursor_u64, size_u64, "r5 区段布局不合格")?;
    }
    if cursor_u64 != raw_len_u64 {
        return Err(dat_error("r5 区段长度不合格"));
    }
    Ok(out)
}

/// Port of gxr_studio/camdata/r5.py:31 _check_envelope.
fn check_envelope(raw: &[u8], magic: &[u8; 8], flags: u32) -> Result<(), CoreError> {
    if raw.len() < HEADER
        || &raw[..8] != magic
        || raw[8..12] != [0, 1, 1, 0]
        || u32_be(raw, 12) != flags
    {
        return Err(dat_error("r5 文件头不合格"));
    }
    if u64::from(u32_be(raw, 16)) != raw.len() as u64
        || u32_be(raw, 20) != zero_crc(&raw[..256], 20)
        || u32_be(raw, 24) != crc(&raw[256..])
    {
        return Err(dat_error("r5 文件长度或 CRC 对不上"));
    }
    Ok(())
}

/// Port of gxr_studio/camdata/r5.py:39 _catalogue.
fn catalogue(library: Option<&[u8]>, generation: u32) -> Result<&[u8], CoreError> {
    let prof = profile(generation)
        .filter(|p| p.library_generation.is_some())
        .ok_or_else(|| unsupported_error(format!("代数 {generation} 没有登记 r5 库")))?;
    let lib_gen = prof.library_generation.expect("checked above");
    let lib = library.ok_or_else(|| {
        dat_error(
            "缺少 GXRFX.BIN。请将 GXRCE.DAT 和 GXRFX.BIN 一起从存储卡复制到同一文件夹，再同时导入。",
        )
    })?;
    if lib.len() > MAX_LIBRARY_BYTES {
        return Err(dat_error("无法导入相机配方文件。文件过大。"));
    }
    let expected_sha = library_sha256(lib_gen)
        .ok_or_else(|| unsupported_error(format!("代数 {generation} 没有登记 r5 库")))?;
    if sha256_hex(lib) != expected_sha {
        return Err(dat_error(format!(
            "GXRFX.BIN 与第 {generation} 代 GXRCE.DAT 不匹配或已损坏，请重新复制同一套两个文件。"
        )));
    }
    if lib.len() < 64 {
        return Err(dat_error("GXRFX.BIN 库头或 CRC 不合格"));
    }
    let cat = &lib[64..];
    if &lib[..12] != b"GXRLB003\x00\x03\x00@"
        || u64::from(u32_be(lib, 12)) != lib.len() as u64
        || u64::from(u32_be(lib, 16)) != cat.len() as u64
        || u32_be(lib, 20) != crc(cat)
        || u32_be(lib, 24) != 7
        || u32_be(lib, 28) != 3
        || u32_be(lib, 32) != zero_crc(&lib[..64], 32)
        || u32_be(lib, 36) != lib_gen
        || u64::from(u32_be(lib, 40)) != cat.len() as u64
        || lib[44..64].iter().any(|&b| b != 0)
    {
        return Err(dat_error("GXRFX.BIN 库头或 CRC 不合格"));
    }
    check_envelope(cat, b"GXRCL002", 2)?;
    Ok(cat)
}

/// Port of gxr_studio/camdata/r5.py:64 _pools.
fn pools(shared: &[u8]) -> Result<[(usize, usize, usize); 4], CoreError> {
    if shared.len() < 68 {
        return Err(dat_error("r5 行池头不合格"));
    }
    let mut out = [(0usize, 0usize, 0usize); 4];
    for (kind, slot) in out.iter_mut().enumerate() {
        let off = 16 + 12 * kind;
        *slot = (
            usize::from(u16_be(shared, off)),
            usize::from(u16_be(shared, off + 2)),
            fit_count(u64::from(u32_be(shared, off + 4)), "r5 行池偏移越界")?,
        );
    }
    Ok(out)
}

/// Port of gxr_studio/camdata/r5.py:68 _entry.
pub fn build_entry(
    desc: &[u8],
    shared: &[u8],
    pool_info: &[(usize, usize, usize); 4],
    wide: bool,
) -> Result<[u8; ENTRY_BYTES], CoreError> {
    let min_desc = if wide { 28 } else { 18 };
    if desc.len() < min_desc {
        return Err(dat_error("r5 描述符尺寸不合格"));
    }
    let mut entry = [0u8; ENTRY_BYTES];
    entry[..2].copy_from_slice(&desc[..2]);
    entry[4..8].copy_from_slice(&desc[4..8]);
    entry[445..447].copy_from_slice(&desc[2..4]);

    let extra: &[u8] = if wide {
        let extra_off = fit_count(u64::from(u32_be(shared, 64)), "r5 扩展区偏移越界")?;
        shared
            .get(extra_off..)
            .ok_or_else(|| dat_error("r5 扩展区偏移越界"))?
    } else {
        &[]
    };
    let mut extra_at_u64 = if wide {
        if extra.len() < 48 {
            return Err(dat_error("r5 扩展区头不合格"));
        }
        let desc_bytes = mul_u64(44, u64::from(u16_be(extra, 8)), "r5 扩展区头不合格")?;
        add_u64(48, desc_bytes, "r5 扩展区头不合格")?
    } else {
        0
    };
    let mut starts = [0u64; 4];
    for (kind, &size) in POOL_LENGTHS.iter().enumerate() {
        starts[kind] = extra_at_u64;
        if wide {
            let count = u64::from(u16_be(extra, 16 + 2 * kind));
            let raw_bytes = mul_u64(count, size as u64, "r5 扩展区头不合格")?;
            let padded = add_u64(raw_bytes, 3, "r5 扩展区头不合格")? & !3u64;
            extra_at_u64 = add_u64(extra_at_u64, padded, "r5 扩展区头不合格")?;
        }
    }

    for i in 0..10 {
        let kind = KINDS[i];
        let off = OFFSETS[i];
        let size = ROW_BYTES[i];
        let raw_idx = if wide {
            usize::from(u16_be(desc, 8 + 2 * i))
        } else {
            usize::from(desc[8 + i])
        };
        let row = if wide && (raw_idx & 32768) != 0 {
            let index = u64::from((raw_idx & 32767) as u16);
            if index >= u64::from(u16_be(extra, 24 + 2 * kind)) {
                return Err(dat_error("完整配方行索引越界"));
            }
            let row_off = mul_u64(index, size as u64, "完整配方行索引越界")?;
            let start_u64 = add_u64(starts[kind], row_off, "完整配方行索引越界")?;
            let end_u64 = add_u64(start_u64, size as u64, "完整配方行索引越界")?;
            let start = fit_count(start_u64, "完整配方行索引越界")?;
            let end = fit_count(end_u64, "完整配方行索引越界")?;
            extra
                .get(start..end)
                .ok_or_else(|| dat_error("完整配方行索引越界"))?
        } else {
            let (count, _, at) = pool_info[kind];
            if raw_idx >= count {
                return Err(dat_error("原生配方行索引越界"));
            }
            let row_off = mul_u64(raw_idx as u64, size as u64, "原生配方行索引越界")?;
            let start_u64 = add_u64(at as u64, row_off, "原生配方行索引越界")?;
            let end_u64 = add_u64(start_u64, size as u64, "原生配方行索引越界")?;
            let start = fit_count(start_u64, "原生配方行索引越界")?;
            let end = fit_count(end_u64, "原生配方行索引越界")?;
            shared
                .get(start..end)
                .ok_or_else(|| dat_error("原生配方行索引越界"))?
        };
        entry[off..off + size].copy_from_slice(row);
    }

    for &(off, size, field) in &CRC_FIELDS {
        let sum = crc(&entry[off..off + size]).to_be_bytes();
        entry[field..field + 4].copy_from_slice(&sum);
    }
    Ok(entry)
}

/// Port of gxr_studio/camdata/r5.py:92 _modules.
fn modules(
    cat: &[u8],
    cat_sections: &[(usize, usize); 7],
    shared: &[u8],
) -> Result<Vec<Module>, CoreError> {
    let mut out = Vec::with_capacity(3);
    let pool_info = pools(shared)?;
    for (unit, &name) in NAMES.iter().enumerate() {
        let directory = cat_sections[2].0 + 64 * unit;
        if directory + 64 > cat.len() {
            return Err(dat_error("r5 目录越界"));
        }
        let map_at = fit_count(u64::from(u32_be(cat, directory + 12)), "r5 映射越界")?;
        let bank_at = fit_count(u64::from(u32_be(cat, directory + 16)), "r5 配方库越界")?;
        let table = fit_count(u64::from(u32_be(cat, directory + 20)), "r5 描述符表越界")?;
        let ranges_at = fit_count(u64::from(u32_be(cat, directory + 24)), "r5 范围表越界")?;
        let count_u64 = u64::from(cat[directory + 30]);
        let count = fit_count(count_u64, "r5 描述符表越界")?;

        let bank_end = add_u64(bank_at as u64, 32 + 1024, "r5 配方库越界")?;
        let bank_body = cat
            .get(bank_at + 32..fit_count(bank_end, "r5 配方库越界")?)
            .ok_or_else(|| dat_error("r5 配方库越界"))?;
        let mut records = Vec::with_capacity(16);
        for i in 0..16 {
            let mut rec = [0u8; 64];
            rec.copy_from_slice(&bank_body[64 * i..64 * (i + 1)]);
            records.push(rec);
        }

        let desc_bytes = mul_u64(20, count_u64, "r5 描述符表越界")?;
        let table_start = add_u64(table as u64, 16, "r5 描述符表越界")?;
        let table_end = add_u64(table_start, desc_bytes, "r5 描述符表越界")?;
        let desc_table = cat
            .get(
                fit_count(table_start, "r5 描述符表越界")?
                    ..fit_count(table_end, "r5 描述符表越界")?,
            )
            .ok_or_else(|| dat_error("r5 描述符表越界"))?;
        let mut entries = BTreeMap::new();
        let mut owners = try_vec_with_capacity(count, "r5 描述符表越界")?;
        for i in 0..count {
            let d = &desc_table[20 * i..20 * (i + 1)];
            entries.insert(d[0], build_entry(d, shared, &pool_info, false)?);
            owners.push(d[0]);
        }

        let range_end = add_u64(ranges_at as u64, 28 * 16, "r5 范围表越界")?;
        let range_slice = cat
            .get(ranges_at..fit_count(range_end, "r5 范围表越界")?)
            .ok_or_else(|| dat_error("r5 范围表越界"))?;
        let mut refs = [0u8; 16];
        let mut ranges = [[[0i8; 3]; 8]; 16];
        for i in 0..16 {
            let row = &range_slice[28 * i..28 * (i + 1)];
            let owner_idx = usize::from(row[24]);
            refs[i] = *owners
                .get(owner_idx)
                .ok_or_else(|| dat_error("r5 范围表引用越界"))?;
            for k in 0..8 {
                ranges[i][k] = [
                    i8::from_be_bytes([row[3 * k]]),
                    i8::from_be_bytes([row[3 * k + 1]]),
                    i8::from_be_bytes([row[3 * k + 2]]),
                ];
            }
        }

        let map_end = add_u64(map_at as u64, 152, "r5 映射越界")?;
        let mapping = cat
            .get(map_at..fit_count(map_end, "r5 映射越界")?)
            .ok_or_else(|| dat_error("r5 映射越界"))?
            .to_vec();
        let stock = entries
            .get(&4)
            .ok_or_else(|| dat_error("原厂 04 或 map128 钉子不合格"))?;
        let mut raw_stock = Vec::with_capacity(152 + 552);
        raw_stock.extend_from_slice(&mapping);
        for j in 0..10 {
            raw_stock.extend_from_slice(&stock[OFFSETS[j]..OFFSETS[j] + ROW_BYTES[j]]);
        }
        raw_stock.extend_from_slice(&stock[445..447]);
        let pin = crc(&raw_stock);
        if pin != PIN_CRC32[unit] || crc(&stock[..448]) != STOCK_ENTRY_CRC[unit] {
            return Err(dat_error("原厂 04 或 map128 钉子不合格"));
        }
        let mut pin_bytes = [0u8; 32];
        pin_bytes.copy_from_slice(&cat[directory + 32..directory + 64]);
        out.push(Module {
            name,
            map: mapping,
            records,
            entries,
            refs,
            ranges,
            pin: pin_bytes,
            crc_pin: pin,
        });
    }
    Ok(out)
}

/// Port of gxr_studio/camdata/r5.py:121 parse.
pub fn parse(raw: &[u8], library: Option<&[u8]>) -> Result<Dat, CoreError> {
    check_envelope(raw, b"GXRCE001", 0)?;
    let secs = sections(raw)?;
    let generation = peek_generation(raw)?;
    let (engine_at, engine_size) = secs[0];
    let (reloc_at, reloc_size) = secs[1];
    if raw.len() > 98304
        || engine_size > 81920
        || reloc_size != 8
        || secs[2..].iter().any(|&(_, n)| n != 0)
    {
        return Err(dat_error("r5 执行外壳尺寸不合格"));
    }

    let relocations = vec![(u32_be(raw, reloc_at), u32_be(raw, reloc_at + 4))];
    let mut resident_id = [0u8; 32];
    resident_id.copy_from_slice(&raw[80..112]);
    let mut abi_id = [0u8; 16];
    abi_id.copy_from_slice(&raw[212..228]);

    let mut dat = Dat {
        generation,
        flags: 0,
        entry: u32_be(raw, 112),
        bss: u32_be(raw, 116),
        resident_id,
        abi_id,
        engine: raw[engine_at..engine_at + engine_size].to_vec(),
        relocations,
        modules: Vec::new(),
        ids: [0; 8],
        ext_config: Vec::new(),
        users: Vec::new(),
        kv2_block: None,
        effect_area: Vec::new(),
        looks: Vec::new(),
        user_names: Vec::new(),
        recipes: Vec::new(),
        shell_header: raw[..256].to_vec(),
    };

    check_engine_binding(
        dat.generation,
        &dat.engine,
        dat.entry,
        dat.bss,
        &dat.relocations,
        raw.len(),
    )?;
    let ram_u64 = add_u64(
        raw.len() as u64,
        u64::from(dat.bss),
        "r5 BSS 或重定位不合格",
    )?;
    if ram_u64 > MAX_RAM as u64 || u16_be(raw, 120) != 1 {
        return Err(dat_error("r5 BSS 或重定位不合格"));
    }
    for &(patch, target) in &dat.relocations {
        let p_u64 = u64::from(patch);
        let p_end = add_u64(p_u64, 4, "r5 重定位与引擎不匹配")?;
        if p_end > raw.len() as u64
            || u32_be(raw, fit_count(p_u64, "r5 重定位与引擎不匹配")?) != target
        {
            return Err(dat_error("r5 重定位与引擎不匹配"));
        }
    }

    let cat = catalogue(library, generation)?;
    let cat_sections = sections(cat)?;
    let (shared_at, shared_size) = cat_sections[5];
    let shared = &cat[shared_at..shared_at + shared_size];
    dat.modules = modules(cat, &cat_sections, shared)?;
    if cat.len() < KV3_BLOCK_BYTES {
        return Err(dat_error("r5 旋钮表未登记"));
    }
    let kv = &cat[cat.len() - KV3_BLOCK_BYTES..];
    if sha256_hex(kv) != KV3_BLOCK_SHA256 {
        return Err(dat_error("r5 旋钮表未登记"));
    }
    dat.kv2_block = Some(kv.to_vec());

    let pool_info = pools(shared)?;
    let area_off = fit_count(u64::from(u32_be(shared, 64)), "r5 完整配方区越界")?;
    let area = shared
        .get(area_off..)
        .ok_or_else(|| dat_error("r5 完整配方区越界"))?;
    if area.len() < 12 {
        return Err(dat_error("r5 完整配方区头不合格"));
    }
    let count_u64 = u64::from(u16_be(area, 10));
    let area_need = add_u64(
        48,
        mul_u64(44, count_u64, "r5 完整配方描述越界")?,
        "r5 完整配方描述越界",
    )?;
    if area_need > area.len() as u64 {
        return Err(dat_error("r5 完整配方描述越界"));
    }
    let count = fit_count(count_u64, "r5 完整配方描述越界")?;
    let mut recipes = try_vec_with_capacity(count, "r5 完整配方描述越界")?;
    for i in 0..count {
        let desc = &area[48 + 44 * i..92 + 44 * i];
        let name_bytes = desc[32..44].split(|&b| b == 0).next().unwrap_or(&[]);
        let name = std::str::from_utf8(name_bytes)
            .ok()
            .filter(|s| s.is_ascii())
            .ok_or_else(|| dat_error("r5 配方名称不是 ASCII"))?
            .to_string();
        let entry = build_entry(desc, shared, &pool_info, true)?;
        recipes.push(FullRecipe {
            name,
            base: desc[0],
            unit_mask: desc[28],
            entry,
        });
    }
    dat.recipes = recipes;
    Ok(dat)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_huge_r5_library_and_wide_entry_counts() {
        let mut desc = [0u8; 44];
        desc[8] = 0x80;
        let shared = vec![0u8; 80];
        let pool_info = [(0, 62, 0), (0, 33, 0), (0, 96, 0), (0, 56, 0)];
        assert!(build_entry(&desc, &shared, &pool_info, true).is_err());
        assert!(catalogue(Some(&vec![0u8; MAX_LIBRARY_BYTES + 1]), 178).is_err());
    }
}
