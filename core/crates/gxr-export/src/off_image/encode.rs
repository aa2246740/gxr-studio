//! 独立的 640×480 基线 4:2:2 + DRI 40 JPEG 编码器（libjpeg-turbo 整数定点算术）。
//!
//! 仅用于关机图生成，完全独立于 `gxr-jpeg`，不改动任何常规导出字节。
//! 颜色转换（`jccolor.c`）、水平 2:1 下采样（`jcsample.c h2v1_downsample` 交替偏置 0/1）、
//! 整数 FDCT（`jfdctint.c jpeg_fdct_islow`）、Annex K.1 量化缩放与 Annex K.3 霍夫曼表
//! 与 Pillow 12.2.0 / libjpeg-turbo 逐位对齐。

use gxr_raw::CoreError;

use super::check::{ANNEX_K, DRI_VALUE, HEIGHT, RESTARTS, WIDTH};

const ZIGZAG: [usize; 64] = [
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33, 40, 48, 41, 34, 27, 20,
    13, 6, 7, 14, 21, 28, 35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51, 58, 59,
    52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
];

const ANNEX_K_LUMA_Q: [u16; 64] = [
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55, 14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62, 18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113,
    92, 49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
];

const ANNEX_K_CHROMA_Q: [u16; 64] = [
    17, 18, 24, 47, 99, 99, 99, 99, 18, 21, 26, 66, 99, 99, 99, 99, 24, 26, 56, 99, 99, 99, 99, 99,
    47, 66, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
];

const CONST_BITS: i32 = 13;
const PASS1_BITS: i32 = 2;
const FIX_0_298631336: i32 = 2446;
const FIX_0_390180644: i32 = 3196;
const FIX_0_541196100: i32 = 4433;
const FIX_0_765366865: i32 = 6270;
const FIX_0_899976223: i32 = 7373;
const FIX_1_175875602: i32 = 9633;
const FIX_1_501321110: i32 = 12299;
const FIX_1_847759065: i32 = 15137;
const FIX_1_961570560: i32 = 16069;
const FIX_2_053119869: i32 = 16819;
const FIX_2_562915447: i32 = 20995;
const FIX_3_072711026: i32 = 25172;

#[inline]
fn rgb_to_ycbcr(r: u8, g: u8, b: u8) -> (u8, u8, u8) {
    let r = i32::from(r);
    let g = i32::from(g);
    let b = i32::from(b);
    let y = (19595 * r + 38470 * g + 7471 * b + 0x8000) >> 16;
    let cb = (-11059 * r - 21709 * g + 32768 * b + (128 << 16) + 0x7FFF) >> 16;
    let cr = (32768 * r - 27439 * g - 5329 * b + (128 << 16) + 0x7FFF) >> 16;
    (y as u8, cb as u8, cr as u8)
}

#[inline]
fn descale(x: i32, n: i32) -> i32 {
    (x + (1 << (n - 1))) >> n
}

fn fdct_islow(data: &mut [i16; 64]) {
    for y in 0..8 {
        let o = y * 8;
        let tmp0 = i32::from(data[o]) + i32::from(data[o + 7]);
        let tmp7 = i32::from(data[o]) - i32::from(data[o + 7]);
        let tmp1 = i32::from(data[o + 1]) + i32::from(data[o + 6]);
        let tmp6 = i32::from(data[o + 1]) - i32::from(data[o + 6]);
        let tmp2 = i32::from(data[o + 2]) + i32::from(data[o + 5]);
        let tmp5 = i32::from(data[o + 2]) - i32::from(data[o + 5]);
        let tmp3 = i32::from(data[o + 3]) + i32::from(data[o + 4]);
        let tmp4 = i32::from(data[o + 3]) - i32::from(data[o + 4]);

        let tmp10 = tmp0 + tmp3;
        let tmp13 = tmp0 - tmp3;
        let tmp11 = tmp1 + tmp2;
        let tmp12 = tmp1 - tmp2;

        data[o] = ((tmp10 + tmp11) << PASS1_BITS) as i16;
        data[o + 4] = ((tmp10 - tmp11) << PASS1_BITS) as i16;
        let z1 = (tmp12 + tmp13) * FIX_0_541196100;
        data[o + 2] = descale(z1 + tmp13 * FIX_0_765366865, CONST_BITS - PASS1_BITS) as i16;
        data[o + 6] = descale(z1 + tmp12 * -FIX_1_847759065, CONST_BITS - PASS1_BITS) as i16;

        let z1 = tmp4 + tmp7;
        let z2 = tmp5 + tmp6;
        let z3 = tmp4 + tmp6;
        let z4 = tmp5 + tmp7;
        let z5 = (z3 + z4) * FIX_1_175875602;
        let t4 = tmp4 * FIX_0_298631336;
        let t5 = tmp5 * FIX_2_053119869;
        let t6 = tmp6 * FIX_3_072711026;
        let t7 = tmp7 * FIX_1_501321110;
        let z1 = z1 * -FIX_0_899976223;
        let z2 = z2 * -FIX_2_562915447;
        let z3 = z3 * -FIX_1_961570560 + z5;
        let z4 = z4 * -FIX_0_390180644 + z5;

        data[o + 7] = descale(t4 + z1 + z3, CONST_BITS - PASS1_BITS) as i16;
        data[o + 5] = descale(t5 + z2 + z4, CONST_BITS - PASS1_BITS) as i16;
        data[o + 3] = descale(t6 + z2 + z3, CONST_BITS - PASS1_BITS) as i16;
        data[o + 1] = descale(t7 + z1 + z4, CONST_BITS - PASS1_BITS) as i16;
    }

    for x in 0..8 {
        let tmp0 = i32::from(data[x]) + i32::from(data[56 + x]);
        let tmp7 = i32::from(data[x]) - i32::from(data[56 + x]);
        let tmp1 = i32::from(data[8 + x]) + i32::from(data[48 + x]);
        let tmp6 = i32::from(data[8 + x]) - i32::from(data[48 + x]);
        let tmp2 = i32::from(data[16 + x]) + i32::from(data[40 + x]);
        let tmp5 = i32::from(data[16 + x]) - i32::from(data[40 + x]);
        let tmp3 = i32::from(data[24 + x]) + i32::from(data[32 + x]);
        let tmp4 = i32::from(data[24 + x]) - i32::from(data[32 + x]);

        let tmp10 = tmp0 + tmp3;
        let tmp13 = tmp0 - tmp3;
        let tmp11 = tmp1 + tmp2;
        let tmp12 = tmp1 - tmp2;

        data[x] = descale(tmp10 + tmp11, PASS1_BITS) as i16;
        data[32 + x] = descale(tmp10 - tmp11, PASS1_BITS) as i16;
        let z1 = (tmp12 + tmp13) * FIX_0_541196100;
        data[16 + x] = descale(z1 + tmp13 * FIX_0_765366865, CONST_BITS + PASS1_BITS) as i16;
        data[48 + x] = descale(z1 + tmp12 * -FIX_1_847759065, CONST_BITS + PASS1_BITS) as i16;

        let z1 = tmp4 + tmp7;
        let z2 = tmp5 + tmp6;
        let z3 = tmp4 + tmp6;
        let z4 = tmp5 + tmp7;
        let z5 = (z3 + z4) * FIX_1_175875602;
        let t4 = tmp4 * FIX_0_298631336;
        let t5 = tmp5 * FIX_2_053119869;
        let t6 = tmp6 * FIX_3_072711026;
        let t7 = tmp7 * FIX_1_501321110;
        let z1 = z1 * -FIX_0_899976223;
        let z2 = z2 * -FIX_2_562915447;
        let z3 = z3 * -FIX_1_961570560 + z5;
        let z4 = z4 * -FIX_0_390180644 + z5;

        data[56 + x] = descale(t4 + z1 + z3, CONST_BITS + PASS1_BITS) as i16;
        data[40 + x] = descale(t5 + z2 + z4, CONST_BITS + PASS1_BITS) as i16;
        data[24 + x] = descale(t6 + z2 + z3, CONST_BITS + PASS1_BITS) as i16;
        data[8 + x] = descale(t7 + z1 + z4, CONST_BITS + PASS1_BITS) as i16;
    }
}

fn scaled_quant_table(base: &[u16; 64], quality: u8) -> [u8; 64] {
    let q = u32::from(quality.clamp(1, 100));
    let scale = if q < 50 { 5000 / q } else { 200 - q * 2 };
    let mut out = [1_u8; 64];
    for (slot, &v) in out.iter_mut().zip(base.iter()) {
        let scaled = ((u32::from(v) * scale) + 50) / 100;
        *slot = scaled.clamp(1, 255) as u8;
    }
    out
}

fn quantize_zigzag(block: &[i16; 64], q_natural: &[u8; 64]) -> [i16; 64] {
    let mut out = [0_i16; 64];
    for (i, &z) in ZIGZAG.iter().enumerate() {
        let val = i32::from(block[z]);
        let q = i32::from(q_natural[z]) << 3;
        let mag = (val.abs() + (q >> 1)) / q;
        out[i] = if val < 0 { -(mag as i16) } else { mag as i16 };
    }
    out
}

struct HuffLookup {
    lut: [(u8, u16); 256],
}

impl HuffLookup {
    fn from_annex(bits: &[u8; 16], vals: &[u8]) -> Self {
        let mut sizes = [0_u8; 256];
        let mut k = 0_usize;
        for (i, &cnt) in bits.iter().enumerate() {
            for _ in 0..cnt {
                sizes[k] = (i + 1) as u8;
                k += 1;
            }
        }
        let mut lut = [(0_u8, 0_u16); 256];
        let mut code = 0_u16;
        let mut prev_size = sizes[0];
        for (idx, &sym) in vals.iter().enumerate() {
            let sz = sizes[idx];
            if sz != prev_size {
                code <<= sz - prev_size;
                prev_size = sz;
            }
            lut[usize::from(sym)] = (sz, code);
            code = code.wrapping_add(1);
        }
        Self { lut }
    }
}

struct BitWriter {
    out: Vec<u8>,
    buf: u64,
    bits: u8,
}

impl BitWriter {
    fn new(cap: usize) -> Self {
        Self {
            out: Vec::with_capacity(cap),
            buf: 0,
            bits: 0,
        }
    }

    #[inline]
    fn write_bits(&mut self, value: u32, size: u8) {
        self.buf = (self.buf << size) | u64::from(value);
        self.bits += size;
        while self.bits >= 8 {
            self.bits -= 8;
            let byte = ((self.buf >> self.bits) & 0xFF) as u8;
            self.out.push(byte);
            if byte == 0xFF {
                self.out.push(0x00);
            }
        }
        if self.bits == 0 {
            self.buf = 0;
        } else {
            self.buf &= (1_u64 << self.bits) - 1;
        }
    }

    fn flush_ones(&mut self) {
        if self.bits > 0 {
            let pad = 8 - self.bits;
            self.write_bits((1_u32 << pad) - 1, pad);
        }
    }

    #[inline]
    fn encode_block(&mut self, q: &[i16; 64], prev_dc: i16, dc: &HuffLookup, ac: &HuffLookup) {
        let diff = q[0] - prev_dc;
        let (dc_sz, dc_bits) = value_code(diff);
        let (h_sz, h_code) = dc.lut[usize::from(dc_sz)];
        self.write_bits(u32::from(h_code), h_sz);
        if dc_sz > 0 {
            self.write_bits(u32::from(dc_bits), dc_sz);
        }

        let mut zeros = 0_u8;
        for &coeff in &q[1..] {
            if coeff == 0 {
                zeros += 1;
                continue;
            }
            while zeros > 15 {
                let (z_sz, z_code) = ac.lut[0xF0];
                self.write_bits(u32::from(z_code), z_sz);
                zeros -= 16;
            }
            let (sz, bits) = value_code(coeff);
            let sym = (zeros << 4) | sz;
            let (a_sz, a_code) = ac.lut[usize::from(sym)];
            self.write_bits(u32::from(a_code), a_sz);
            self.write_bits(u32::from(bits), sz);
            zeros = 0;
        }
        if zeros > 0 {
            let (e_sz, e_code) = ac.lut[0x00];
            self.write_bits(u32::from(e_code), e_sz);
        }
    }
}

#[inline]
fn value_code(v: i16) -> (u8, u16) {
    if v == 0 {
        return (0, 0);
    }
    let abs = v.unsigned_abs();
    let bits = (16 - abs.leading_zeros()) as u8;
    let code = if v < 0 {
        (i32::from(v) - 1) as u16 & ((1_u16 << bits) - 1)
    } else {
        abs
    };
    (bits, code)
}

fn push_seg(out: &mut Vec<u8>, marker: u8, body: &[u8]) {
    out.push(0xFF);
    out.push(marker);
    let len = (body.len() + 2) as u16;
    out.extend_from_slice(&len.to_be_bytes());
    out.extend_from_slice(body);
}

/// 将 640×480 RGB 像素编码为标准基线 4:2:2 + DRI 40 + Annex K.3 JPEG（供 `remux_off_image` 重封装）。
pub fn encode_baseline_422_dri40(rgb: &[u8], quality: u8) -> Result<Vec<u8>, CoreError> {
    let w = WIDTH as usize;
    let h = HEIGHT as usize;
    if rgb.len() != w * h * 3 {
        return Err(CoreError::new(
            "bad_image",
            "关机图编码输入必须是 640×480 RGB。",
        ));
    }
    let q_luma = scaled_quant_table(&ANNEX_K_LUMA_Q, quality);
    let q_chroma = scaled_quant_table(&ANNEX_K_CHROMA_Q, quality);
    let dc0 = HuffLookup::from_annex(ANNEX_K[0].1, ANNEX_K[0].2);
    let dc1 = HuffLookup::from_annex(ANNEX_K[1].1, ANNEX_K[1].2);
    let ac0 = HuffLookup::from_annex(ANNEX_K[2].1, ANNEX_K[2].2);
    let ac1 = HuffLookup::from_annex(ANNEX_K[3].1, ANNEX_K[3].2);

    let mut bw = BitWriter::new(36 * 1024);
    // 先写常规段（含两段 DQT 与四段 DHT），再由 remux_off_image 统一合段与重排
    bw.out.extend_from_slice(b"\xFF\xD8");
    for (id, qt) in [(0_u8, &q_luma), (1_u8, &q_chroma)] {
        let mut body = [0_u8; 65];
        body[0] = id;
        for (i, &z) in ZIGZAG.iter().enumerate() {
            body[1 + i] = qt[z];
        }
        push_seg(&mut bw.out, 0xDB, &body);
    }
    let sof0 = [
        8, 0x01, 0xE0, 0x02, 0x80, 3, 1, 0x21, 0, 2, 0x11, 1, 3, 0x11, 1,
    ];
    push_seg(&mut bw.out, 0xC0, &sof0);
    for &(tc, bits, vals) in &ANNEX_K {
        let mut body = Vec::with_capacity(17 + vals.len());
        body.push(tc);
        body.extend_from_slice(bits);
        body.extend_from_slice(vals);
        push_seg(&mut bw.out, 0xC4, &body);
    }
    push_seg(&mut bw.out, 0xDD, &DRI_VALUE.to_be_bytes());
    let sos = [3, 1, 0x00, 2, 0x11, 3, 0x11, 0, 63, 0];
    push_seg(&mut bw.out, 0xDA, &sos);

    let mut strip_y = vec![0_u8; w * 8];
    let mut strip_cb = vec![0_u8; w * 8];
    let mut strip_cr = vec![0_u8; w * 8];

    for mcu_row in 0..=RESTARTS {
        let base_y = mcu_row * 8;
        for dy in 0..8 {
            let src_row = (base_y + dy) * w * 3;
            let dst_row = dy * w;
            for x in 0..w {
                let p = src_row + x * 3;
                let (yv, cbv, crv) = rgb_to_ycbcr(rgb[p], rgb[p + 1], rgb[p + 2]);
                strip_y[dst_row + x] = yv;
                strip_cb[dst_row + x] = cbv;
                strip_cr[dst_row + x] = crv;
            }
        }

        let mut prev_dc = [0_i16; 3];
        for mcu_col in 0..usize::from(DRI_VALUE) {
            let x_base = mcu_col * 16;

            // Y0 与 Y1
            for half in 0..2 {
                let mut blk = [0_i16; 64];
                for dy in 0..8 {
                    let row = dy * w + x_base + half * 8;
                    for dx in 0..8 {
                        blk[dy * 8 + dx] = i16::from(strip_y[row + dx]) - 128;
                    }
                }
                fdct_islow(&mut blk);
                let q = quantize_zigzag(&blk, &q_luma);
                bw.encode_block(&q, prev_dc[0], &dc0, &ac0);
                prev_dc[0] = q[0];
            }

            // Cb 与 Cr：libjpeg-turbo h2v1_downsample（每行交替偏置 0, 1, 0, 1...）
            for (comp_idx, strip) in [(1_usize, &strip_cb), (2_usize, &strip_cr)] {
                let mut blk = [0_i16; 64];
                for dy in 0..8 {
                    let row = dy * w + x_base;
                    let mut bias = 0_i32;
                    for dx in 0..8 {
                        let p0 = i32::from(strip[row + dx * 2]);
                        let p1 = i32::from(strip[row + dx * 2 + 1]);
                        let avg = (p0 + p1 + bias) >> 1;
                        blk[dy * 8 + dx] = (avg - 128) as i16;
                        bias ^= 1;
                    }
                }
                fdct_islow(&mut blk);
                let q = quantize_zigzag(&blk, &q_chroma);
                bw.encode_block(&q, prev_dc[comp_idx], &dc1, &ac1);
                prev_dc[comp_idx] = q[0];
            }
        }

        bw.flush_ones();
        if mcu_row < RESTARTS {
            bw.out.push(0xFF);
            bw.out.push(0xD0 | (mcu_row as u8 & 7));
        }
    }

    bw.out.extend_from_slice(b"\xFF\xD9");
    Ok(bw.out)
}
