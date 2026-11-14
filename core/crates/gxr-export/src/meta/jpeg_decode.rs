//! Pure-Rust JPEG structural, table, and entropy-scan validator matching Pillow `Image.load()`.
//!
//! Used by [`super::preview::dng_preview_location`] to reject corrupt or table-less JPEGs (such as
//! a bare SOI+SOF+SOS+EOI stub) that `JpegImageFile._open` accepts on open but `pv.load()` fails
//! to decode in `libjpeg`.

use super::tiff_exif::HARD_MAX_IMAGE_PIXELS;

const MAX_PROGRESSIVE_BLOCKS: usize = 2_500_000;

#[derive(Clone, Debug)]
struct SofComponent {
    id: u8,
    h_samp: u8,
    v_samp: u8,
    tq: u8,
}

#[derive(Clone, Debug)]
struct SofInfo {
    progressive: bool,
    width: u16,
    height: u16,
    components: Vec<SofComponent>,
    max_h: u8,
    max_v: u8,
}

#[derive(Clone, Debug)]
struct HuffTable {
    mincode: [i32; 17],
    maxcode: [i32; 17],
    valptr: [usize; 17],
    vals: Vec<u8>,
}

impl HuffTable {
    fn build(bits: &[u8; 16], vals: Vec<u8>) -> Option<Self> {
        let mut mincode = [0i32; 17];
        let mut maxcode = [-1i32; 17];
        let mut valptr = [0usize; 17];
        let mut p = 0usize;
        let mut code: i32 = 0;

        for l in 1..=16 {
            let cnt = usize::from(bits[l - 1]);
            if cnt > 0 {
                valptr[l] = p;
                mincode[l] = code;
                p = p.checked_add(cnt)?;
                code = code.checked_add(cnt as i32)?;
                maxcode[l] = code - 1;
                if code > (1i32 << l) {
                    return None;
                }
            }
            code <<= 1;
        }
        (p == vals.len()).then_some(Self {
            mincode,
            maxcode,
            valptr,
            vals,
        })
    }
}

struct EntropyReader<'a> {
    data: &'a [u8],
    pos: usize,
    bit_buf: u8,
    bits_left: u8,
    saw_unexpected_marker: bool,
}

impl<'a> EntropyReader<'a> {
    fn new(data: &'a [u8], pos: usize) -> Self {
        Self {
            data,
            pos,
            bit_buf: 0,
            bits_left: 0,
            saw_unexpected_marker: false,
        }
    }

    fn next_bit(&mut self) -> Option<i32> {
        if self.bits_left == 0 {
            let &b = self.data.get(self.pos)?;
            self.pos += 1;
            if b == 0xFF {
                loop {
                    let &b2 = self.data.get(self.pos)?;
                    self.pos += 1;
                    if b2 == 0xFF {
                        continue;
                    }
                    if b2 == 0x00 {
                        break;
                    }
                    self.saw_unexpected_marker = true;
                    return None;
                }
            }
            self.bit_buf = b;
            self.bits_left = 8;
        }
        self.bits_left -= 1;
        Some(i32::from((self.bit_buf >> self.bits_left) & 1))
    }

    fn read_bits(&mut self, n: u8) -> Option<u16> {
        let mut val: u16 = 0;
        for _ in 0..n {
            val = (val << 1) | (self.next_bit()? as u16);
        }
        Some(val)
    }

    fn decode_huff(&mut self, table: &HuffTable) -> Option<u8> {
        let mut code: i32 = 0;
        for l in 1..=16 {
            code = (code << 1) | self.next_bit()?;
            if table.maxcode[l] >= 0 && code <= table.maxcode[l] {
                let idx = table.valptr[l] + (code - table.mincode[l]) as usize;
                return table.vals.get(idx).copied();
            }
        }
        None
    }

    fn consume_restart(&mut self, expected_rst: u8) -> Option<()> {
        self.bit_buf = 0;
        self.bits_left = 0;
        while self.data.get(self.pos) == Some(&0xFF) {
            self.pos += 1;
        }
        let &m = self.data.get(self.pos)?;
        self.pos += 1;
        (m == 0xD0 + (expected_rst & 7)).then_some(())
    }

    fn finish_scan_to_marker(&mut self) -> Option<(u8, usize)> {
        self.bit_buf = 0;
        self.bits_left = 0;
        if *self.data.get(self.pos)? != 0xFF {
            return None;
        }
        while self.data.get(self.pos) == Some(&0xFF) {
            self.pos += 1;
        }
        let &m = self.data.get(self.pos)?;
        self.pos += 1;
        (m != 0x00).then_some((m, self.pos))
    }
}

/// Validate that `slice` is both openable AND decodable by Pillow (`Image.open(...).load()`).
pub fn is_pillow_decodable_jpeg(slice: &[u8]) -> bool {
    validate_jpeg_decode(slice).is_some()
}

fn next_marker(data: &[u8], pos: &mut usize, require_ff: bool) -> Option<u8> {
    if require_ff && data.get(*pos) != Some(&0xFF) {
        return None;
    }
    while data.get(*pos) == Some(&0xFF) {
        *pos += 1;
    }
    let &m = data.get(*pos)?;
    *pos += 1;
    Some(m)
}

fn validate_jpeg_decode(data: &[u8]) -> Option<()> {
    if !data.starts_with(b"\xff\xd8\xff") {
        return None;
    }
    let mut pos = 2usize;
    let mut dqt = [false; 4];
    let mut huff_dc: [Option<HuffTable>; 4] = [None, None, None, None];
    let mut huff_ac: [Option<HuffTable>; 4] = [None, None, None, None];
    let mut sof: Option<SofInfo> = None;
    let mut restart_interval: u16 = 0;
    let mut saw_scan = false;
    let mut prog_nonzero: Vec<Vec<u64>> = Vec::new();
    let mut marker = next_marker(data, &mut pos, false)?;

    loop {
        if marker == 0xD9 {
            return saw_scan.then_some(());
        }
        if marker == 0x00 || (0xD0..=0xD8).contains(&marker) || marker == 0x01 {
            marker = next_marker(data, &mut pos, false)?;
            continue;
        }
        let len_bytes = data.get(pos..pos + 2)?;
        pos += 2;
        let seg_len = usize::from(u16::from_be_bytes([len_bytes[0], len_bytes[1]]));
        let payload_len = seg_len.checked_sub(2)?;
        let payload = data.get(pos..pos + payload_len)?;
        pos += payload_len;

        match marker {
            0xC0..=0xC2 => {
                if sof.is_some() {
                    return None;
                }
                sof = Some(parse_sof(marker, payload)?);
            }
            0xC3 | 0xC5..=0xC7 | 0xC9..=0xCB | 0xCD..=0xCF => return None,
            0xDB => parse_dqt(payload, &mut dqt)?,
            0xC4 => parse_dht(payload, &mut huff_dc, &mut huff_ac)?,
            0xDD => {
                if payload.len() != 2 {
                    return None;
                }
                restart_interval = u16::from_be_bytes([payload[0], payload[1]]);
            }
            0xDA => {
                let info = sof.as_ref()?;
                let (next_m, next_pos) = decode_sos_scan(
                    data,
                    pos,
                    payload,
                    info,
                    &dqt,
                    &huff_dc,
                    &huff_ac,
                    restart_interval,
                    &mut prog_nonzero,
                )?;
                saw_scan = true;
                marker = next_m;
                pos = next_pos;
                continue;
            }
            _ => {}
        }
        marker = next_marker(data, &mut pos, true)?;
    }
}

fn parse_sof(marker: u8, payload: &[u8]) -> Option<SofInfo> {
    if payload.len() < 6 {
        return None;
    }
    let bits = payload[0];
    let height = u16::from_be_bytes([payload[1], payload[2]]);
    let width = u16::from_be_bytes([payload[3], payload[4]]);
    let nf = usize::from(payload[5]);
    if bits != 8
        || width == 0
        || height == 0
        || !matches!(nf, 1 | 3 | 4)
        || payload.len() != 6 + 3 * nf
    {
        return None;
    }
    if u64::from(width) * u64::from(height) > HARD_MAX_IMAGE_PIXELS {
        return None;
    }
    let mut components = Vec::with_capacity(nf);
    let mut max_h = 1u8;
    let mut max_v = 1u8;
    let mut mcu_blocks = 0usize;
    for c in payload[6..].chunks_exact(3) {
        let (id, h_samp, v_samp, tq) = (c[0], c[1] >> 4, c[1] & 0x0F, c[2]);
        if !(1..=4).contains(&h_samp)
            || !(1..=4).contains(&v_samp)
            || tq > 3
            || components.iter().any(|e: &SofComponent| e.id == id)
        {
            return None;
        }
        max_h = max_h.max(h_samp);
        max_v = max_v.max(v_samp);
        mcu_blocks += usize::from(h_samp) * usize::from(v_samp);
        components.push(SofComponent {
            id,
            h_samp,
            v_samp,
            tq,
        });
    }
    if nf > 1 && mcu_blocks > 10 {
        return None;
    }
    Some(SofInfo {
        progressive: marker == 0xC2,
        width,
        height,
        components,
        max_h,
        max_v,
    })
}

fn parse_dqt(mut payload: &[u8], dqt: &mut [bool; 4]) -> Option<()> {
    if payload.is_empty() {
        return None;
    }
    while !payload.is_empty() {
        let info = payload[0];
        let (prec, tq) = (info >> 4, usize::from(info & 0x0F));
        if prec > 1 || tq > 3 {
            return None;
        }
        let table_bytes = if prec == 0 { 64 } else { 128 };
        if payload.len() < 1 + table_bytes {
            return None;
        }
        dqt[tq] = true;
        payload = &payload[1 + table_bytes..];
    }
    Some(())
}

fn parse_dht(
    mut payload: &[u8],
    huff_dc: &mut [Option<HuffTable>; 4],
    huff_ac: &mut [Option<HuffTable>; 4],
) -> Option<()> {
    if payload.is_empty() {
        return None;
    }
    while !payload.is_empty() {
        if payload.len() < 17 {
            return None;
        }
        let (tc, th) = (payload[0] >> 4, usize::from(payload[0] & 0x0F));
        if tc > 1 || th > 3 {
            return None;
        }
        let bits: [u8; 16] = payload[1..17].try_into().ok()?;
        let sum: usize = bits.iter().map(|&b| usize::from(b)).sum();
        if sum > 256 || payload.len() < 17 + sum {
            return None;
        }
        let table = HuffTable::build(&bits, payload[17..17 + sum].to_vec())?;
        if tc == 0 {
            huff_dc[th] = Some(table);
        } else {
            huff_ac[th] = Some(table);
        }
        payload = &payload[17 + sum..];
    }
    Some(())
}

struct ScanComp {
    comp_idx: usize,
    td: usize,
    ta: usize,
    h_samp: usize,
    v_samp: usize,
}

#[allow(clippy::too_many_arguments)]
fn decode_sos_scan(
    data: &[u8],
    scan_start: usize,
    payload: &[u8],
    sof: &SofInfo,
    dqt: &[bool; 4],
    huff_dc: &[Option<HuffTable>; 4],
    huff_ac: &[Option<HuffTable>; 4],
    restart_interval: u16,
    prog_nonzero: &mut Vec<Vec<u64>>,
) -> Option<(u8, usize)> {
    if payload.len() < 4 {
        return None;
    }
    let ns = usize::from(payload[0]);
    if !matches!(ns, 1..=4) || payload.len() != 4 + 2 * ns {
        return None;
    }
    let mut scan_comps = Vec::with_capacity(ns);
    for pair in payload[1..1 + 2 * ns].chunks_exact(2) {
        let (cs, td, ta) = (
            pair[0],
            usize::from(pair[1] >> 4),
            usize::from(pair[1] & 0x0F),
        );
        if td > 3 || ta > 3 {
            return None;
        }
        let comp_idx = sof.components.iter().position(|c| c.id == cs)?;
        let sof_comp = &sof.components[comp_idx];
        if !dqt[usize::from(sof_comp.tq)] {
            return None;
        }
        scan_comps.push(ScanComp {
            comp_idx,
            td,
            ta,
            h_samp: usize::from(sof_comp.h_samp),
            v_samp: usize::from(sof_comp.v_samp),
        });
    }

    let (ss, se) = (payload[1 + 2 * ns], payload[2 + 2 * ns]);
    let (ah, al) = (payload[3 + 2 * ns] >> 4, payload[3 + 2 * ns] & 0x0F);
    let mut reader = EntropyReader::new(data, scan_start);

    if !sof.progressive {
        if ss != 0 || se != 63 || ah != 0 || al != 0 {
            return None;
        }
        for sc in &scan_comps {
            huff_dc[sc.td].as_ref()?;
            huff_ac[sc.ta].as_ref()?;
        }
        decode_sequential_scan(
            &mut reader,
            sof,
            &scan_comps,
            huff_dc,
            huff_ac,
            restart_interval,
        )?;
    } else {
        if ss > se || se > 63 || ah > 13 || al > 13 {
            return None;
        }
        if ss == 0 {
            if se != 0 {
                return None;
            }
            if ah == 0 {
                for sc in &scan_comps {
                    huff_dc[sc.td].as_ref()?;
                }
            }
        } else {
            if ns != 1 {
                return None;
            }
            huff_ac[scan_comps[0].ta].as_ref()?;
        }
        if prog_nonzero.is_empty() {
            init_progressive_state(sof, prog_nonzero)?;
        }
        decode_progressive_scan(
            &mut reader,
            sof,
            &scan_comps,
            ss,
            se,
            ah,
            huff_dc,
            huff_ac,
            restart_interval,
            prog_nonzero,
        )?;
    }

    if reader.saw_unexpected_marker {
        return None;
    }
    reader.finish_scan_to_marker()
}

fn scan_grid(sof: &SofInfo, scan_comps: &[ScanComp]) -> (usize, usize) {
    let (w, h) = (usize::from(sof.width), usize::from(sof.height));
    let (max_h, max_v) = (usize::from(sof.max_h), usize::from(sof.max_v));
    if scan_comps.len() == 1 {
        let sc = &scan_comps[0];
        let comp_w = (w * sc.h_samp).div_ceil(max_h);
        let comp_h = (h * sc.v_samp).div_ceil(max_v);
        (comp_w.div_ceil(8), comp_h.div_ceil(8))
    } else {
        (w.div_ceil(8 * max_h), h.div_ceil(8 * max_v))
    }
}

fn decode_sequential_scan(
    reader: &mut EntropyReader<'_>,
    sof: &SofInfo,
    scan_comps: &[ScanComp],
    huff_dc: &[Option<HuffTable>; 4],
    huff_ac: &[Option<HuffTable>; 4],
    restart_interval: u16,
) -> Option<()> {
    let (mcu_cols, mcu_rows) = scan_grid(sof, scan_comps);
    let total_mcus = mcu_cols.checked_mul(mcu_rows)?;
    let interleaved = scan_comps.len() > 1;
    let dri = usize::from(restart_interval);
    let mut rst_num = 0u8;

    for mcu in 0..total_mcus {
        if dri > 0 && mcu > 0 && mcu.is_multiple_of(dri) {
            reader.consume_restart(rst_num)?;
            rst_num = rst_num.wrapping_add(1);
        }
        for sc in scan_comps {
            let dc_tbl = huff_dc[sc.td].as_ref()?;
            let ac_tbl = huff_ac[sc.ta].as_ref()?;
            let blocks = if interleaved {
                sc.h_samp * sc.v_samp
            } else {
                1
            };
            for _ in 0..blocks {
                let s = reader.decode_huff(dc_tbl)?;
                if s > 11 {
                    return None;
                }
                if s > 0 {
                    reader.read_bits(s)?;
                }
                let mut k = 1usize;
                while k < 64 {
                    let rs = reader.decode_huff(ac_tbl)?;
                    if rs == 0 {
                        break;
                    }
                    if rs == 0xF0 {
                        k += 16;
                        if k > 64 {
                            return None;
                        }
                        continue;
                    }
                    let (r, ac_s) = (usize::from(rs >> 4), rs & 0x0F);
                    k += r;
                    if k >= 64 || ac_s > 10 {
                        return None;
                    }
                    if ac_s > 0 {
                        reader.read_bits(ac_s)?;
                    }
                    k += 1;
                }
            }
        }
    }
    Some(())
}

fn init_progressive_state(sof: &SofInfo, state: &mut Vec<Vec<u64>>) -> Option<()> {
    let (w, h) = (usize::from(sof.width), usize::from(sof.height));
    let (max_h, max_v) = (usize::from(sof.max_h), usize::from(sof.max_v));
    let mut total_blocks = 0usize;
    for comp in &sof.components {
        let (ch, cv) = (usize::from(comp.h_samp), usize::from(comp.v_samp));
        let bw = ((w * ch).div_ceil(max_h)).div_ceil(8);
        let bh = ((h * cv).div_ceil(max_v)).div_ceil(8);
        let mcu_bw = w.div_ceil(8 * max_h) * ch;
        let mcu_bh = h.div_ceil(8 * max_v) * cv;
        let alloc = bw.max(mcu_bw).checked_mul(bh.max(mcu_bh))?;
        total_blocks = total_blocks.checked_add(alloc)?;
        if total_blocks > MAX_PROGRESSIVE_BLOCKS {
            return None;
        }
        state.push(vec![0u64; alloc]);
    }
    Some(())
}

#[allow(clippy::too_many_arguments)]
fn decode_progressive_scan(
    reader: &mut EntropyReader<'_>,
    sof: &SofInfo,
    scan_comps: &[ScanComp],
    ss: u8,
    se: u8,
    ah: u8,
    huff_dc: &[Option<HuffTable>; 4],
    huff_ac: &[Option<HuffTable>; 4],
    restart_interval: u16,
    prog_nonzero: &mut [Vec<u64>],
) -> Option<()> {
    let (mcu_cols, mcu_rows) = scan_grid(sof, scan_comps);
    let interleaved = scan_comps.len() > 1;
    let dri = usize::from(restart_interval);
    let mut rst_num = 0u8;
    let mut eobrun = 0usize;

    let max_h = usize::from(sof.max_h);
    let w = usize::from(sof.width);
    let mut block_row_stride = vec![0usize; sof.components.len()];
    for (idx, comp) in sof.components.iter().enumerate() {
        let ch = usize::from(comp.h_samp);
        let cw = ((w * ch).div_ceil(max_h)).div_ceil(8);
        block_row_stride[idx] = cw.max(w.div_ceil(8 * max_h) * ch);
    }

    for mcu_y in 0..mcu_rows {
        for mcu_x in 0..mcu_cols {
            let mcu = mcu_y * mcu_cols + mcu_x;
            if dri > 0 && mcu > 0 && mcu.is_multiple_of(dri) {
                reader.consume_restart(rst_num)?;
                rst_num = rst_num.wrapping_add(1);
                eobrun = 0;
            }
            for sc in scan_comps {
                let (h_blocks, v_blocks) = if interleaved {
                    (sc.h_samp, sc.v_samp)
                } else {
                    (1, 1)
                };
                for dy in 0..v_blocks {
                    for dx in 0..h_blocks {
                        let bx = if interleaved {
                            mcu_x * h_blocks + dx
                        } else {
                            mcu_x
                        };
                        let by = if interleaved {
                            mcu_y * v_blocks + dy
                        } else {
                            mcu_y
                        };
                        let bidx = by * block_row_stride[sc.comp_idx] + bx;
                        if ss == 0 {
                            if ah == 0 {
                                let s = reader.decode_huff(huff_dc[sc.td].as_ref()?)?;
                                if s > 11 {
                                    return None;
                                }
                                let val = if s > 0 { reader.read_bits(s)? } else { 0 };
                                if val != 0 {
                                    *prog_nonzero[sc.comp_idx].get_mut(bidx)? |= 1u64;
                                }
                            } else {
                                reader.next_bit()?;
                            }
                        } else {
                            let ac_tbl = huff_ac[sc.ta].as_ref()?;
                            let mask_ref = prog_nonzero[sc.comp_idx].get_mut(bidx)?;
                            if ah == 0 {
                                decode_prog_ac_first(
                                    reader,
                                    ac_tbl,
                                    ss,
                                    se,
                                    &mut eobrun,
                                    mask_ref,
                                )?;
                            } else {
                                decode_prog_ac_refine(
                                    reader,
                                    ac_tbl,
                                    ss,
                                    se,
                                    &mut eobrun,
                                    mask_ref,
                                )?;
                            }
                        }
                    }
                }
            }
        }
    }
    Some(())
}

fn decode_prog_ac_first(
    reader: &mut EntropyReader<'_>,
    ac_tbl: &HuffTable,
    ss: u8,
    se: u8,
    eobrun: &mut usize,
    mask: &mut u64,
) -> Option<()> {
    if *eobrun > 0 {
        *eobrun -= 1;
        return Some(());
    }
    let mut k = ss;
    while k <= se {
        let rs = reader.decode_huff(ac_tbl)?;
        let (r, s) = (rs >> 4, rs & 0x0F);
        if s == 0 {
            if r == 15 {
                k = k.checked_add(16)?;
                if k > se + 1 {
                    return None;
                }
            } else {
                let extra = reader.read_bits(r)? as usize;
                *eobrun = (1usize << r) + extra - 1;
                return Some(());
            }
        } else {
            k = k.checked_add(r)?;
            if k > se || s > 10 {
                return None;
            }
            reader.read_bits(s)?;
            *mask |= 1u64 << k;
            k += 1;
        }
    }
    Some(())
}

fn decode_prog_ac_refine(
    reader: &mut EntropyReader<'_>,
    ac_tbl: &HuffTable,
    ss: u8,
    se: u8,
    eobrun: &mut usize,
    mask: &mut u64,
) -> Option<()> {
    let mut k = ss;
    if *eobrun > 0 {
        while k <= se {
            if (*mask & (1u64 << k)) != 0 {
                reader.next_bit()?;
            }
            k += 1;
        }
        *eobrun -= 1;
        return Some(());
    }

    while k <= se {
        let rs = reader.decode_huff(ac_tbl)?;
        let (r, s) = (rs >> 4, rs & 0x0F);
        if s == 0 && r < 15 {
            let extra = reader.read_bits(r)? as usize;
            *eobrun = (1usize << r) + extra - 1;
            while k <= se {
                if (*mask & (1u64 << k)) != 0 {
                    reader.next_bit()?;
                }
                k += 1;
            }
            return Some(());
        }
        if s > 1 || (s == 0 && r != 15) {
            return None;
        }
        if s == 1 {
            reader.next_bit()?;
        }
        let mut zeros = r;
        loop {
            if k > se {
                return None;
            }
            if (*mask & (1u64 << k)) != 0 {
                reader.next_bit()?;
            } else {
                if zeros == 0 {
                    if s == 1 {
                        *mask |= 1u64 << k;
                    }
                    k += 1;
                    break;
                }
                zeros -= 1;
            }
            k += 1;
        }
    }
    Some(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_tableless_sof0_sos_eoi_stub() {
        let stub = b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x06\x00\x08\x01\x01\x11\x00\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\xff\xd9";
        assert!(!is_pillow_decodable_jpeg(stub));
    }

    #[test]
    fn accepts_encoded_baseline_jpeg_and_rejects_truncated_scan() {
        let rgb = vec![128u8; 8 * 6 * 3];
        let buf = gxr_jpeg::encode_jpeg(&rgb, 8, 6, 90, "{}").expect("encode_jpeg");
        assert!(is_pillow_decodable_jpeg(&buf));
        assert!(!is_pillow_decodable_jpeg(&buf[..buf.len() - 2]));
        assert!(!is_pillow_decodable_jpeg(&buf[..buf.len() - 5]));
    }
}
