//! Optimal Huffman tables for a baseline JPEG, as libjpeg-turbo writes them with
//! `optimize_coding = TRUE` (Pillow `optimize=True`, which `export.py:_finish` passes).
//!
//! `gxr-jpeg::encode_jpeg` matches Pillow byte for byte with `optimize=False`: the Annex K
//! tables in one interleaved scan. libjpeg-turbo's optimised mode encodes the **same symbols**
//! (the quantised coefficients don't change) in the same single interleaved scan, but with
//! tables built from the image's own symbol counts by `jpeg_gen_optimal_table`. So this module
//! is a lossless transcode of `encode_jpeg`'s output:
//! 1. decode the entropy-coded scan with the stored tables, counting each table's symbols
//!    (`jchuff.c:encode_mcu_gather` / `htest_one_block`);
//! 2. build each table with `jpeg_gen_optimal_table` (`jchuff.c`);
//! 3. decode the scan again and write every symbol with the new codes and the same extra bits,
//!    byte stuffing and the final 1-bit padding (`jchuff.c:flush_bits`);
//! 4. rewrite each DHT segment in place, in the same order. Every other byte is kept.
//!
//! Only what `encode_jpeg` produces is accepted: SOF0, 8-bit, one scan covering every
//! component, no restart markers. Anything else is the error `encode_failed`.

use gxr_raw::CoreError;

const MAX_CLEN: usize = 32;

fn bad(what: &str) -> CoreError {
    CoreError::new(
        "encode_failed",
        format!("无法导出。请重试。（JPEG 结构：{what}）"),
    )
}

/// One Huffman table as stored in DHT: `bits[1..=16]` and the symbol values.
#[derive(Clone, Debug, PartialEq, Eq)]
struct HuffTable {
    bits: [u8; 17],
    vals: Vec<u8>,
}

/// Canonical codes (Annex C) for encoding, and the decoding limits (Annex F.2.2.3).
struct Coder {
    code: [u32; 256],
    size: [u8; 256],
    mincode: [i32; 17],
    maxcode: [i32; 18],
    valptr: [i32; 17],
    vals: Vec<u8>,
}

impl Coder {
    fn new(t: &HuffTable) -> Result<Self, CoreError> {
        let mut code = [0_u32; 256];
        let mut size = [0_u8; 256];
        let mut mincode = [0_i32; 17];
        let mut maxcode = [-1_i32; 18];
        let mut valptr = [0_i32; 17];
        let mut next: u32 = 0;
        let mut k: usize = 0;
        for len in 1..=16_usize {
            let n = usize::from(t.bits[len]);
            valptr[len] = i32::try_from(k).map_err(|_| bad("DHT"))?;
            mincode[len] = i32::try_from(next).map_err(|_| bad("DHT"))?;
            for _ in 0..n {
                let sym = *t.vals.get(k).ok_or_else(|| bad("DHT"))?;
                let slot = usize::from(sym);
                if size[slot] != 0 {
                    return Err(bad("DHT 重复符号"));
                }
                code[slot] = next;
                size[slot] = u8::try_from(len).map_err(|_| bad("DHT"))?;
                next += 1;
                k += 1;
            }
            if n > 0 {
                maxcode[len] = i32::try_from(next - 1).map_err(|_| bad("DHT"))?;
            }
            if next > (1_u32 << len) {
                return Err(bad("DHT 码字溢出"));
            }
            next <<= 1;
        }
        maxcode[17] = i32::MAX;
        Ok(Self {
            code,
            size,
            mincode,
            maxcode,
            valptr,
            vals: t.vals.clone(),
        })
    }

    fn decode(&self, r: &mut BitReader<'_>) -> Result<u8, CoreError> {
        let mut code: i32 = 0;
        for len in 1..=16_usize {
            code = (code << 1) | i32::from(r.bit()?);
            if code <= self.maxcode[len] {
                let idx = self.valptr[len] + code - self.mincode[len];
                let idx = usize::try_from(idx).map_err(|_| bad("霍夫曼码"))?;
                return self.vals.get(idx).copied().ok_or_else(|| bad("霍夫曼码"));
            }
        }
        Err(bad("霍夫曼码"))
    }
}

/// Reads entropy-coded bits, removing the stuffed zero after each 0xFF.
struct BitReader<'a> {
    data: &'a [u8],
    pos: usize,
    cur: u8,
    left: u8,
}

impl<'a> BitReader<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self {
            data,
            pos: 0,
            cur: 0,
            left: 0,
        }
    }

    fn bit(&mut self) -> Result<u8, CoreError> {
        if self.left == 0 {
            let b = *self
                .data
                .get(self.pos)
                .ok_or_else(|| bad("扫描数据提前结束"))?;
            self.pos += 1;
            if b == 0xFF {
                match self.data.get(self.pos) {
                    Some(0x00) => self.pos += 1,
                    _ => return Err(bad("扫描数据里有标记")),
                }
            }
            self.cur = b;
            self.left = 8;
        }
        self.left -= 1;
        Ok((self.cur >> self.left) & 1)
    }

    fn bits(&mut self, n: u8) -> Result<u32, CoreError> {
        let mut v: u32 = 0;
        for _ in 0..n {
            v = (v << 1) | u32::from(self.bit()?);
        }
        Ok(v)
    }

    /// The padding after the last code: the rest of the byte, all 1 bits (`flush_bits`).
    fn finish(&mut self) -> Result<(), CoreError> {
        while self.left > 0 {
            if self.bit()? != 1 {
                return Err(bad("扫描末尾的填充位"));
            }
        }
        if self.pos != self.data.len() {
            return Err(bad("扫描数据后还有多余字节"));
        }
        Ok(())
    }
}

/// Writes entropy-coded bits with byte stuffing, as `jchuff.c` does.
struct BitWriter {
    out: Vec<u8>,
    acc: u32,
    n: u8,
}

impl BitWriter {
    fn put(&mut self, code: u32, len: u8) {
        for i in (0..len).rev() {
            self.acc = (self.acc << 1) | ((code >> i) & 1);
            self.n += 1;
            if self.n == 8 {
                let byte = (self.acc & 0xFF) as u8;
                self.out.push(byte);
                if byte == 0xFF {
                    self.out.push(0);
                }
                self.acc = 0;
                self.n = 0;
            }
        }
    }

    /// `flush_bits`: `emit_bits(0x7F, 7)` fills the last byte with 1 bits.
    fn flush(&mut self) {
        if self.n > 0 {
            let pad = 8 - self.n;
            self.put((1_u32 << pad) - 1, pad);
        }
    }
}

/// Port of libjpeg-turbo `jchuff.c jpeg_gen_optimal_table`, with its tie rules (the larger
/// symbol wins a tie) and its `1000000000L` search bound.
fn gen_optimal_table(freq_in: &[i64; 257]) -> Result<HuffTable, CoreError> {
    // A table no symbol uses (an extra DHT our encoder never writes) would leave no code
    // length to remove below; refuse it (Sol r1 P2-03).
    if freq_in[..256].iter().all(|&f| f == 0) {
        return Err(bad("未使用的霍夫曼表"));
    }
    let mut freq = *freq_in;
    let mut bits = [0_u8; MAX_CLEN + 1];
    let mut codesize = [0_i32; 257];
    let mut others = [-1_i32; 257];
    freq[256] = 1;
    loop {
        let mut c1: i32 = -1;
        let mut v: i64 = 1_000_000_000;
        for (i, &f) in freq.iter().enumerate() {
            if f != 0 && f <= v {
                v = f;
                c1 = i as i32;
            }
        }
        let mut c2: i32 = -1;
        v = 1_000_000_000;
        for (i, &f) in freq.iter().enumerate() {
            if f != 0 && f <= v && i as i32 != c1 {
                v = f;
                c2 = i as i32;
            }
        }
        if c2 < 0 || c1 < 0 {
            break;
        }
        let (mut a, mut b) = (c1 as usize, c2 as usize);
        freq[a] += freq[b];
        freq[b] = 0;
        codesize[a] += 1;
        while others[a] >= 0 {
            a = others[a] as usize;
            codesize[a] += 1;
        }
        others[a] = c2;
        codesize[b] += 1;
        while others[b] >= 0 {
            b = others[b] as usize;
            codesize[b] += 1;
        }
    }
    for &cs in &codesize {
        if cs != 0 {
            let cs = usize::try_from(cs).map_err(|_| bad("霍夫曼码长"))?;
            if cs > MAX_CLEN {
                return Err(bad("霍夫曼码长溢出"));
            }
            bits[cs] += 1;
        }
    }
    let mut i = MAX_CLEN;
    while i > 16 {
        while bits[i] > 0 {
            let mut j = i - 2;
            while bits[j] == 0 {
                j -= 1;
            }
            bits[i] -= 2;
            bits[i - 1] += 1;
            bits[j + 1] += 2;
            bits[j] -= 1;
        }
        i -= 1;
    }
    while bits[i] == 0 {
        i -= 1;
    }
    bits[i] -= 1;
    let mut out_bits = [0_u8; 17];
    out_bits.copy_from_slice(&bits[..17]);
    let mut vals = Vec::new();
    for len in 1..=MAX_CLEN as i32 {
        for (j, &cs) in codesize.iter().enumerate().take(256) {
            if cs == len {
                vals.push(j as u8);
            }
        }
    }
    Ok(HuffTable {
        bits: out_bits,
        vals,
    })
}

#[derive(Clone, Copy)]
struct Comp {
    h: usize,
    v: usize,
    dc: usize,
    ac: usize,
}

struct Layout {
    comps: Vec<Comp>,
    mcus_x: usize,
    mcus_y: usize,
}

fn u16_be(data: &[u8], at: usize) -> Result<usize, CoreError> {
    let s = data.get(at..at + 2).ok_or_else(|| bad("段"))?;
    Ok(usize::from(u16::from_be_bytes([s[0], s[1]])))
}

/// A segment before SOS: `(marker, start, end)`.
type Seg = (u8, usize, usize);
/// One DHT segment's tables: `(class, id, table)`.
type DhtList = Vec<(u8, u8, HuffTable)>;

/// Segments before SOS, and the SOS offset.
fn segments(data: &[u8]) -> Result<(Vec<Seg>, usize), CoreError> {
    if data.get(0..2) != Some(&[0xFF, 0xD8]) {
        return Err(bad("缺 SOI"));
    }
    let mut i = 2;
    let mut out = Vec::new();
    loop {
        if data.get(i) != Some(&0xFF) {
            return Err(bad("段"));
        }
        let marker = *data.get(i + 1).ok_or_else(|| bad("段"))?;
        let len = u16_be(data, i + 2)?;
        let end = i + 2 + len;
        if len < 2 || end > data.len() {
            return Err(bad("段长度"));
        }
        if marker == 0xDA {
            return Ok((out, i));
        }
        out.push((marker, i, end));
        i = end;
    }
}

fn parse_dht(payload: &[u8]) -> Result<DhtList, CoreError> {
    let mut out = Vec::new();
    let mut p = 0;
    while p < payload.len() {
        let tc_th = payload[p];
        let mut bits = [0_u8; 17];
        let counts = payload.get(p + 1..p + 17).ok_or_else(|| bad("DHT"))?;
        bits[1..].copy_from_slice(counts);
        let total: usize = counts.iter().map(|&b| usize::from(b)).sum();
        let vals = payload
            .get(p + 17..p + 17 + total)
            .ok_or_else(|| bad("DHT"))?
            .to_vec();
        let (tc, th) = (tc_th >> 4, tc_th & 0x0F);
        if tc > 1 || th > 3 {
            return Err(bad("DHT 表号"));
        }
        out.push((tc, th, HuffTable { bits, vals }));
        p += 17 + total;
    }
    Ok(out)
}

fn layout(data: &[u8], segs: &[Seg], sos: usize) -> Result<Layout, CoreError> {
    let mut sof = None;
    for &(m, s, e) in segs {
        match m {
            0xC0 => sof = Some((s, e)),
            0xC1..=0xCF if m != 0xC4 && m != 0xC8 && m != 0xCC => return Err(bad("不是基线 JPEG")),
            0xDD => return Err(bad("有重启间隔")),
            _ => {}
        }
    }
    let (s, _) = sof.ok_or_else(|| bad("缺 SOF0"))?;
    let p = data.get(s + 4..).ok_or_else(|| bad("SOF0"))?;
    if p.first() != Some(&8) {
        return Err(bad("不是 8 位"));
    }
    let height = u16_be(p, 1)?;
    let width = u16_be(p, 3)?;
    let nc = usize::from(*p.get(5).ok_or_else(|| bad("SOF0"))?);
    if width == 0 || height == 0 || nc == 0 || nc > 4 {
        return Err(bad("SOF0"));
    }
    let mut frame = Vec::new();
    for c in 0..nc {
        let q = p.get(6 + 3 * c..9 + 3 * c).ok_or_else(|| bad("SOF0"))?;
        let (h, v) = (usize::from(q[1] >> 4), usize::from(q[1] & 0x0F));
        if !(1..=4).contains(&h) || !(1..=4).contains(&v) {
            return Err(bad("采样因子"));
        }
        frame.push((q[0], h, v));
    }
    let sp = data.get(sos + 4..).ok_or_else(|| bad("SOS"))?;
    let ns = usize::from(*sp.first().ok_or_else(|| bad("SOS"))?);
    if ns != nc {
        return Err(bad("不是单次交错扫描"));
    }
    let mut comps = Vec::new();
    for k in 0..ns {
        let q = sp.get(1 + 2 * k..3 + 2 * k).ok_or_else(|| bad("SOS"))?;
        let &(_, h, v) = frame
            .iter()
            .find(|f| f.0 == q[0])
            .ok_or_else(|| bad("SOS 分量"))?;
        comps.push(Comp {
            h,
            v,
            dc: usize::from(q[1] >> 4),
            ac: usize::from(q[1] & 0x0F),
        });
    }
    let tail = sp.get(1 + 2 * ns..4 + 2 * ns).ok_or_else(|| bad("SOS"))?;
    if tail != [0, 63, 0] {
        return Err(bad("不是基线扫描"));
    }
    let hmax = comps.iter().map(|c| c.h).max().unwrap_or(1);
    let vmax = comps.iter().map(|c| c.v).max().unwrap_or(1);
    Ok(Layout {
        mcus_x: width.div_ceil(8 * hmax),
        mcus_y: height.div_ceil(8 * vmax),
        comps,
    })
}

/// Walk every block of the scan, calling `sym(class, table, symbol, extra_bits, n_extra)`.
fn walk<F>(scan: &[u8], lay: &Layout, coders: &Coders, mut sym: F) -> Result<(), CoreError>
where
    F: FnMut(usize, usize, u8, u32, u8),
{
    let mut r = BitReader::new(scan);
    let mcus = lay
        .mcus_x
        .checked_mul(lay.mcus_y)
        .ok_or_else(|| bad("MCU 数"))?;
    for _ in 0..mcus {
        for c in &lay.comps {
            let dc = coders.get(0, c.dc)?;
            let ac = coders.get(1, c.ac)?;
            for _ in 0..c.h * c.v {
                let s = dc.decode(&mut r)?;
                if s > 15 {
                    return Err(bad("DC 类别"));
                }
                let extra = r.bits(s)?;
                sym(0, c.dc, s, extra, s);
                let mut k = 1;
                while k < 64 {
                    let s = ac.decode(&mut r)?;
                    let (run, size) = (usize::from(s >> 4), s & 0x0F);
                    if size == 0 {
                        if run == 15 {
                            sym(1, c.ac, s, 0, 0);
                            k += 16;
                            continue;
                        }
                        if run != 0 {
                            return Err(bad("AC 符号"));
                        }
                        sym(1, c.ac, s, 0, 0);
                        break;
                    }
                    k += run;
                    if k > 63 {
                        return Err(bad("AC 越界"));
                    }
                    let extra = r.bits(size)?;
                    sym(1, c.ac, s, extra, size);
                    k += 1;
                }
                if k > 64 {
                    return Err(bad("AC 越界"));
                }
            }
        }
    }
    r.finish()
}

struct Coders {
    tables: [[Option<Coder>; 4]; 2],
}

impl Coders {
    fn get(&self, class: usize, id: usize) -> Result<&Coder, CoreError> {
        self.tables
            .get(class)
            .and_then(|t| t.get(id))
            .and_then(Option::as_ref)
            .ok_or_else(|| bad("缺霍夫曼表"))
    }
}

fn dht_segment(tables: &[(u8, u8, HuffTable)]) -> Result<Vec<u8>, CoreError> {
    let mut payload = Vec::new();
    for (tc, th, t) in tables {
        payload.push((tc << 4) | th);
        payload.extend_from_slice(&t.bits[1..]);
        payload.extend_from_slice(&t.vals);
    }
    let len = u16::try_from(payload.len() + 2).map_err(|_| bad("DHT 太长"))?;
    let mut out = vec![0xFF, 0xC4];
    out.extend_from_slice(&len.to_be_bytes());
    out.extend_from_slice(&payload);
    Ok(out)
}

/// libjpeg-turbo writes JFIF version 1.01 (`JFIF_minor_version = 1`); the vendored encoder in
/// `gxr-jpeg` writes 1.02. Fix the minor version byte of a JFIF APP0 segment in place.
fn jfif_101(seg: &mut [u8]) {
    if seg.len() >= 11 && seg[1] == 0xE0 && &seg[4..9] == b"JFIF\0" && seg[9] == 1 {
        seg[10] = 1;
    }
}

/// Re-encode `jpeg` (from `gxr-jpeg::encode_jpeg`) as libjpeg-turbo writes it for Pillow's
/// `optimize=True`: optimal Huffman tables, libjpeg's marker order (DQT before SOF0) and JFIF
/// 1.01. The quantised coefficients and every other byte stay as they are.
pub fn optimize_huffman(jpeg: &[u8]) -> Result<Vec<u8>, CoreError> {
    let (segs, sos) = segments(jpeg)?;
    let lay = layout(jpeg, &segs, sos)?;
    let mut stored: Vec<DhtList> = Vec::new();
    let mut tables: [[Option<Coder>; 4]; 2] = Default::default();
    for &(m, s, e) in &segs {
        if m == 0xC4 {
            let list = parse_dht(&jpeg[s + 4..e])?;
            for (tc, th, t) in &list {
                tables[usize::from(*tc)][usize::from(*th)] = Some(Coder::new(t)?);
            }
            stored.push(list);
        }
    }
    let coders = Coders { tables };
    let sos_end = sos + 2 + u16_be(jpeg, sos + 2)?;
    if jpeg.len() < sos_end + 2 || jpeg[jpeg.len() - 2..] != [0xFF, 0xD9] {
        return Err(bad("缺 EOI"));
    }
    let scan = &jpeg[sos_end..jpeg.len() - 2];

    let mut freq = [[[0_i64; 257]; 4]; 2];
    walk(scan, &lay, &coders, |class, id, s, _, _| {
        freq[class][id][usize::from(s)] += 1;
    })?;

    let mut new_tables: [[Option<HuffTable>; 4]; 2] = Default::default();
    let mut new_coders: [[Option<Coder>; 4]; 2] = Default::default();
    for class in 0..2 {
        for id in 0..4 {
            if coders.tables[class][id].is_some() {
                let t = gen_optimal_table(&freq[class][id])?;
                new_coders[class][id] = Some(Coder::new(&t)?);
                new_tables[class][id] = Some(t);
            }
        }
    }
    let new_coders = Coders { tables: new_coders };

    let mut w = BitWriter {
        out: Vec::with_capacity(scan.len()),
        acc: 0,
        n: 0,
    };
    let mut fail = None;
    walk(
        scan,
        &lay,
        &coders,
        |class, id, s, extra, n_extra| match new_coders.get(class, id) {
            Ok(c) => {
                let slot = usize::from(s);
                if c.size[slot] == 0 {
                    fail = Some(bad("新表缺符号"));
                    return;
                }
                w.put(c.code[slot], c.size[slot]);
                w.put(extra, n_extra);
            }
            Err(e) => fail = Some(e),
        },
    )?;
    if let Some(e) = fail {
        return Err(e);
    }
    w.flush();

    // libjpeg-turbo's marker order (`jcmarker.c`): SOI, the APPn/COM markers in the order
    // written (JFIF first, then Pillow's EXIF and ICC), every DQT, SOF0, the DHTs, SOS.
    let mut out = Vec::with_capacity(jpeg.len());
    out.extend_from_slice(&jpeg[..2]);
    for &(m, s, e) in &segs {
        if (0xE0..=0xEF).contains(&m) || m == 0xFE {
            let start = out.len();
            out.extend_from_slice(&jpeg[s..e]);
            jfif_101(&mut out[start..]);
        } else if !matches!(m, 0xDB | 0xC0 | 0xC4) {
            return Err(bad("未知的段"));
        }
    }
    for &(m, s, e) in &segs {
        if m == 0xDB {
            out.extend_from_slice(&jpeg[s..e]);
        }
    }
    for &(m, s, e) in &segs {
        if m == 0xC0 {
            out.extend_from_slice(&jpeg[s..e]);
        }
    }
    for list in &stored {
        let mut fresh = Vec::new();
        for (tc, th, _) in list {
            let t = new_tables[usize::from(*tc)][usize::from(*th)]
                .clone()
                .ok_or_else(|| bad("DHT"))?;
            fresh.push((*tc, *th, t));
        }
        out.extend_from_slice(&dht_segment(&fresh)?);
    }
    out.extend_from_slice(&jpeg[sos..sos_end]);
    out.extend_from_slice(&w.out);
    out.extend_from_slice(&[0xFF, 0xD9]);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn optimal_table_matches_known_small_case() {
        let mut freq = [0_i64; 257];
        freq[0] = 10;
        freq[1] = 5;
        freq[2] = 1;
        let t = gen_optimal_table(&freq).unwrap();
        // Symbols 0 (len 1), 1 (len 2), 2 (len 3); the reserved 256 takes the all-ones code
        // and is dropped from the counts.
        assert_eq!(&t.bits[1..4], &[1, 1, 1]);
        assert_eq!(t.vals, vec![0, 1, 2]);
    }

    #[test]
    fn unused_table_is_an_error_not_a_panic() {
        let err = gen_optimal_table(&[0_i64; 257]).unwrap_err();
        assert_eq!(err.kind, "encode_failed");
    }

    #[test]
    fn rejects_non_jpeg() {
        assert!(optimize_huffman(b"not a jpeg").is_err());
        assert!(optimize_huffman(&[]).is_err());
    }
}
