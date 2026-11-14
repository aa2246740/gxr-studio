//! Integer recipe knobs. Every call starts again from the stock and base bytes.

use gxr_raw::CoreError;

use crate::crc::crc32;

#[path = "exp_q12.rs"]
mod exp_q12;
use exp_q12::EXP_Q12;

#[cfg(test)]
#[path = "knobs_tests.rs"]
mod tests;

const Q: i64 = 4096;
const MAX_DOSES: [i64; 10] = [
    16384, 16384, 16384, 4096, 16384, 4096, 4096, 4096, 4096, 4096,
];
const OFFSETS: [usize; 10] = [20, 82, 115, 216, 284, 346, 379, 412, 456, 518];
const LENGTHS: [usize; 10] = [62, 33, 96, 56, 62, 33, 33, 33, 62, 33];
const CRCS: [(usize, usize, usize); 9] = [
    (20, 62, 8),
    (82, 33, 12),
    (115, 96, 16),
    (216, 56, 212),
    (284, 62, 272),
    (346, 33, 276),
    (379, 66, 280),
    (456, 62, 448),
    (518, 33, 452),
];
const PARAMS: [&str; 10] = [
    "contrast",
    "highlight",
    "shadow",
    "fade",
    "saturation",
    "warmth",
    "tint",
    "split",
    "filter",
    "tone",
];

fn value(message: &str) -> CoreError {
    CoreError::new("value", message)
}

fn overflow() -> CoreError {
    value("integer overflow")
}

/// Python `//` (floor), not Rust's truncating division.
fn floor_div(n: i64, d: i64) -> Result<i64, CoreError> {
    if d == 0 {
        return Err(value("positive divisor"));
    }
    let q = n.checked_div(d).ok_or_else(overflow)?;
    let r = n.checked_rem(d).ok_or_else(overflow)?;
    if r != 0 && (n < 0) != (d < 0) {
        q.checked_sub(1).ok_or_else(overflow)
    } else {
        Ok(q)
    }
}

fn mul(a: i64, b: i64) -> Result<i64, CoreError> {
    a.checked_mul(b).ok_or_else(overflow)
}

fn add(a: i64, b: i64) -> Result<i64, CoreError> {
    a.checked_add(b).ok_or_else(overflow)
}

/// Port of gxr_studio/engine/knobs.py:20 rd
pub fn rd(n: i64, d: i64) -> Result<i64, CoreError> {
    if d <= 0 {
        return Err(value("positive divisor"));
    }
    let half = floor_div(d, 2)?;
    if n >= 0 {
        floor_div(add(n, half)?, d)
    } else {
        let mag = n.checked_neg().ok_or_else(overflow)?;
        Ok(-floor_div(add(mag, half)?, d)?)
    }
}

/// Counts from one `apply` call. Keys match the Python dict.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Stats {
    pub clamps: i64,
    pub node_repairs: i64,
    pub monotone_repairs: i64,
    pub slope_repairs: i64,
    pub colour_cap: i64,
    pub matrix_cap: i64,
    pub rowsum_cap: i64,
}

impl Stats {
    fn new() -> Self {
        Self {
            clamps: 0,
            node_repairs: 0,
            monotone_repairs: 0,
            slope_repairs: 0,
            colour_cap: 0,
            matrix_cap: 0,
            rowsum_cap: 0,
        }
    }
}

/// Port of gxr_studio/engine/knobs.py:25 clamp
fn clamp(v: i64, lo: i64, hi: i64, stats: &mut Stats) -> i64 {
    let r = v.max(lo).min(hi);
    if r != v {
        stats.clamps += 1;
    }
    r
}

/// Port of gxr_studio/engine/knobs.py:31 dec
pub fn dec(v: i64, sign: i64) -> i64 {
    if v & sign != 0 {
        -(v & (sign - 1))
    } else {
        v & (sign - 1)
    }
}

/// Port of gxr_studio/engine/knobs.py:32 enc
pub fn enc(v: i64, sign: i64) -> Result<i64, CoreError> {
    if v < 0 {
        let mag = v.checked_neg().ok_or_else(overflow)?;
        Ok(sign | mag)
    } else {
        Ok(v)
    }
}

fn read_u16(b: &[u8], at: usize) -> Result<u16, CoreError> {
    let bytes = b.get(at..at + 2).ok_or_else(|| value("entry length"))?;
    Ok(u16::from_be_bytes([bytes[0], bytes[1]]))
}

fn write_u16(b: &mut [u8], at: usize, v: i64) -> Result<(), CoreError> {
    let v = u16::try_from(v).map_err(|_| value("'H' format requires 0 <= number <= 65535"))?;
    b[at..at + 2].copy_from_slice(&v.to_be_bytes());
    Ok(())
}

fn put_u8(b: &mut [u8], at: usize, v: i64) -> Result<(), CoreError> {
    b[at] = u8::try_from(v).map_err(|_| value("byte must be in range(0, 256)"))?;
    Ok(())
}

/// Port of gxr_studio/engine/knobs.py:33 words
fn words(b: &[u8], at: usize) -> Result<[i64; 28], CoreError> {
    let mut out = [0; 28];
    for (i, slot) in out.iter_mut().enumerate() {
        *slot = dec(i64::from(read_u16(b, at + 2 * i)?), 2048);
    }
    Ok(out)
}

fn knots(b: &[u8], at: usize) -> Result<[i64; 33], CoreError> {
    let mut xs = [0; 33];
    for i in 0..31 {
        xs[i + 1] = i64::from(read_u16(b, at + 2 * i)?);
    }
    xs[32] = 4095;
    Ok(xs)
}

/// Amount, ten doses, and the two method flags.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Request {
    pub amount: i64,
    pub doses: [i64; 10],
    pub wb_method: i64,
    pub tone_method: i64,
}

impl Request {
    /// The Python default: amount 100, doses 0, both methods 0.
    pub fn identity() -> Self {
        Self {
            amount: 100,
            doses: [0; 10],
            wb_method: 0,
            tone_method: 0,
        }
    }

    /// Port of gxr_studio/engine/knobs.py:43 Request.validate
    pub fn validate(&self) -> Result<(), CoreError> {
        if !(0..=150).contains(&self.amount) {
            return Err(value("amount"));
        }
        if self.doses.len() != 10 {
            return Err(value("doses"));
        }
        for (&dose, &limit) in self.doses.iter().zip(MAX_DOSES.iter()) {
            if !(-limit..=limit).contains(&dose) {
                return Err(value("doses"));
            }
        }
        if self.doses[3] < 0 {
            return Err(value("fade is one-way"));
        }
        if !matches!(self.wb_method, 0 | 1) || !matches!(self.tone_method, 0 | 1) {
            return Err(value("method"));
        }
        Ok(())
    }
}

/// Port of gxr_studio/engine/knobs.py:50 checksum
fn checksum(b: &mut [u8]) -> Result<(), CoreError> {
    for &(at, length, crc_at) in &CRCS {
        let sum = crc32(&b[at..at + length]);
        b[crc_at..crc_at + 4].copy_from_slice(&sum.to_be_bytes());
    }
    Ok(())
}

/// Port of gxr_studio/engine/knobs.py:54 effective
fn effective(b: &[u8], stock: &[u8]) -> Vec<u8> {
    let mut o = b.to_vec();
    if b[1] & 1 == 0 {
        o[216..272].copy_from_slice(&stock[216..272]);
    }
    if b[1] & 2 == 0 {
        o[379..412].copy_from_slice(&b[82..115]);
        o[412..445].copy_from_slice(&b[82..115]);
    }
    if b[1] & 16 == 0 {
        o[284..379].copy_from_slice(&stock[284..379]);
    }
    o
}

/// Port of gxr_studio/engine/knobs.py:62 interp_q16
fn interp_q16(knot_bytes: &[u8], curve: &[u8], x: i64) -> Result<i64, CoreError> {
    let xs = knots(knot_bytes, 0)?;
    if x >= 4095 {
        return mul(i64::from(curve[32]), 65536);
    }
    let mut right = 1usize;
    while xs[right] < x {
        right += 1;
        if right >= xs.len() {
            return Err(CoreError::new("index", "tuple index out of range"));
        }
    }
    let left = right - 1;
    let span = xs[right] - xs[left];
    let delta = i64::from(curve[right]) - i64::from(curve[left]);
    let numer = mul(mul(x - xs[left], delta)?, 65536)?;
    add(mul(i64::from(curve[left]), 65536)?, rd(numer, span)?)
}

/// Port of gxr_studio/engine/knobs.py:71 blend
fn blend(stock: &[u8], base: &[u8], amount: i64, stats: &mut Stats) -> Result<Vec<u8>, CoreError> {
    if amount == 100 {
        return Ok(base.to_vec());
    }
    if amount == 0 || base == stock {
        return Ok(stock.to_vec());
    }
    let a = effective(stock, stock);
    let b = effective(base, stock);
    let mut out = base.to_vec();
    out[1] = stock[1] | base[1];
    if out[1] & 4 != 0 {
        write_u16(&mut out, 445, 0)?;
    }
    for (&at, &length) in OFFSETS.iter().zip(LENGTHS.iter()) {
        if matches!(at, 379 | 412) && out[1] & 2 == 0 {
            continue;
        }
        if length == 62 {
            out[at..at + 62].copy_from_slice(&b[at..at + 62]);
        } else if length == 96 {
            blend_sectors(&mut out, &a, &b, at, amount, stats)?;
        } else if length == 56 {
            blend_matrix(&mut out, &a, &b, at, amount, stats)?;
        } else {
            blend_curve(&mut out, &a, &b, at, length, amount, stats)?;
        }
    }
    Ok(out)
}

fn blend_sectors(
    out: &mut [u8],
    a: &[u8],
    b: &[u8],
    at: usize,
    amount: i64,
    stats: &mut Stats,
) -> Result<(), CoreError> {
    for j in 0..96 {
        let mixed = rd(
            add(
                mul(100 - amount, dec(i64::from(a[at + j]), 128))?,
                mul(amount, dec(i64::from(b[at + j]), 128))?,
            )?,
            100,
        )?;
        put_u8(out, at + j, enc(clamp(mixed, -127, 127, stats), 128)?)?;
    }
    for j in (0..96).step_by(4) {
        let a_sym = a[at + j + 2] == (a[at + j + 1] ^ 128) && a[at + j + 3] == a[at + j];
        let b_sym = b[at + j + 2] == (b[at + j + 1] ^ 128) && b[at + j + 3] == b[at + j];
        if a_sym && b_sym {
            out[at + j + 2] = out[at + j + 1] ^ 128;
            out[at + j + 3] = out[at + j];
        }
    }
    Ok(())
}

fn blend_matrix(
    out: &mut [u8],
    a: &[u8],
    b: &[u8],
    at: usize,
    amount: i64,
    stats: &mut Stats,
) -> Result<(), CoreError> {
    for j in 0..28 {
        let av = i64::from(read_u16(a, at + 2 * j)?);
        let bv = i64::from(read_u16(b, at + 2 * j)?);
        let v = if j % 14 < 9 {
            let mixed = rd(
                add(
                    mul(100 - amount, dec(av, 2048))?,
                    mul(amount, dec(bv, 2048))?,
                )?,
                100,
            )?;
            let lim = if j % 14 < 3 { 2047 } else { 1279 };
            enc(clamp(mixed, -lim, lim, stats), 2048)?
        } else {
            let mixed = rd(add(mul(100 - amount, av)?, mul(amount, bv)?)?, 100)?;
            clamp(mixed, 0, 4095, stats)
        };
        write_u16(out, at + 2 * j, v)?;
    }
    Ok(())
}

fn blend_curve(
    out: &mut [u8],
    a: &[u8],
    b: &[u8],
    at: usize,
    length: usize,
    amount: i64,
    stats: &mut Stats,
) -> Result<(), CoreError> {
    let aa = if matches!(at, 82 | 379 | 412) {
        20
    } else if at == 346 {
        284
    } else {
        456
    };
    let xs = knots(b, aa)?;
    let mut prev = 0i64;
    for j in 0..length {
        let av = interp_q16(&a[aa..aa + 62], &a[at..at + 33], xs[j])?;
        let numer = add(
            mul(100 - amount, av)?,
            mul(mul(amount, i64::from(b[at + j]))?, 65536)?,
        )?;
        let mut v = clamp(rd(numer, 100 * 65536)?, 0, 255, stats);
        if v < prev {
            stats.monotone_repairs += 1;
            v = prev;
        }
        put_u8(out, at + j, v)?;
        prev = v;
    }
    Ok(())
}

/// Port of gxr_studio/engine/knobs.py:108 limit_slopes
fn limit_slopes(b: &mut [u8], stats: &mut Stats) -> Result<(), CoreError> {
    let mut pairs = vec![(20usize, 82usize), (456, 518)];
    if b[1] & 16 != 0 {
        pairs.push((284, 346));
    }
    if b[1] & 4 != 0 {
        pairs.push((20, 379));
        pairs.push((20, 412));
    }
    for (aa, bb) in pairs {
        let xs = knots(b, aa)?;
        for j in 1..33 {
            let cap = add(i64::from(b[bb + j - 1]), xs[j] - xs[j - 1])?;
            if i64::from(b[bb + j]) > cap {
                put_u8(b, bb + j, cap)?;
                stats.slope_repairs += 1;
            }
        }
    }
    Ok(())
}

/// Port of gxr_studio/engine/knobs.py:119 warp
fn warp(y: i64, lo: i64, hi: i64, s: i64) -> Result<i64, CoreError> {
    if s == 0 || y <= lo || y >= hi {
        return Ok(y);
    }
    let t = y - lo;
    let length = hi - lo;
    let abs_s = s.checked_neg().map_or(s, |n| if s < 0 { n } else { s });
    let j = abs_s / 64;
    let rem = abs_s % 64;
    let gain = if j == 256 {
        EXP_Q12[256]
    } else {
        let step = EXP_Q12[usize::try_from(j + 1).map_err(|_| overflow())?]
            - EXP_Q12[usize::try_from(j).map_err(|_| overflow())?];
        add(
            EXP_Q12[usize::try_from(j).map_err(|_| overflow())?],
            rd(mul(step, rem)?, 64)?,
        )?
    };
    let (n, d) = if s > 0 { (gain, Q) } else { (Q, gain) };
    let numer = mul(mul(length, n)?, t)?;
    let denom = add(mul(d, length - t)?, mul(n, t)?)?;
    add(lo, rd(numer, denom)?)
}

/// Port of gxr_studio/engine/knobs.py:128 tone_curve
fn tone_curve(curve: &[u8], doses: &[i64; 10]) -> Result<Vec<u8>, CoreError> {
    let (c, h, s, f) = (doses[0], doses[1], doses[2], doses[3]);
    let mut out = Vec::with_capacity(curve.len());
    for &byte in curve {
        let mut y = i64::from(byte);
        if c != 0 {
            y = if y <= 128 {
                warp(y, 0, 128, -c)?
            } else {
                warp(y, 128, 255, c)?
            };
        }
        if h != 0 {
            y = warp(y, 112, 255, h)?;
        }
        if s != 0 {
            y = warp(y, 0, 144, s)?;
        }
        if f != 0 {
            let black = rd(mul(f.abs(), 80)?, Q)?;
            y = rd(add(mul(black, 255)?, mul(255 - black, y)?)?, 255)?;
        }
        out.push(u8::try_from(y).map_err(|_| value("bytes must be in range(0, 256)"))?);
    }
    Ok(out)
}

/// Port of gxr_studio/engine/knobs.py:157 store_matrix
fn store_matrix(b: &mut [u8], w: &[i64; 28]) -> Result<(), CoreError> {
    for (i, &word) in w.iter().enumerate() {
        write_u16(b, 216 + 2 * i, enc(word, 2048)?)?;
    }
    b[1] |= 1;
    Ok(())
}

/// Port of gxr_studio/engine/knobs.py:141 sat
fn sat(b: &mut [u8], dose: i64, stats: &mut Stats) -> Result<(), CoreError> {
    if dose == 0 {
        return Ok(());
    }
    let mut w = words(b, 216)?;
    let idx: Vec<usize> = (0..28).filter(|i| (3..9).contains(&(i % 14))).collect();
    let wanted = if dose > 0 {
        add(Q, mul(3, dose)?)?
    } else {
        rd(mul(Q, Q)?, add(Q, mul(3, dose.abs())?)?)?
    };
    let cm = max_of(b[115..211].iter().map(|v| i64::from(v & 127)))?;
    let mm = max_of(idx.iter().map(|&i| w[i].abs()))?;
    let cg = if cm == 0 {
        wanted
    } else {
        wanted.min(floor_div(mul(127, Q)?, cm)?)
    };
    let residual = rd(mul(wanted, Q)?, cg)?;
    let mg = if mm == 0 {
        residual
    } else {
        residual.min(floor_div(mul(1279, Q)?, mm)?)
    };
    stats.colour_cap += i64::from(cg < wanted);
    stats.matrix_cap += i64::from(mg < residual);
    for j in 0..96 {
        let old = i64::from(b[115 + j]);
        let mag = rd(mul(old & 127, cg)?, Q)?;
        put_u8(b, 115 + j, (old & 128) | mag)?;
    }
    if mg != Q {
        for i in idx {
            let sign = if w[i] >= 0 { 1 } else { -1 };
            w[i] = mul(sign, rd(mul(w[i].abs(), mg)?, Q)?)?;
        }
        store_matrix(b, &w)?;
    }
    Ok(())
}

fn max_of(values: impl Iterator<Item = i64>) -> Result<i64, CoreError> {
    let mut best: Option<i64> = None;
    for v in values {
        best = Some(best.map_or(v, |b| b.max(v)));
    }
    best.ok_or_else(|| value("entry length"))
}

fn first_max(values: &[i64]) -> usize {
    let mut best_i = 0;
    let mut best = values[0];
    for (i, &v) in values.iter().enumerate().skip(1) {
        if v > best {
            best = v;
            best_i = i;
        }
    }
    best_i
}

/// Port of gxr_studio/engine/knobs.py:182 filter_weights
fn filter_weights(y: [i64; 3], dose: i64) -> Result<[i64; 3], CoreError> {
    if dose == 0 {
        return Ok(y);
    }
    let anchors: [[i64; 3]; 4] = [y, [420, 580, 24], [700, 300, 24], [930, 80, 14]];
    let (a, b, t, den) = if dose < 0 {
        (y, [96, 896, 32], -dose, Q)
    } else {
        let p = mul(dose, 3)?;
        let seg = 2.min(floor_div(p, Q)?);
        let seg_u = usize::try_from(seg).map_err(|_| overflow())?;
        (anchors[seg_u], anchors[seg_u + 1], p - seg * Q, Q)
    };
    let mut out = [0; 3];
    for i in 0..3 {
        out[i] = rd(add(mul(den - t, a[i])?, mul(t, b[i])?)?, den)?;
    }
    let j = first_max(&out);
    let sum = out[0] + out[1] + out[2];
    out[j] = add(out[j], 1024 - sum)?;
    Ok(out)
}

/// Port of gxr_studio/engine/knobs.py:194 bw_filter
fn bw_filter(b: &mut [u8], dose: i64) -> Result<(), CoreError> {
    if dose == 0 {
        return Ok(());
    }
    let mut w = words(b, 216)?;
    for half in [0usize, 14] {
        let y = [w[half], w[half + 1], w[half + 2]];
        let next = filter_weights(y, dose)?;
        w[half..half + 3].copy_from_slice(&next);
    }
    store_matrix(b, &w)
}

/// Port of gxr_studio/engine/knobs.py:162 add_rowsum
fn add_rowsum(b: &mut [u8], cb: i64, cr: i64, stats: &mut Stats) -> Result<(), CoreError> {
    if cb == 0 && cr == 0 {
        return Ok(());
    }
    let mut w = words(b, 216)?;
    let mut deltas = [0i64; 28];
    for half in [0usize, 14] {
        let y = [w[half], w[half + 1], w[half + 2]];
        let ys = y[0] + y[1] + y[2];
        if ys <= 0 {
            return Err(value("positive Y sum required"));
        }
        for (row, amount) in [(3usize, cb), (6, cr)] {
            let mut dd = [0i64; 3];
            for k in 0..3 {
                dd[k] = rd(mul(amount, y[k])?, ys)?;
            }
            let j = first_max(&[y[0].abs(), y[1].abs(), y[2].abs()]);
            dd[j] = add(dd[j], amount - (dd[0] + dd[1] + dd[2]))?;
            for k in 0..3 {
                deltas[half + row + k] = dd[k];
            }
        }
    }
    let mut alpha = Q;
    for (i, &d) in deltas.iter().enumerate() {
        if d > 0 {
            alpha = alpha.min(floor_div(mul(1279 - w[i], Q)?, d)?);
        } else if d < 0 {
            alpha = alpha.min(floor_div(mul(-1279 - w[i], Q)?, d)?);
        }
    }
    alpha = alpha.clamp(0, Q);
    if alpha < Q {
        stats.rowsum_cap += 1;
    }
    for (i, &d) in deltas.iter().enumerate() {
        w[i] = add(w[i], rd(mul(d, alpha)?, Q)?)?;
    }
    store_matrix(b, &w)
}

/// Port of gxr_studio/engine/knobs.py:201 t1
fn t1(
    b: &mut [u8],
    warmth: i64,
    tint: i64,
    split: i64,
    stats: &mut Stats,
) -> Result<(), CoreError> {
    if warmth == 0 && tint == 0 && split == 0 {
        return Ok(());
    }
    if b[1] & 2 == 0 {
        let curve = b[82..115].to_vec();
        b[379..412].copy_from_slice(&curve);
        b[412..445].copy_from_slice(&curve);
    }
    b[1] |= 6;
    write_u16(b, 445, 0)?;
    for (at, sign) in [(379usize, 1i64), (412, -1)] {
        for j in 0..33 {
            let mut y = i64::from(b[at + j]);
            let gain = add(Q, rd(mul(3, warmth.abs())?, 5)?)?;
            if warmth * sign > 0 {
                y = rd(mul(y, gain)?, Q)?;
            } else if warmth * sign < 0 {
                y = rd(mul(y, Q)?, gain)?;
            }
            let gain = add(Q, rd(tint.abs(), 2)?)?;
            if tint > 0 {
                y = rd(mul(y, gain)?, Q)?;
            } else if tint < 0 {
                y = rd(mul(y, Q)?, gain)?;
            }
            y = clamp(y, 0, 255, stats);
            if split != 0 {
                y = if y <= 128 {
                    warp(y, 0, 128, -sign * split)?
                } else {
                    warp(y, 128, 255, sign * split)?
                };
            }
            put_u8(b, at + j, clamp(y, 0, 255, stats))?;
        }
    }
    Ok(())
}

fn write_curve(b: &mut [u8], at: usize, curve: &[u8]) {
    b[at..at + curve.len()].copy_from_slice(curve);
}

/// Port of gxr_studio/engine/knobs.py:219 apply
pub fn apply(stock: &[u8], base: &[u8], request: &Request) -> Result<(Vec<u8>, Stats), CoreError> {
    request.validate()?;
    if stock.len() != 552 || base.len() != 552 {
        return Err(value("entry length"));
    }
    let mut stats = Stats::new();
    let doses = request.doses;
    if request.amount == 100 && doses.iter().all(|&d| d == 0) {
        return Ok((base.to_vec(), stats));
    }
    let mut b = blend(stock, base, request.amount, &mut stats)?;
    if b[1] & 1 == 0 {
        b[216..272].copy_from_slice(&stock[216..272]);
    }
    if doses[..4].iter().any(|&d| d != 0) {
        for at in [82usize, 518] {
            let curve = tone_curve(&b[at..at + 33], &doses)?;
            write_curve(&mut b, at, &curve);
        }
        if b[1] & 16 != 0 {
            let curve = tone_curve(&b[346..379], &doses)?;
            write_curve(&mut b, 346, &curve);
        }
        if b[1] & 2 != 0 {
            for at in [379usize, 412] {
                let curve = tone_curve(&b[at..at + 33], &doses)?;
                write_curve(&mut b, at, &curve);
            }
        }
    }
    if doses[4] != 0 {
        sat(&mut b, doses[4], &mut stats)?;
    }
    if doses[8] != 0 {
        bw_filter(&mut b, doses[8])?;
    }
    let (warm, tint) = if request.wb_method == 0 {
        let cb = rd(add(mul(-140, doses[5])?, mul(100, doses[6])?)?, Q)?;
        let cr = rd(add(mul(180, doses[5])?, mul(110, doses[6])?)?, Q)?;
        add_rowsum(&mut b, cb, cr, &mut stats)?;
        (0, 0)
    } else {
        (doses[5], doses[6])
    };
    if doses[9] != 0 {
        if request.tone_method == 0 {
            let cb = rd(mul(-128, doses[9])?, Q)?;
            let cr = rd(mul(128, doses[9])?, Q)?;
            add_rowsum(&mut b, cb, cr, &mut stats)?;
        } else {
            let mut w = words(&b, 216)?;
            for half in [0usize, 14] {
                w[half + 10] = clamp(
                    add(w[half + 10], rd(mul(-8, doses[9])?, Q)?)?,
                    120,
                    136,
                    &mut stats,
                );
                w[half + 11] = clamp(
                    add(w[half + 11], rd(mul(8, doses[9])?, Q)?)?,
                    120,
                    136,
                    &mut stats,
                );
            }
            store_matrix(&mut b, &w)?;
        }
    }
    t1(&mut b, warm, tint, doses[7], &mut stats)?;
    limit_slopes(&mut b, &mut stats)?;
    checksum(&mut b)?;
    Ok((b, stats))
}

/// Port of gxr_studio/engine/knobs.py:254 request_for
pub fn request_for(
    param: &str,
    dose: i64,
    amount: i64,
    wb_method: i64,
    tone_method: i64,
) -> Result<Request, CoreError> {
    let mut doses = [0; 10];
    if param != "amount" {
        let index = PARAMS
            .iter()
            .position(|name| *name == param)
            .ok_or_else(|| value("param"))?;
        doses[index] = dose;
    }
    Ok(Request {
        amount,
        doses,
        wb_method,
        tone_method,
    })
}
