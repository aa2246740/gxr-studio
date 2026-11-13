//! Port of gxr_studio/camdata/datcodec.py:562 pack and helpers (single-file test oracle).

use std::collections::{BTreeMap, BTreeSet};

use gxr_raw::CoreError;

use crate::crc32::{align4, crc, hex32, u16_be, u32_be, zero_crc};
use crate::datcodec::{
    CONFIG_BYTES, Dat, ENTRY_BYTES, EXT_BYTES, GXUI_MAGIC, HEADER, ILAUNCH3_SHA256, KINDS, MAX_RAM,
    Module, NAMES, OFFSETS, POOL_LENGTHS, Profile, ROW_BYTES, USER_CAPACITY, USER_ENTRY_BYTES,
    USER_OFFSET, UserEntry, dat_error, identity, profile, unsupported_error,
};
use crate::engine_bindings::check_engine_binding;

/// Port of gxr_studio/camdata/datcodec.py:592 _owners.
fn owners_of(m: &Module) -> Vec<u8> {
    let mut out = Vec::with_capacity(m.entries.len());
    out.push(4);
    for &s in m.entries.keys() {
        if s != 4 {
            out.push(s);
        }
    }
    out
}

/// Port of gxr_studio/camdata/datcodec.py:562 _metadata.
fn metadata(modules: &[Module], ids: &[u32; 8]) -> Result<Vec<u8>, CoreError> {
    let n = ids.len();
    let stride = align4(3 * n + 4);
    let mut out = vec![0u8; 32 + 4 * n + 48 * stride];
    out[0..4].copy_from_slice(b"GXTM");
    out[4..6].copy_from_slice(&2u16.to_be_bytes());
    out[6..8].copy_from_slice(&32u16.to_be_bytes());
    out[8..10].copy_from_slice(&(n as u16).to_be_bytes());
    out[10..12].copy_from_slice(&3u16.to_be_bytes());
    out[12..14].copy_from_slice(&16u16.to_be_bytes());
    out[14..16].copy_from_slice(&(stride as u16).to_be_bytes());
    out[16..20].copy_from_slice(&32u32.to_be_bytes());
    out[20..24].copy_from_slice(&((32 + 4 * n) as u32).to_be_bytes());
    out[24..28].copy_from_slice(&((48 * stride) as u32).to_be_bytes());
    out[28..32].copy_from_slice(&0u32.to_be_bytes());
    for (k, &pid) in ids.iter().enumerate() {
        out[32 + 4 * k..36 + 4 * k].copy_from_slice(&pid.to_be_bytes());
    }
    for (mi, m) in modules.iter().enumerate() {
        let owners = owners_of(m);
        let mut index = BTreeMap::new();
        for (i, &s) in owners.iter().enumerate() {
            index.insert(s, i as u8);
        }
        for s in 0..16 {
            let off = 32 + 4 * n + (mi * 16 + s) * stride;
            if m.records[s][1] != 0 {
                for (k, &[lo, hi, default]) in m.ranges[s].iter().enumerate() {
                    if !(lo <= default && default <= hi)
                        || (ids[k] == 0 && (lo != 0 || hi != 0 || default != 0))
                    {
                        return Err(dat_error("范围表不合格"));
                    }
                    out[off + 3 * k] = lo as u8;
                    out[off + 3 * k + 1] = hi as u8;
                    out[off + 3 * k + 2] = default as u8;
                }
                let owner = m.refs[s];
                let &owner_idx = index
                    .get(&owner)
                    .ok_or_else(|| dat_error("范围表引用了不存在的所有者"))?;
                out[off + 3 * n] = owner_idx;
                out[off + 3 * n + 1] = if usize::from(owner) == s + 1 && owner != 4 {
                    1
                } else {
                    3
                };
            } else {
                out[off + 3 * n] = 255;
            }
            out[off + 3 * n + 2] = 2;
        }
    }
    Ok(out)
}

/// Port of gxr_studio/camdata/datcodec.py:596 _extension.
fn extension(
    configs: &[u8],
    banks: &[Vec<u8>; 3],
    users: &[UserEntry],
) -> Result<Vec<u8>, CoreError> {
    if configs.len() != 24 * CONFIG_BYTES || users.len() > USER_CAPACITY {
        return Err(dat_error("GXUI 配置尺寸或用户条目数不合格"));
    }
    let mut out = vec![0u8; EXT_BYTES];
    out[..8].copy_from_slice(GXUI_MAGIC);
    out[8..10].copy_from_slice(&1u16.to_be_bytes());
    out[10..12].copy_from_slice(&64u16.to_be_bytes());
    out[12..16].copy_from_slice(&(EXT_BYTES as u32).to_be_bytes());
    out[16..20].copy_from_slice(&0u32.to_be_bytes());
    out[20..22].copy_from_slice(&24u16.to_be_bytes());
    out[22..24].copy_from_slice(&(CONFIG_BYTES as u16).to_be_bytes());
    out[24..26].copy_from_slice(&(users.len() as u16).to_be_bytes());
    out[26..28].copy_from_slice(&(USER_ENTRY_BYTES as u16).to_be_bytes());
    out[28..30].copy_from_slice(&11u16.to_be_bytes());
    out[30..32].copy_from_slice(&(USER_CAPACITY as u16).to_be_bytes());
    out[32..36].copy_from_slice(&64u32.to_be_bytes());
    out[36..40].copy_from_slice(&(USER_OFFSET as u32).to_be_bytes());
    for (i, bank) in banks.iter().enumerate() {
        out[40 + 4 * i..44 + 4 * i].copy_from_slice(&crc(bank).to_be_bytes());
    }
    out[64..USER_OFFSET].copy_from_slice(configs);

    const KEYS: [&str; 11] = [
        "Cont", "High", "Shad", "Satu", "Fade", "Warm", "Tint", "Splt", "Filt", "Tone", "Amt",
    ];
    const GROUPS: [&str; 8] = ["GRP", "GRN", "HCB", "STD", "FCC", "FCN", "LCH", "LVI"];
    let mut ordinals = [0u16; 8];
    for (i, item) in users.iter().enumerate() {
        let slot = item.base;
        if !(1..=8).contains(&slot) {
            return Err(dat_error("用户条目：base 必须是 1..8，levels 必须有 11 项"));
        }
        for mi in 0..3 {
            let c = &configs[(mi * 8 + usize::from(slot) - 1) * CONFIG_BYTES..][..CONFIG_BYTES];
            let count = usize::from(c[2]).min(CONFIG_BYTES - 4);
            let visible: BTreeSet<u8> = c[4..4 + count].iter().copied().collect();
            for (k, &value) in item.levels.iter().enumerate() {
                let lo = c[15 + 3 * k] as i8;
                let hi = c[15 + 3 * k + 1] as i8;
                let default = c[15 + 3 * k + 2] as i8;
                if !(lo..=hi).contains(&value)
                    || (!visible.contains(&((k + 1) as u8)) && value != default)
                {
                    return Err(dat_error(format!(
                        "用户条目：{}={value} 超出内置配方 {slot} 的有效范围",
                        KEYS[k]
                    )));
                }
            }
        }
        ordinals[usize::from(slot - 1)] += 1;
        let ordinal = ordinals[usize::from(slot - 1)];
        let mark = if item.levels.iter().any(|&v| v != 0) {
            '*'
        } else {
            '-'
        };
        let name = format!("{}{mark}{ordinal:03}", GROUPS[usize::from(slot - 1)]);
        let at = USER_OFFSET + i * USER_ENTRY_BYTES;
        out[at] = slot;
        let name_bytes = name.as_bytes();
        out[at + 2..at + 2 + name_bytes.len()].copy_from_slice(name_bytes);
        out[at + 2 + name_bytes.len()] = 0;
        for (j, &lv) in item.levels.iter().enumerate() {
            out[at + 10 + j] = lv as u8;
        }
        out[at + 22..at + 24].copy_from_slice(&ordinal.to_be_bytes());
    }
    let ext_crc = zero_crc(&out, 16);
    out[16..20].copy_from_slice(&ext_crc.to_be_bytes());
    Ok(out)
}

/// Port of gxr_studio/camdata/datcodec.py:634 _bank_blob.
fn bank_blob(records: &[[u8; 64]], build: u32) -> Vec<u8> {
    let mut out = Vec::with_capacity(32 + records.len() * 64);
    out.extend_from_slice(b"GXRRV201");
    out.extend_from_slice(&2u16.to_be_bytes());
    out.extend_from_slice(&32u16.to_be_bytes());
    out.extend_from_slice(&64u16.to_be_bytes());
    out.extend_from_slice(&16u16.to_be_bytes());
    out.extend_from_slice(&build.to_be_bytes());
    out.extend_from_slice(&1024u32.to_be_bytes());
    let mut body = Vec::with_capacity(1024);
    for rec in records {
        body.extend_from_slice(rec);
    }
    out.extend_from_slice(&crc(&body).to_be_bytes());
    let hdr_crc = crc(&out[..28]);
    out.extend_from_slice(&hdr_crc.to_be_bytes());
    out.extend_from_slice(&body);
    out
}

/// Port of gxr_studio/camdata/datcodec.py:642 _check_list_capacity.
fn check_list_capacity(dat: &Dat, prof: &Profile) -> Result<(), CoreError> {
    if prof.flat_list {
        for unit in 0..3 {
            let look_count = dat
                .looks
                .iter()
                .filter(|look| (look.unit_mask >> unit) & 1 != 0)
                .count();
            if dat.users.len() + look_count > 40 {
                return Err(dat_error("Looks 与用户条目总数超限"));
            }
        }
    }
    Ok(())
}

/// Port of gxr_studio/camdata/datcodec.py:647 pack (single-file generations only).
pub fn pack(dat: &Dat) -> Result<Vec<u8>, CoreError> {
    let prof = profile(dat.generation)
        .ok_or_else(|| unsupported_error(format!("代数 {} 未登记", dat.generation)))?;
    if prof.library_generation.is_some() {
        return Err(dat_error("r5 配方库只读，不支持改写已解码的数据"));
    }
    check_list_capacity(dat, prof)?;
    if dat.modules.len() != 3
        || dat.modules[0].name != NAMES[0]
        || dat.modules[1].name != NAMES[1]
        || dat.modules[2].name != NAMES[2]
    {
        return Err(dat_error("模块顺序必须是 jlaunch, qlaunch, l08firm"));
    }

    let mut buckets: [Vec<Vec<u8>>; 4] = Default::default();
    let mut indexes: [BTreeMap<Vec<u8>, u8>; 4] = Default::default();
    let mut tables = Vec::with_capacity(3);
    for m in &dat.modules {
        let owners = owners_of(m);
        if !(1..=16).contains(&owners.len()) {
            return Err(dat_error("描述符数量不合格"));
        }
        let mut body = Vec::with_capacity(owners.len() * 20);
        for &s in &owners {
            let e = m
                .entries
                .get(&s)
                .ok_or_else(|| dat_error("条目长度、所有者或身份对不上"))?;
            if e.len() != ENTRY_BYTES
                || e[0] != s
                || u32_be(e, 4) != crc(&identity(&m.records[usize::from(s - 1)]))
            {
                return Err(dat_error("条目长度、所有者或身份对不上"));
            }
            let mut refs = [0u8; 10];
            for j in 0..10 {
                let kind = KINDS[j];
                let off = OFFSETS[j];
                let size = ROW_BYTES[j];
                let row = e[off..off + size].to_vec();
                let idx = if let Some(&existing) = indexes[kind].get(&row) {
                    existing
                } else {
                    let next =
                        u8::try_from(buckets[kind].len()).map_err(|_| dat_error("行池数量超限"))?;
                    indexes[kind].insert(row.clone(), next);
                    buckets[kind].push(row);
                    next
                };
                refs[j] = idx;
            }
            body.push(s);
            body.push(e[1]);
            body.extend_from_slice(&e[445..447]);
            body.extend_from_slice(&e[4..8]);
            body.extend_from_slice(&refs);
            body.extend_from_slice(&[0, 0]);
        }
        let mut table = Vec::with_capacity(16 + body.len());
        table.extend_from_slice(b"GXDT");
        table.push(6);
        table.push(owners.len() as u8);
        table.extend_from_slice(&0u16.to_be_bytes());
        table.extend_from_slice(&20u16.to_be_bytes());
        table.extend_from_slice(&0u16.to_be_bytes());
        table.extend_from_slice(&crc(&body).to_be_bytes());
        table.extend_from_slice(&body);
        tables.push(table);
    }

    let pool_header = if prof.looks { 80usize } else { 64usize };
    let mut shared = vec![0u8; pool_header];
    for t in &tables {
        shared.extend_from_slice(t);
    }
    for (kind, rows) in buckets.iter().enumerate() {
        let mut raw_pool = Vec::new();
        for row in rows {
            raw_pool.extend_from_slice(row);
        }
        let off = 16 + 12 * kind;
        let offset_val = (shared.len() as u32).to_be_bytes();
        shared[off..off + 2].copy_from_slice(&(rows.len() as u16).to_be_bytes());
        shared[off + 2..off + 4].copy_from_slice(&(POOL_LENGTHS[kind] as u16).to_be_bytes());
        shared[off + 4..off + 8].copy_from_slice(&offset_val);
        shared[off + 8..off + 12].copy_from_slice(&crc(&raw_pool).to_be_bytes());
        let pad = align4(raw_pool.len()) - raw_pool.len();
        shared.extend_from_slice(&raw_pool);
        shared.resize(shared.len() + pad, 0);
    }
    if prof.looks {
        let eff_off = (shared.len() as u32).to_be_bytes();
        let eff_len = (dat.effect_area.len() as u32).to_be_bytes();
        shared[64..68].copy_from_slice(&eff_off);
        shared[68..72].copy_from_slice(&eff_len);
        shared.extend_from_slice(&dat.effect_area);
    }
    let total_shared = (shared.len() as u32).to_be_bytes();
    shared[0..4].copy_from_slice(b"GXDP");
    shared[4..6].copy_from_slice(&(if prof.looks { 2u16 } else { 1u16 }).to_be_bytes());
    shared[6..8].copy_from_slice(&(pool_header as u16).to_be_bytes());
    shared[8..12].copy_from_slice(&total_shared);
    shared[12..14].copy_from_slice(&16u16.to_be_bytes());
    shared[14..16].copy_from_slice(&4u16.to_be_bytes());

    let banks: [Vec<u8>; 3] = std::array::from_fn(|i| {
        let mut b = Vec::with_capacity(1024);
        for rec in &dat.modules[i].records {
            b.extend_from_slice(rec);
        }
        b
    });
    let mut tweaks = metadata(&dat.modules, &dat.ids)?;
    tweaks.extend_from_slice(&extension(&dat.ext_config, &banks, &dat.users)?);
    if prof.kv2_block {
        let kv2 = dat
            .kv2_block
            .as_deref()
            .ok_or_else(|| dat_error("这一代需要 kv2 点表"))?;
        tweaks.extend_from_slice(kv2);
    }

    let mut reloc = Vec::with_capacity(dat.relocations.len() * 8);
    for &(patch, target) in &dat.relocations {
        reloc.extend_from_slice(&patch.to_be_bytes());
        reloc.extend_from_slice(&target.to_be_bytes());
    }
    let mut maps = Vec::with_capacity(456);
    let mut bank_blobs = Vec::with_capacity(3168);
    for m in &dat.modules {
        maps.extend_from_slice(&m.map);
        bank_blobs.extend_from_slice(&bank_blob(&m.records, prof.bank_generation()));
    }

    let mut parts: [Vec<u8>; 7] = [
        dat.engine.clone(),
        reloc,
        vec![0u8; 192],
        maps,
        bank_blobs,
        shared,
        tweaks,
    ];
    let mut starts = [0usize; 7];
    let mut cursor = HEADER;
    for (i, p) in parts.iter().enumerate() {
        starts[i] = cursor;
        cursor += p.len();
    }
    if cursor > prof.max_file || cursor + dat.bss as usize > MAX_RAM {
        return Err(dat_error(format!(
            "容量超限 {cursor}/{}",
            cursor + dat.bss as usize
        )));
    }
    check_engine_binding(
        dat.generation,
        &dat.engine,
        dat.entry,
        dat.bss,
        &dat.relocations,
        cursor,
    )?;

    let mut dir = vec![0u8; 192];
    let mut next_table = starts[5] + pool_header;
    let stride = usize::from(u16_be(&parts[6], 14));
    let meta_ranges_base = starts[6] + u32_be(&parts[6], 20) as usize;
    for (i, m) in dat.modules.iter().enumerate() {
        let off = 64 * i;
        dir[off..off + m.name.len()].copy_from_slice(m.name.as_bytes());
        dir[off + m.name.len()] = 0;
        dir[off + 8..off + 10].copy_from_slice(&0x151u16.to_be_bytes());
        dir[off + 10..off + 12].copy_from_slice(&0u16.to_be_bytes());
        dir[off + 12..off + 16].copy_from_slice(&((starts[3] + 152 * i) as u32).to_be_bytes());
        dir[off + 16..off + 20].copy_from_slice(&((starts[4] + 1056 * i) as u32).to_be_bytes());
        dir[off + 20..off + 24].copy_from_slice(&(next_table as u32).to_be_bytes());
        dir[off + 24..off + 28]
            .copy_from_slice(&((meta_ranges_base + 16 * stride * i) as u32).to_be_bytes());
        dir[off + 28..off + 30].copy_from_slice(&(tables[i].len() as u16).to_be_bytes());
        dir[off + 30] = m.entries.len() as u8;
        dir[off + 31] = 0;
        dir[off + 32..off + 64].copy_from_slice(&m.pin);
        next_table += tables[i].len();
    }
    parts[2] = dir;

    let mut h = vec![0u8; HEADER];
    h[..8].copy_from_slice(b"GXRCE001");
    h[8..10].copy_from_slice(&1u16.to_be_bytes());
    h[10..12].copy_from_slice(&256u16.to_be_bytes());
    h[12..16].copy_from_slice(&dat.flags.to_be_bytes());
    h[28..32].copy_from_slice(&dat.generation.to_be_bytes());
    h[32..34].copy_from_slice(&1u16.to_be_bytes());
    h[34..36].copy_from_slice(&3u16.to_be_bytes());
    h[36..40].copy_from_slice(b"M32B");
    h[40..42].copy_from_slice(&0x151u16.to_be_bytes());
    h[42..44].copy_from_slice(&0u16.to_be_bytes());
    h[44..46].copy_from_slice(&7u16.to_be_bytes());
    h[46..48].copy_from_slice(&3u16.to_be_bytes());
    h[48..80].copy_from_slice(&hex32(ILAUNCH3_SHA256));
    h[80..112].copy_from_slice(&dat.resident_id);
    h[112..116].copy_from_slice(&dat.entry.to_be_bytes());
    h[116..120].copy_from_slice(&dat.bss.to_be_bytes());
    h[120..122].copy_from_slice(&(dat.relocations.len() as u16).to_be_bytes());
    h[212..228].copy_from_slice(&dat.abi_id);
    for (i, p) in parts.iter().enumerate() {
        let off = 128 + 12 * i;
        h[off..off + 2].copy_from_slice(&((i + 1) as u16).to_be_bytes());
        h[off + 2..off + 4].copy_from_slice(&0u16.to_be_bytes());
        h[off + 4..off + 8].copy_from_slice(&(starts[i] as u32).to_be_bytes());
        h[off + 8..off + 12].copy_from_slice(&(p.len() as u32).to_be_bytes());
    }

    let mut b = Vec::with_capacity(cursor);
    b.extend_from_slice(&h);
    for p in &parts {
        b.extend_from_slice(p);
    }
    let total_len = b.len() as u32;
    b[16..20].copy_from_slice(&total_len.to_be_bytes());
    let body_crc = crc(&b[HEADER..]);
    b[24..28].copy_from_slice(&body_crc.to_be_bytes());
    b[20..24].copy_from_slice(&0u32.to_be_bytes());
    let hdr_crc = crc(&b[..HEADER]);
    b[20..24].copy_from_slice(&hdr_crc.to_be_bytes());
    Ok(b)
}
