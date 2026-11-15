//! Shortest exact decimal representation matching CPython `repr(float)` (David Gay `dtoa(mode=0)`).
//!
//! Unlike Rust's `flt2dec` (which breaks exact decimal midpoints upward), CPython breaks
//! exact midpoints in the last decimal digit (`2 * rem == scale`) toward an **even** digit.

use std::cmp::Ordering;

/// Returns `(digits, exp10)` for a finite positive `abs_f > 0.0` where `digits` has elements in
/// `0..=9` with `digits[0] != 0` and trailing zeros trimmed, representing
/// `digits[0].digits[1..] * 10^exp10`.
pub(crate) fn shortest_decimal_digits(abs_f: f64) -> (Vec<u8>, i32) {
    debug_assert!(abs_f.is_finite() && abs_f > 0.0);
    let bits = abs_f.to_bits();
    let exp_bits = ((bits >> 52) & 0x7FF) as i32;
    let frac_bits = bits & ((1u64 << 52) - 1);

    let (sig, e2) = if exp_bits == 0 {
        (frac_bits, 1 - 1023 - 52)
    } else {
        ((1u64 << 52) | frac_bits, exp_bits - 1023 - 52)
    };

    let accept_bounds = (sig & 1) == 0;
    let is_pow2_boundary = frac_bits == 0 && exp_bits > 1;
    let shift: u32 = if is_pow2_boundary { 2 } else { 1 };

    let mut r = BigUint::from_u64(sig << shift);
    let mut s = BigUint::from_u64(1);
    let mut mp = BigUint::from_u64(u64::from(shift));
    let mut mm = BigUint::from_u64(1);

    let e_scale = e2 - (shift as i32);
    if e_scale >= 0 {
        let sh = e_scale as u32;
        r = r.shl(sh);
        mp = mp.shl(sh);
        mm = mm.shl(sh);
    } else {
        s = s.shl((-e_scale) as u32);
    }

    let bit_len = (64 - sig.leading_zeros()) as i32;
    let log2_int = e2 + bit_len - 1;
    #[allow(clippy::cast_possible_truncation)]
    let mut k: i32 = ((i64::from(log2_int) * 301_030).div_euclid(1_000_000) as i32) + 1;
    if k >= 0 {
        s.mul_pow10(k as u32);
    } else {
        let p = (-k) as u32;
        r.mul_pow10(p);
        mp.mul_pow10(p);
        mm.mul_pow10(p);
    }

    let too_high = |r_val: &BigUint, mp_val: &BigUint, s_val: &BigUint| -> bool {
        let upper = r_val.add(mp_val);
        if accept_bounds {
            upper.cmp(s_val) != Ordering::Less
        } else {
            upper.cmp(s_val) == Ordering::Greater
        }
    };

    while too_high(&r, &mp, &s) {
        s.mul_u32_in_place(10);
        k += 1;
    }

    loop {
        let r10 = r.mul_u32(10);
        let mp10 = mp.mul_u32(10);
        if too_high(&r10, &mp10, &s) {
            break;
        }
        r = r10;
        mp = mp10;
        mm.mul_u32_in_place(10);
        k -= 1;
    }

    let mut digits = Vec::with_capacity(18);
    loop {
        r.mul_u32_in_place(10);
        mp.mul_u32_in_place(10);
        mm.mul_u32_in_place(10);

        let (q, rem) = div_rem_digit(&r, &s);
        r = rem;

        let tc1 = if accept_bounds {
            r.cmp(&mm) != Ordering::Greater
        } else {
            r.cmp(&mm) == Ordering::Less
        };
        let tc2 = too_high(&r, &mp, &s);

        if !tc1 && !tc2 {
            digits.push(q);
            continue;
        }

        let last = match (tc1, tc2) {
            (true, false) => q,
            (false, true) => q + 1,
            (true, true) => {
                let two_r = r.mul_u32(2);
                match two_r.cmp(&s) {
                    Ordering::Less => q,
                    Ordering::Greater => q + 1,
                    Ordering::Equal => {
                        if q.is_multiple_of(2) {
                            q
                        } else {
                            q + 1
                        }
                    }
                }
            }
            (false, false) => unreachable!(),
        };
        digits.push(last);
        break;
    }

    while digits.len() > 1 && digits.last() == Some(&0) {
        digits.pop();
    }

    (digits, k - 1)
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct BigUint {
    limbs: Vec<u32>,
}

impl BigUint {
    fn from_u64(v: u64) -> Self {
        let lo = v as u32;
        let hi = (v >> 32) as u32;
        if hi != 0 {
            Self {
                limbs: vec![lo, hi],
            }
        } else if lo != 0 {
            Self { limbs: vec![lo] }
        } else {
            Self { limbs: Vec::new() }
        }
    }

    fn trim(&mut self) {
        while self.limbs.last() == Some(&0) {
            self.limbs.pop();
        }
    }

    fn shl(&self, bits: u32) -> Self {
        if self.limbs.is_empty() || bits == 0 {
            return self.clone();
        }
        let word_shift = (bits / 32) as usize;
        let bit_shift = bits % 32;
        let mut out = Vec::with_capacity(word_shift + self.limbs.len() + 1);
        out.resize(word_shift, 0);
        if bit_shift == 0 {
            out.extend_from_slice(&self.limbs);
        } else {
            let mut carry = 0u32;
            for &limb in &self.limbs {
                out.push((limb << bit_shift) | carry);
                carry = limb >> (32 - bit_shift);
            }
            if carry != 0 {
                out.push(carry);
            }
        }
        Self { limbs: out }
    }

    fn mul_u32_in_place(&mut self, m: u32) {
        if self.limbs.is_empty() || m == 1 {
            return;
        }
        if m == 0 {
            self.limbs.clear();
            return;
        }
        let m64 = u64::from(m);
        let mut carry = 0u64;
        for limb in &mut self.limbs {
            let prod = u64::from(*limb) * m64 + carry;
            *limb = prod as u32;
            carry = prod >> 32;
        }
        if carry != 0 {
            self.limbs.push(carry as u32);
        }
    }

    fn mul_u32(&self, m: u32) -> Self {
        let mut out = self.clone();
        out.mul_u32_in_place(m);
        out
    }

    fn mul_pow10(&mut self, mut exp: u32) {
        const TEN_9: u32 = 1_000_000_000;
        while exp >= 9 {
            self.mul_u32_in_place(TEN_9);
            exp -= 9;
        }
        if exp > 0 {
            self.mul_u32_in_place(10u32.pow(exp));
        }
    }

    fn add(&self, other: &Self) -> Self {
        let n = self.limbs.len().max(other.limbs.len());
        let mut out = Vec::with_capacity(n + 1);
        let mut carry = 0u64;
        for i in 0..n {
            let a = u64::from(*self.limbs.get(i).unwrap_or(&0));
            let b = u64::from(*other.limbs.get(i).unwrap_or(&0));
            let sum = a + b + carry;
            out.push(sum as u32);
            carry = sum >> 32;
        }
        if carry != 0 {
            out.push(carry as u32);
        }
        Self { limbs: out }
    }

    fn sub(&self, other: &Self) -> Self {
        let mut out = Vec::with_capacity(self.limbs.len());
        let mut borrow = 0i64;
        for i in 0..self.limbs.len() {
            let a = i64::from(self.limbs[i]);
            let b = i64::from(*other.limbs.get(i).unwrap_or(&0));
            let mut diff = a - b - borrow;
            if diff < 0 {
                diff += 1i64 << 32;
                borrow = 1;
            } else {
                borrow = 0;
            }
            out.push(diff as u32);
        }
        let mut res = Self { limbs: out };
        res.trim();
        res
    }

    fn cmp(&self, other: &Self) -> Ordering {
        match self.limbs.len().cmp(&other.limbs.len()) {
            Ordering::Equal => {
                for (a, b) in self.limbs.iter().rev().zip(other.limbs.iter().rev()) {
                    match a.cmp(b) {
                        Ordering::Equal => continue,
                        ord => return ord,
                    }
                }
                Ordering::Equal
            }
            ord => ord,
        }
    }
}

fn div_rem_digit(num: &BigUint, den: &BigUint) -> (u8, BigUint) {
    let mut lo = 0u8;
    let mut hi = 9u8;
    while lo < hi {
        let mid = (lo + hi).div_ceil(2);
        let prod = den.mul_u32(u32::from(mid));
        if num.cmp(&prod) != Ordering::Less {
            lo = mid;
        } else {
            hi = mid - 1;
        }
    }
    if lo == 0 {
        (0, num.clone())
    } else {
        let prod = den.mul_u32(u32::from(lo));
        (lo, num.sub(&prod))
    }
}
