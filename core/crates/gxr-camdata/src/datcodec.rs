//! Port of gxr_studio/camdata/datcodec.py: DAT parsing and validation.

use std::collections::BTreeMap;

use gxr_raw::CoreError;
use gxr_raw::sha256::sha256_hex;

use crate::bounds::{add_u64, fit_count, mul_u64, try_vec_with_capacity};
use crate::crc32::{crc, u16_be, u32_be, zero_crc};
use crate::datcodec_looks::parse_looks;
use crate::datcodec_pack::pack;
pub use crate::datcodec_types::*;
use crate::engine_bindings::check_engine_binding;
use crate::r5;

/// Port of gxr_studio/camdata/datcodec.py:228 peek_generation.
pub fn peek_generation(raw: &[u8]) -> Result<u32, CoreError> {
    if raw.len() < HEADER || &raw[..8] != b"GXRCE001" {
        return Err(dat_error("不是 GXRCE.DAT"));
    }
    Ok(u32_be(raw, 28))
}

/// Port of gxr_studio/camdata/datcodec.py:234 parse.
pub fn parse(raw: &[u8], library: Option<&[u8]>) -> Result<Dat, CoreError> {
    if raw.len() > MAX_DAT_BYTES {
        return Err(dat_error("无法导入相机配方文件。文件过大。"));
    }
    let g = peek_generation(raw)?;
    let prof = profile(g).ok_or_else(|| {
        let supported: Vec<String> = PROFILES.iter().map(|p| p.generation.to_string()).collect();
        unsupported_error(format!(
            "代数 {g} 未登记（支持 [{}]）",
            supported.join(", ")
        ))
    })?;
    if prof.library_generation.is_some() {
        return r5::parse(raw, library);
    }
    if library.is_some() {
        return Err(dat_error(
            "这个代数使用单文件配方，不支持附加 GXRFX.BIN。请只选择 GXRCE.DAT。",
        ));
    }
    let raw_len_u64 = raw.len() as u64;
    if raw.len() > prof.max_file || u64::from(u32_be(raw, 16)) != raw_len_u64 {
        return Err(dat_error("DAT 长度不合格"));
    }
    if u32_be(raw, 20) != zero_crc(&raw[..HEADER], 20) || u32_be(raw, 24) != crc(&raw[HEADER..]) {
        return Err(dat_error("DAT CRC 对不上"));
    }
    let flags = u32_be(raw, 12);
    if raw[8..12] != [0, 1, 1, 0] || flags != 0 {
        return Err(dat_error("头部不是可执行形态（本编解码器不处理占位件）"));
    }
    let bss = u32_be(raw, 116);
    if u64::from(bss) > (MAX_RAM as u64) - raw_len_u64 {
        return Err(dat_error("BSS 超限"));
    }

    let mut cursor_u64 = HEADER as u64;
    let mut sections = [(0usize, 0usize); 7];
    for (i, slot) in sections.iter_mut().enumerate() {
        let off = 128 + 12 * i;
        let kind = u16_be(raw, off);
        let fl = u16_be(raw, off + 2);
        let start_u64 = u64::from(u32_be(raw, off + 4));
        let size_u64 = u64::from(u32_be(raw, off + 8));
        if usize::from(kind) != i + 1
            || fl != 0
            || start_u64 != cursor_u64
            || !start_u64.is_multiple_of(4)
            || !size_u64.is_multiple_of(4)
            || size_u64 > raw_len_u64 - start_u64
        {
            return Err(dat_error("区段布局不合格"));
        }
        let start = fit_count(start_u64, "区段布局不合格")?;
        let size = fit_count(size_u64, "区段布局不合格")?;
        *slot = (start, size);
        cursor_u64 = add_u64(cursor_u64, size_u64, "区段布局不合格")?;
    }
    if cursor_u64 != raw_len_u64
        || sections[2].1 != 192
        || sections[3].1 != 456
        || sections[4].1 != 3168
    {
        return Err(dat_error("固定区段尺寸不合格"));
    }

    let (engine_at, engine_len) = sections[0];
    let entry_u64 = u64::from(u32_be(raw, 112));
    if !(16..=prof.max_engine).contains(&engine_len)
        || entry_u64 < 256
        || entry_u64 > 256 + (engine_len as u64) - 4
        || !entry_u64.is_multiple_of(4)
    {
        return Err(dat_error("引擎入口越界"));
    }
    let entry = entry_u64 as u32;

    let nreloc_u64 = u64::from(u16_be(raw, 120));
    let reloc_bytes = mul_u64(8, nreloc_u64, "重定位尺寸不合格")?;
    if nreloc_u64 > 256 || (sections[1].1 as u64) != reloc_bytes {
        return Err(dat_error("重定位尺寸不合格"));
    }
    let nreloc = fit_count(nreloc_u64, "重定位尺寸不合格")?;
    let mut relocations = try_vec_with_capacity(nreloc, "重定位尺寸不合格")?;
    let mut previous = 0u32;
    let ram_end = add_u64(raw_len_u64, u64::from(bss), "BSS 超限")?;
    for i in 0..nreloc {
        let off = sections[1].0 + 8 * i;
        let patch = u32_be(raw, off);
        let target = u32_be(raw, off + 4);
        let patch_u64 = u64::from(patch);
        let target_u64 = u64::from(target);
        if patch <= previous
            || !patch.is_multiple_of(4)
            || patch_u64 < 256
            || patch_u64 > 256 + (engine_len as u64) - 4
            || target_u64 < 256
            || target_u64 >= ram_end
            || u32_be(raw, fit_count(patch_u64, "重定位或映像绑定不合格")?) != target
        {
            return Err(dat_error("重定位或映像绑定不合格"));
        }
        relocations.push((patch, target));
        previous = patch;
    }

    let (meta_at, meta_len) = sections[6];
    let tail = &raw[meta_at..meta_at + meta_len];
    let want_tail = LEGACY_BYTES + EXT_BYTES + prof.knob_bytes();
    if tail.len() != want_tail {
        return Err(dat_error("区段 7 长度不合格"));
    }
    let legacy = &tail[..LEGACY_BYTES];
    let ext = &tail[LEGACY_BYTES..LEGACY_BYTES + EXT_BYTES];
    let kv2 = if prof.kv2_block {
        let block = &tail[LEGACY_BYTES + EXT_BYTES..];
        if sha256_hex(block) != prof.knob_sha256() {
            return Err(dat_error("kv2 点表与已登记的不同"));
        }
        Some(block.to_vec())
    } else {
        None
    };

    const WANT_LEGACY_HEADER: [u8; 32] = [
        b'G', b'X', b'T', b'M', 0, 2, 0, 32, 0, 8, 0, 3, 0, 16, 0, 28, 0, 0, 0, 32, 0, 0, 0, 64, 0,
        0, 5, 64, 0, 0, 0, 0,
    ];
    if legacy[..32] != WANT_LEGACY_HEADER {
        return Err(dat_error("范围表布局不合格"));
    }
    let mut ids = [0u32; 8];
    for (i, id) in ids.iter_mut().enumerate() {
        *id = u32_be(legacy, 32 + 4 * i);
    }
    if ids != [1, 2, 3, 4, 5, 6, 0, 0] {
        return Err(dat_error("参数 ID 不合格"));
    }
    if &ext[..8] != GXUI_MAGIC
        || u16_be(ext, 8) != 1
        || u16_be(ext, 10) != 64
        || u32_be(ext, 12) as usize != EXT_BYTES
        || u32_be(ext, 16) != zero_crc(ext, 16)
        || usize::from(u16_be(ext, 24)) > USER_CAPACITY
        || ext[20..24] != [0, 24, 0, 48]
        || ext[26..32] != [0, 24, 0, 11, 0, 32]
        || u32_be(ext, 32) != 64
        || u32_be(ext, 36) as usize != USER_OFFSET
        || ext[52..64].iter().any(|&b| b != 0)
    {
        return Err(dat_error("GXUI 头不合格"));
    }

    let (shared, pool_bytes) = sections[5];
    let pool_header = if prof.looks { 80usize } else { 64usize };
    let want_gxdp: [u8; 8] = [
        b'G',
        b'X',
        b'D',
        b'P',
        0,
        if prof.looks { 2 } else { 1 },
        0,
        pool_header as u8,
    ];
    if pool_bytes < pool_header
        || raw[shared..shared + 8] != want_gxdp
        || u64::from(u32_be(raw, shared + 8)) != pool_bytes as u64
    {
        return Err(dat_error("行池头不合格"));
    }

    let mut effect_area = Vec::new();
    let mut pool_end = pool_bytes;
    if prof.looks {
        let pool_end_u64 = u64::from(u32_be(raw, shared + 64));
        let effect_bytes_u64 = u64::from(u32_be(raw, shared + 68));
        let total_u64 = add_u64(pool_end_u64, effect_bytes_u64, "效果区边界不合格")?;
        if !pool_end_u64.is_multiple_of(4)
            || pool_end_u64 < pool_header as u64
            || effect_bytes_u64 < 32
            || total_u64 != pool_bytes as u64
            || raw[shared + 72..shared + 80].iter().any(|&b| b != 0)
        {
            return Err(dat_error("效果区边界不合格"));
        }
        pool_end = fit_count(pool_end_u64, "效果区边界不合格")?;
        effect_area = raw[shared + pool_end..shared + pool_bytes].to_vec();
    }

    let mut pools = [(0usize, 0usize, 0usize); 4];
    for (k, pool_slot) in pools.iter_mut().enumerate() {
        let off = shared + 16 + 12 * k;
        let count_u64 = u64::from(u16_be(raw, off));
        let size_u64 = u64::from(u16_be(raw, off + 2));
        let offset_u64 = u64::from(u32_be(raw, off + 4));
        let checksum = u32_be(raw, off + 8);
        let span_u64 = mul_u64(count_u64, size_u64, "行池边界不合格")?;
        let end_u64 = add_u64(offset_u64, span_u64, "行池边界不合格")?;
        if !(1..=POOL_LIMITS[k] as u64).contains(&count_u64)
            || size_u64 != POOL_LENGTHS[k] as u64
            || offset_u64 < pool_header as u64
            || end_u64 > pool_end as u64
        {
            return Err(dat_error("行池边界不合格"));
        }
        let count = fit_count(count_u64, "行池边界不合格")?;
        let size = fit_count(size_u64, "行池边界不合格")?;
        let offset = fit_count(offset_u64, "行池边界不合格")?;
        let body = &raw[shared + offset..shared + offset + count * size];
        if crc(body) != checksum {
            return Err(dat_error("行池 CRC 对不上"));
        }
        *pool_slot = (count, size, shared + offset);
    }

    let directory = sections[2].0;
    let mut modules = Vec::with_capacity(3);
    for (mi, &name) in NAMES.iter().enumerate() {
        let d = directory + 64 * mi;
        let mut want_name = [0u8; 8];
        want_name[..name.len()].copy_from_slice(name.as_bytes());
        if raw[d..d + 8] != want_name || u16_be(raw, d + 8) != 0x151 || u16_be(raw, d + 10) != 0 {
            return Err(dat_error("目录项名称或版本不合格"));
        }
        let map_at_u64 = u64::from(u32_be(raw, d + 12));
        let bank_at_u64 = u64::from(u32_be(raw, d + 16));
        let table_u64 = u64::from(u32_be(raw, d + 20));
        let meta_ranges_u64 = u64::from(u32_be(raw, d + 24));
        let count_u64 = u64::from(raw[d + 30]);
        if map_at_u64 != (sections[3].0 + 152 * mi) as u64
            || bank_at_u64 != (sections[4].0 + 1056 * mi) as u64
        {
            return Err(dat_error("映射或配方库地址不合格"));
        }
        let map_at = fit_count(map_at_u64, "映射或配方库地址不合格")?;
        let bank_at = fit_count(bank_at_u64, "映射或配方库地址不合格")?;
        let header = &raw[bank_at..bank_at + 32];
        let bank = &raw[bank_at + 32..bank_at + 32 + 1024];
        if &header[..8] != b"GXRRV201"
            || u32_be(header, 16) != prof.bank_generation()
            || u32_be(header, 20) != 1024
            || u32_be(header, 24) != crc(bank)
            || u32_be(header, 28) != crc(&header[..28])
            || header[8..16] != [0, 2, 0, 32, 0, 64, 0, 16]
        {
            return Err(dat_error("配方库头、CRC 或代数不合格"));
        }
        if meta_ranges_u64 != (meta_at + 64 + 16 * 28 * mi) as u64 {
            return Err(dat_error("范围目录不合格"));
        }
        let meta_ranges = fit_count(meta_ranges_u64, "范围目录不合格")?;
        let table_bytes_u64 = add_u64(
            16,
            mul_u64(20, count_u64, "描述符表边界不合格")?,
            "描述符表边界不合格",
        )?;
        let table_end_u64 = add_u64(table_u64, table_bytes_u64, "描述符表边界不合格")?;
        let pool_limit_u64 = (shared + pool_end) as u64;
        if !(1..=16).contains(&count_u64)
            || table_u64 < (shared + pool_header) as u64
            || table_end_u64 > pool_limit_u64
        {
            return Err(dat_error("描述符表边界不合格"));
        }
        let count = fit_count(count_u64, "描述符表边界不合格")?;
        let table = fit_count(table_u64, "描述符表边界不合格")?;
        let want_gxdt = [b'G', b'X', b'D', b'T', 0x06, count as u8];
        if raw[table..table + 6] != want_gxdt
            || u32_be(raw, table + 12) != crc(&raw[table + 16..table + 16 + 20 * count])
        {
            return Err(dat_error("描述符表 CRC 不合格"));
        }
        if usize::from(u16_be(raw, d + 28)) != 16 + 20 * count || raw[d + 31] != 0 {
            return Err(dat_error("描述符表长度不合格"));
        }

        let mut records = Vec::with_capacity(16);
        for slot in 1u8..=16 {
            let rec_slice = &bank[64 * usize::from(slot - 1)..64 * usize::from(slot)];
            let mut rec = [0u8; 64];
            rec.copy_from_slice(rec_slice);
            if rec[0] != slot || u32_be(&rec, 60) != crc(&rec[..60]) {
                return Err(dat_error("配方记录 CRC 不合格"));
            }
            records.push(rec);
        }

        let mut entries = BTreeMap::new();
        let mut owners = try_vec_with_capacity(count, "描述符所有者不合格")?;
        let mut descriptors = try_vec_with_capacity(count, "描述符所有者不合格")?;
        for i in 0..count {
            descriptors.push(&raw[table + 16 + 20 * i..table + 36 + 20 * i]);
        }
        for &desc in &descriptors {
            let slot = desc[0];
            if !(1..=16).contains(&slot) || entries.contains_key(&slot) {
                return Err(dat_error("描述符所有者不合格"));
            }
            if u32_be(desc, 4) != crc(&identity(&records[usize::from(slot - 1)])) {
                return Err(dat_error("描述符与配方身份绑定不合格"));
            }
            if desc[18..20] != [0, 0] {
                return Err(dat_error("描述符保留字节非零"));
            }
            let mut e = [0u8; ENTRY_BYTES];
            e[..2].copy_from_slice(&desc[..2]);
            e[4..8].copy_from_slice(&desc[4..8]);
            e[445..447].copy_from_slice(&desc[2..4]);
            for j in 0..10 {
                let off = OFFSETS[j];
                let size = ROW_BYTES[j];
                let kind = KINDS[j];
                let index = usize::from(desc[8 + j]);
                if index >= pools[kind].0 {
                    return Err(dat_error("描述符行索引越界"));
                }
                let at = pools[kind].2 + index * size;
                e[off..off + size].copy_from_slice(&raw[at..at + size]);
            }
            for &(off, size, crc_off) in &CRC_FIELDS {
                let sum = crc(&e[off..off + size]).to_be_bytes();
                e[crc_off..crc_off + 4].copy_from_slice(&sum);
            }
            entries.insert(slot, e);
            owners.push(slot);
        }

        if descriptors[0][..4] != [0x04, 0, 0, 0x02] {
            return Err(dat_error("第一个描述符不是原厂 04"));
        }
        let stock_entry = entries
            .get(&4)
            .ok_or_else(|| dat_error("第一个描述符不是原厂 04"))?;
        if crc(&stock_entry[..448]) != STOCK_ENTRY_CRC[mi] {
            return Err(dat_error("原厂 04 条目 CRC 与已登记的不同"));
        }
        let mapping = raw[map_at..map_at + 152].to_vec();
        let mut raw_stock = Vec::with_capacity(152 + 552);
        raw_stock.extend_from_slice(&mapping);
        for j in 0..10 {
            raw_stock.extend_from_slice(&stock_entry[OFFSETS[j]..OFFSETS[j] + ROW_BYTES[j]]);
        }
        raw_stock.extend_from_slice(&stock_entry[445..447]);
        let pin = crc(&raw_stock);
        if pin != PIN_CRC32[mi] || u32_be(raw, d + 32) != pin {
            return Err(dat_error("map128 ∥ stock 钉子对不上"));
        }

        let mut refs = [0u8; 16];
        let mut ranges = [[[0i8; 3]; 8]; 16];
        for i in 0..16 {
            let row = &raw[meta_ranges + 28 * i..meta_ranges + 28 * (i + 1)];
            let owner_idx = usize::from(row[24]);
            if owner_idx >= owners.len() {
                return Err(dat_error("范围表引用越界"));
            }
            refs[i] = owners[owner_idx];
            for j in 0..8 {
                ranges[i][j] = [
                    i8::from_be_bytes([row[3 * j]]),
                    i8::from_be_bytes([row[3 * j + 1]]),
                    i8::from_be_bytes([row[3 * j + 2]]),
                ];
            }
        }
        let mut pin_bytes = [0u8; 32];
        pin_bytes.copy_from_slice(&raw[d + 32..d + 64]);
        modules.push(Module {
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

    for (i, m) in modules.iter().enumerate() {
        let mut bank = Vec::with_capacity(1024);
        for rec in &m.records {
            bank.extend_from_slice(rec);
        }
        if u32_be(ext, 40 + 4 * i) != crc(&bank) {
            return Err(dat_error("GXUI 与配方库的绑定不合格"));
        }
    }
    for mi in 0..3 {
        for slot in 1usize..=8 {
            let c = &ext[64 + (mi * 8 + slot - 1) * CONFIG_BYTES..][..CONFIG_BYTES];
            let count = usize::from(c[2]);
            if count > 11 || c[..2] != [mi as u8, slot as u8] || c[3] != 0 {
                return Err(dat_error("GXUI 项目顺序不合格"));
            }
            let mut order: Vec<u8> = c[4..4 + count].to_vec();
            order.sort_unstable();
            let expected: &[u8] = if slot == 3 { &[1, 2, 3] } else { &[1, 2, 3, 4] };
            if order != expected || c[4 + count..15].iter().any(|&b| b != 0) {
                return Err(dat_error("GXUI 项目顺序不合格"));
            }
            for k in 0..11 {
                let domain = (
                    i8::from_be_bytes([c[15 + 3 * k]]),
                    i8::from_be_bytes([c[15 + 3 * k + 1]]),
                    i8::from_be_bytes([c[15 + 3 * k + 2]]),
                );
                if k < 4 {
                    if !(-8..=0).contains(&domain.0)
                        || !(0..=8).contains(&domain.1)
                        || domain.2 != 0
                    {
                        return Err(dat_error("GXUI 范围越出旧算法契约"));
                    }
                    let old_at = 64 + (mi * 16 + slot - 1) * 28 + 3 * (LEGACY_IDS[k] - 1);
                    let old = (
                        i8::from_be_bytes([legacy[old_at]]),
                        i8::from_be_bytes([legacy[old_at + 1]]),
                        i8::from_be_bytes([legacy[old_at + 2]]),
                    );
                    if domain != old {
                        return Err(dat_error("GXUI 与旧范围表不一致"));
                    }
                } else {
                    let want = if k == 10 { (-10, 5, 0) } else { (-9, 9, 0) };
                    if domain != want {
                        return Err(dat_error("GXUI 保留范围不合格"));
                    }
                }
            }
        }
    }

    let user_count = usize::from(u16_be(ext, 24));
    let mut users = try_vec_with_capacity(user_count, "GXUI 头不合格")?;
    for i in 0..user_count {
        let at = USER_OFFSET + i * USER_ENTRY_BYTES;
        let mut levels = [0i8; 11];
        for (j, lv) in levels.iter_mut().enumerate() {
            *lv = i8::from_be_bytes([ext[at + 10 + j]]);
        }
        users.push(UserEntry {
            base: ext[at],
            levels,
        });
    }

    let mut resident_id = [0u8; 32];
    resident_id.copy_from_slice(&raw[80..112]);
    let mut abi_id = [0u8; 16];
    abi_id.copy_from_slice(&raw[212..228]);

    let mut dat = Dat {
        generation: g,
        flags,
        entry,
        bss,
        resident_id,
        abi_id,
        engine: raw[engine_at..engine_at + engine_len].to_vec(),
        relocations,
        modules,
        ids,
        ext_config: ext[64..USER_OFFSET].to_vec(),
        users,
        kv2_block: kv2,
        effect_area,
        looks: Vec::new(),
        user_names: Vec::new(),
        recipes: Vec::new(),
        shell_header: Vec::new(),
    };

    check_engine_binding(
        dat.generation,
        &dat.engine,
        dat.entry,
        dat.bss,
        &dat.relocations,
        raw.len(),
    )?;
    if prof.looks {
        dat.looks = parse_looks(&dat, raw, &pools)?;
    }
    if pack(&dat)? != raw {
        return Err(dat_error("重新封装与原文件不同（存在未建模的字节）"));
    }

    let mut user_names = try_vec_with_capacity(user_count, "GXUI 头不合格")?;
    for i in 0..user_count {
        let at = USER_OFFSET + i * USER_ENTRY_BYTES + 2;
        let slice = ext[at..at + 8].split(|&b| b == 0).next().unwrap_or(&[]);
        let s = std::str::from_utf8(slice)
            .ok()
            .filter(|s| s.is_ascii())
            .ok_or_else(|| dat_error("用户条目名称不是 ASCII"))?;
        user_names.push(s.to_string());
    }
    dat.user_names = user_names;

    Ok(dat)
}

/// Port of gxr_studio/camdata/store.py:47 entries_from_dat.
pub fn entries_from_dat(
    raw: &[u8],
    library: Option<&[u8]>,
) -> Result<(Dat, [[u8; ENTRY_BYTES]; 24]), CoreError> {
    let dat = parse(raw, library)?;
    let mut entries = [[0u8; ENTRY_BYTES]; 24];
    for unit in 0..3 {
        for (slot_idx, &slot) in SLOTS.iter().enumerate() {
            entries[unit * 8 + slot_idx].copy_from_slice(dat.entry_of(unit, slot)?);
        }
    }
    Ok((dat, entries))
}
