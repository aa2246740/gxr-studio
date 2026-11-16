//! libjpeg-turbo's `optimize_coding = TRUE` (Pillow `optimize=True`, what `export.py:_finish`
//! passes) for the single interleaved baseline scan.
//!
//! libjpeg-turbo keeps the whole coefficient array for this (`jccoefct.c` full-buffer mode), which
//! at 4288x2848 4:4:4 is 2 bytes per sample, twice the RGB input. This encoder instead runs the
//! block pipeline twice over the same row strips:
//! 1. **gather** (`jchuff.c encode_mcu_gather` / `htest_one_block`): colour conversion,
//!    downsampling, FDCT and quantisation, counting each table's symbols and writing nothing;
//! 2. build each table with `jpeg_gen_optimal_table` ([`gen_optimal_table`]);
//! 3. **write**: the same blocks again, coded with the new tables.
//!
//! The quantised coefficients are a pure function of the input, so both passes see the same
//! symbols. Peak memory stays one MCU row strip plus the output. The headers follow libjpeg's
//! marker order (`jcmarker.c`): JFIF 1.01, the APPn segments, every DQT, SOF0, one DHT per table
//! (DC0, AC0, DC1, AC1), SOS.

use super::encoder::{Component, Encoder};
use super::encoder_scans::{Operations, ceil_div, get_block};
use super::error::EncodingError;
use super::huffman::HuffmanTable;
use super::image_buffer::ImageBuffer;
use super::quantization::QuantizationTable;
use super::writer::JfifWrite;

const MAX_CLEN: usize = 32;
/// `jchuff.c`: `MAX_COEF_BITS + 1` for 8-bit samples.
const MAX_DC_BITS: u8 = 11;

fn huff_err(what: &str) -> EncodingError {
    EncodingError::Huffman(what.to_string())
}

/// Symbol counts per table: `[table][class]`, class 0 = DC, 1 = AC. Index 256 is unused here
/// (`gen_optimal_table` reserves it).
pub(super) type Freqs = [[[i64; 257]; 2]; 2];

/// Port of libjpeg-turbo `jchuff.c jpeg_gen_optimal_table`, with its tie rules (the larger
/// symbol wins a tie) and its `1000000000L` search bound. Returns `(bits[1..=16], huffval)`.
///
/// A table no symbol uses has no code length to take away for the reserved symbol, so it is an
/// error, never a panic (the old `huffopt` P2-03 case).
pub(super) fn gen_optimal_table(
    freq_in: &[i64; 257],
) -> Result<([u8; 16], Vec<u8>), EncodingError> {
    if freq_in[..256].iter().all(|&f| f == 0) {
        return Err(huff_err("未使用的霍夫曼表"));
    }
    if freq_in.iter().any(|&f| f < 0) {
        return Err(huff_err("霍夫曼频数"));
    }
    let mut freq = *freq_in;
    let mut codesize = [0_usize; 257];
    let mut others = [usize::MAX; 257];
    freq[256] = 1;
    loop {
        let mut c1 = None;
        let mut v: i64 = 1_000_000_000;
        for (i, &f) in freq.iter().enumerate() {
            if f != 0 && f <= v {
                v = f;
                c1 = Some(i);
            }
        }
        let mut c2 = None;
        v = 1_000_000_000;
        for (i, &f) in freq.iter().enumerate() {
            if f != 0 && f <= v && Some(i) != c1 {
                v = f;
                c2 = Some(i);
            }
        }
        let (Some(mut a), Some(mut b)) = (c1, c2) else {
            break;
        };
        let c2_index = b;
        freq[a] = freq[a]
            .checked_add(freq[b])
            .ok_or_else(|| huff_err("霍夫曼频数"))?;
        freq[b] = 0;
        codesize[a] += 1;
        while others[a] != usize::MAX {
            a = others[a];
            codesize[a] += 1;
        }
        others[a] = c2_index;
        codesize[b] += 1;
        while others[b] != usize::MAX {
            b = others[b];
            codesize[b] += 1;
        }
    }
    let mut bits = [0_u32; MAX_CLEN + 1];
    for &cs in &codesize {
        if cs != 0 {
            if cs > MAX_CLEN {
                return Err(huff_err("霍夫曼码长溢出"));
            }
            bits[cs] += 1;
        }
    }
    // JPEG limits code lengths to 16 bits (Annex K.3 / `jchuff.c`).
    let mut i = MAX_CLEN;
    while i > 16 {
        while bits[i] > 0 {
            let mut j = i - 2;
            while bits[j] == 0 {
                j = j.checked_sub(1).ok_or_else(|| huff_err("霍夫曼码长"))?;
            }
            if bits[i] < 2 || j == 0 {
                return Err(huff_err("霍夫曼码长"));
            }
            bits[i] -= 2;
            bits[i - 1] += 1;
            bits[j + 1] += 2;
            bits[j] -= 1;
        }
        i -= 1;
    }
    // Remove the reserved code point 256 (the longest code).
    while bits[i] == 0 {
        i = i.checked_sub(1).ok_or_else(|| huff_err("霍夫曼码长"))?;
    }
    if i == 0 {
        return Err(huff_err("霍夫曼码长"));
    }
    bits[i] -= 1;
    let mut out_bits = [0_u8; 16];
    for (slot, &n) in out_bits.iter_mut().zip(&bits[1..=16]) {
        *slot = u8::try_from(n).map_err(|_| huff_err("霍夫曼码长"))?;
    }
    let mut vals = Vec::new();
    for len in 1..=MAX_CLEN {
        for (j, &cs) in codesize.iter().enumerate().take(256) {
            if cs == len {
                vals.push(u8::try_from(j).map_err(|_| huff_err("霍夫曼符号"))?);
            }
        }
    }
    Ok((out_bits, vals))
}

/// The optimal table for `freq`, checked to give every counted symbol a code. libjpeg-turbo
/// would write such a symbol with zero bits (a broken file); this is an error instead.
pub(super) fn optimal_table(freq: &[i64; 257]) -> Result<HuffmanTable, EncodingError> {
    let (bits, vals) = gen_optimal_table(freq)?;
    let table = HuffmanTable::new(&bits, &vals);
    for (sym, &f) in freq.iter().enumerate().take(256) {
        if f > 0 && table.get_for_value(sym as u8).0 == 0 {
            return Err(huff_err("新表缺符号"));
        }
    }
    Ok(table)
}

/// The tables for every `(table, class)` a component uses, built from `freqs`.
pub(super) fn optimal_tables(
    components: &[Component],
    freqs: &Freqs,
) -> Result<[Option<(HuffmanTable, HuffmanTable)>; 2], EncodingError> {
    let mut out: [Option<(HuffmanTable, HuffmanTable)>; 2] = [None, None];
    for (t, slot) in out.iter_mut().enumerate() {
        let used = components
            .iter()
            .any(|c| usize::from(c.dc_huffman_table) == t || usize::from(c.ac_huffman_table) == t);
        if used {
            *slot = Some((optimal_table(&freqs[t][0])?, optimal_table(&freqs[t][1])?));
        }
    }
    Ok(out)
}

fn num_bits(value: i32) -> u8 {
    let mut v = value.unsigned_abs();
    let mut n = 0;
    while v > 0 {
        n += 1;
        v >>= 1;
    }
    n
}

/// `jchuff.c htest_one_block`: count one block's symbols into DC table `dc_t` and AC table
/// `ac_t`, with its coefficient range checks (`JERR_BAD_DCT_COEF`).
fn gather_block(
    block: &[i16; 64],
    prev_dc: i16,
    dc_t: usize,
    ac_t: usize,
    freqs: &mut Freqs,
) -> Result<(), EncodingError> {
    if dc_t > 1 || ac_t > 1 {
        return Err(huff_err("霍夫曼表号"));
    }
    let nbits = num_bits(i32::from(block[0]) - i32::from(prev_dc));
    if nbits > MAX_DC_BITS {
        return Err(huff_err("DCT 系数越界"));
    }
    freqs[dc_t][0][usize::from(nbits)] += 1;
    let ac = &mut freqs[ac_t][1];
    let mut run = 0_usize;
    for &value in &block[1..] {
        if value == 0 {
            run += 1;
        } else {
            while run > 15 {
                ac[0xF0] += 1;
                run -= 16;
            }
            let nbits = num_bits(i32::from(value));
            if nbits > MAX_DC_BITS - 1 {
                return Err(huff_err("DCT 系数越界"));
            }
            ac[(run << 4) + usize::from(nbits)] += 1;
            run = 0;
        }
    }
    if run > 0 {
        ac[0] += 1;
    }
    Ok(())
}

fn row_buffers(components: usize, buffer_size: usize) -> Result<[Vec<u8>; 4], EncodingError> {
    let n = match components {
        1 => 1,
        3 => 3,
        4 => 4,
        _ => return Err(huff_err("分量数")),
    };
    let mut rows: [Vec<u8>; 4] = Default::default();
    for row in rows.iter_mut().take(n) {
        *row = Vec::with_capacity(buffer_size);
    }
    Ok(rows)
}

/// Every block of the single interleaved scan, in MCU order, exactly as
/// `Encoder::encode_image_interleaved` makes them (same strips, edge padding, dummy blocks and
/// DC chaining; no restart intervals). `sink(component, q_block, prev_dc)` sees each one.
fn for_each_block<I, OP, F>(
    image: &I,
    components: &[Component],
    q_tables: &[QuantizationTable; 2],
    mut sink: F,
) -> Result<(), EncodingError>
where
    I: ImageBuffer,
    OP: Operations,
    F: FnMut(usize, &[i16; 64], i16) -> Result<(), EncodingError>,
{
    let max_h_sampling = usize::from(
        components
            .iter()
            .fold(1, |v, c| v.max(c.horizontal_sampling_factor)),
    );
    let max_v_sampling = usize::from(
        components
            .iter()
            .fold(1, |v, c| v.max(c.vertical_sampling_factor)),
    );
    let width = image.width();
    let height = image.height();
    if width == 0 || height == 0 {
        return Err(EncodingError::ZeroImageDimensions { width, height });
    }

    let num_cols = ceil_div(usize::from(width), 8 * max_h_sampling);
    let num_rows = ceil_div(usize::from(height), 8 * max_v_sampling);
    let buffer_width = num_cols * 8 * max_h_sampling;
    let buffer_size = buffer_width * 8 * max_v_sampling;
    let mut row = row_buffers(components.len(), buffer_size)?;
    let mut prev_dc = [0_i16; 4];

    let valid_blocks_x = ceil_div(usize::from(width), 8);
    let valid_blocks_y = ceil_div(usize::from(height), 8);

    for block_y in 0..num_rows {
        for r in &mut row {
            r.clear();
        }
        for y in 0..(8 * max_v_sampling) {
            let y = y + block_y * 8 * max_v_sampling;
            let y = y.min(usize::from(height) - 1) as u16;
            image.fill_buffers(y, &mut row);
            for _ in usize::from(width)..buffer_width {
                for channel in &mut row {
                    if let Some(&last) = channel.last() {
                        channel.push(last);
                    }
                }
            }
        }

        let max_strip_cy = (ceil_div(usize::from(height), 2) - 1).saturating_sub(block_y * 8);
        for block_x in 0..num_cols {
            for (i, component) in components.iter().enumerate() {
                let h_samp = usize::from(component.horizontal_sampling_factor);
                let v_samp = usize::from(component.vertical_sampling_factor);
                let h_scale = max_h_sampling / h_samp;
                let v_scale = max_v_sampling / v_samp;
                let valid_comp_x = ceil_div(valid_blocks_x, h_scale);
                let valid_comp_y = ceil_div(valid_blocks_y, v_scale);

                for v_offset in 0..v_samp {
                    let by = block_y * v_samp + v_offset;
                    for h_offset in 0..h_samp {
                        let bx = block_x * h_samp + h_offset;
                        let mut q_block = [0_i16; 64];
                        if bx >= valid_comp_x || by >= valid_comp_y {
                            // libjpeg jccoefct.c dummy block: previous DC, zero ACs.
                            q_block[0] = prev_dc[i];
                        } else {
                            let mut block = get_block(
                                &row[i],
                                block_x * 8 * max_h_sampling + (h_offset * 8),
                                v_offset * 8,
                                h_scale,
                                v_scale,
                                buffer_width,
                                max_strip_cy,
                            );
                            OP::fdct(&mut block);
                            OP::quantize_block(
                                &block,
                                &mut q_block,
                                &q_tables[usize::from(component.quantization_table)],
                            );
                        }
                        sink(i, &q_block, prev_dc[i])?;
                        prev_dc[i] = q_block[0];
                    }
                }
            }
        }
    }
    Ok(())
}

impl<W: JfifWrite> Encoder<W> {
    /// Encode all components in one interleaved scan with optimal Huffman tables, the way
    /// libjpeg-turbo does for Pillow `optimize=True` (see the module comment).
    pub(super) fn encode_image_libjpeg_optimized<I: ImageBuffer, OP: Operations>(
        &mut self,
        image: I,
        q_tables: &[QuantizationTable; 2],
    ) -> Result<(), EncodingError> {
        if self.restart_interval.is_some() || self.progressive_scans.is_some() {
            return Err(huff_err("只支持单次交错扫描"));
        }
        if !self.sampling_factor().supports_interleaved() {
            return Err(huff_err("只支持单次交错扫描"));
        }

        // Pass 1: count symbols.
        let mut freqs: Freqs = [[[0; 257]; 2]; 2];
        {
            let components = &self.components;
            for_each_block::<I, OP, _>(&image, components, q_tables, |i, q_block, prev_dc| {
                let c = &components[i];
                gather_block(
                    q_block,
                    prev_dc,
                    usize::from(c.dc_huffman_table),
                    usize::from(c.ac_huffman_table),
                    &mut freqs,
                )
            })?;
        }

        // Tables from the counts.
        let tables = optimal_tables(&self.components, &freqs)?;
        for (t, table) in tables.into_iter().enumerate() {
            if let Some(pair) = table {
                self.huffman_tables[t] = pair;
            }
        }

        self.write_frame_header(&image, q_tables)?;
        self.writer
            .write_scan_header(&self.components.iter().collect::<Vec<_>>(), None)?;

        // Pass 2: the same blocks, written with the new tables.
        let components = &self.components;
        let huffman_tables = &self.huffman_tables;
        let writer = &mut self.writer;
        for_each_block::<I, OP, _>(&image, components, q_tables, |i, q_block, prev_dc| {
            let c = &components[i];
            writer.write_block(
                q_block,
                prev_dc,
                &huffman_tables[usize::from(c.dc_huffman_table)].0,
                &huffman_tables[usize::from(c.ac_huffman_table)].1,
            )
        })?;
        self.writer.finalize_bit_buffer()?;
        Ok(())
    }
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
        let (bits, vals) = gen_optimal_table(&freq).unwrap();
        // Symbols 0 (len 1), 1 (len 2), 2 (len 3); the reserved 256 takes the all-ones code
        // and is dropped from the counts.
        assert_eq!(&bits[..3], &[1, 1, 1]);
        assert_eq!(vals, vec![0, 1, 2]);
    }

    #[test]
    fn single_symbol_table() {
        let mut freq = [0_i64; 257];
        freq[0] = 7;
        let (bits, vals) = gen_optimal_table(&freq).unwrap();
        assert_eq!(bits[0], 1);
        assert_eq!(bits[1..].iter().map(|&b| u32::from(b)).sum::<u32>(), 0);
        assert_eq!(vals, vec![0]);
    }

    #[test]
    fn every_symbol_used_stays_within_16_bits() {
        // Fibonacci-like counts force long codes that the 16-bit limit must fold back.
        let mut freq = [0_i64; 257];
        let (mut a, mut b) = (1_i64, 1_i64);
        for f in freq.iter_mut().take(40) {
            *f = a;
            (a, b) = (b, (a + b).min(900_000_000));
        }
        for f in freq.iter_mut().take(256).skip(40) {
            *f = 1;
        }
        let table = optimal_table(&freq).unwrap();
        let total: u32 = table.length().iter().map(|&b| u32::from(b)).sum();
        assert_eq!(total, 256);
    }

    #[test]
    fn unused_table_is_an_error_not_a_panic() {
        let err = gen_optimal_table(&[0_i64; 257]).unwrap_err();
        assert!(matches!(err, EncodingError::Huffman(_)));
        // The reserved slot alone still counts as unused.
        let mut only_reserved = [0_i64; 257];
        only_reserved[256] = 5;
        assert!(gen_optimal_table(&only_reserved).is_err());
        let mut negative = [0_i64; 257];
        negative[3] = -1;
        assert!(gen_optimal_table(&negative).is_err());
    }

    #[test]
    fn unused_dc_or_ac_table_of_a_used_slot_is_an_error() {
        let comps = vec![Component {
            id: 1,
            quantization_table: 0,
            dc_huffman_table: 0,
            ac_huffman_table: 0,
            horizontal_sampling_factor: 1,
            vertical_sampling_factor: 1,
        }];
        let mut freqs: Freqs = [[[0; 257]; 2]; 2];
        freqs[0][0][0] = 1;
        // AC table 0 never counted: an error, not a panic.
        assert!(optimal_tables(&comps, &freqs).is_err());
        freqs[0][1][0] = 1;
        let tables = optimal_tables(&comps, &freqs).unwrap();
        assert!(tables[0].is_some() && tables[1].is_none());
        // DC table never counted.
        let mut freqs: Freqs = [[[0; 257]; 2]; 2];
        freqs[0][1][0] = 1;
        assert!(optimal_tables(&comps, &freqs).is_err());
    }

    #[test]
    fn dc_out_of_range_is_an_error() {
        let mut block = [0_i16; 64];
        block[0] = 2047;
        let mut freqs: Freqs = [[[0; 257]; 2]; 2];
        assert!(gather_block(&block, -2047, 0, 0, &mut freqs).is_err());
        assert!(gather_block(&block, 0, 0, 0, &mut freqs).is_ok());
        assert_eq!(freqs[0][0][11], 1);
        assert_eq!(freqs[0][1][0], 1);
        assert!(gather_block(&block, 0, 2, 0, &mut freqs).is_err());
        block[63] = 1024;
        assert!(gather_block(&block, 0, 0, 1, &mut freqs).is_err());
        block[63] = -1023;
        let mut freqs: Freqs = [[[0; 257]; 2]; 2];
        assert!(gather_block(&block, 0, 0, 1, &mut freqs).is_ok());
        // 62 zeros: three ZRL symbols, then run 14 with a 10-bit value.
        assert_eq!(freqs[1][1][0xF0], 3);
        assert_eq!(freqs[1][1][(14 << 4) | 10], 1);
    }
}
