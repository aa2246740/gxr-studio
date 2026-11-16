//! NumPy 2.4.3 `default_rng(seed)`: `SeedSequence`, `PCG64`, and Ziggurat `normal` / `uniform` / `choice`.

use crate::ink::rng_tables::{FI_DOUBLE, KI_DOUBLE, WI_DOUBLE, ZIGGURAT_NOR_INV_R, ZIGGURAT_NOR_R};

const INIT_A: u32 = 0x43b0_d7e5;
const MULT_A: u32 = 0x931e_8875;
const INIT_B: u32 = 0x8b51_f9dd;
const MULT_B: u32 = 0x58f3_8ded;
const MIX_MULT_L: u32 = 0xca01_f9dd;
const MIX_MULT_R: u32 = 0x4973_f715;
const XSHIFT: u32 = 16;

const PCG_DEFAULT_MULTIPLIER: u128 =
    (2_549_297_995_355_413_924_u128 << 64) | 4_865_540_595_714_422_341_u128;

#[inline]
fn hashmix(value: u32, hash_const: &mut u32) -> u32 {
    let mut v = value ^ *hash_const;
    *hash_const = hash_const.wrapping_mul(MULT_A);
    v = v.wrapping_mul(*hash_const);
    v ^ (v >> XSHIFT)
}

#[inline]
fn mix(x: u32, y: u32) -> u32 {
    let r = MIX_MULT_L
        .wrapping_mul(x)
        .wrapping_sub(MIX_MULT_R.wrapping_mul(y));
    r ^ (r >> XSHIFT)
}

/// Port of NumPy `SeedSequence(seed)` with `pool_size=4`, `spawn_key=()`.
pub struct SeedSequence {
    pool: [u32; 4],
}

impl SeedSequence {
    #[must_use]
    pub fn from_u64(mut entropy: u64) -> Self {
        let mut entropy_words = Vec::with_capacity(2);
        if entropy == 0 {
            entropy_words.push(0);
        } else {
            while entropy > 0 {
                entropy_words.push((entropy & 0xffff_ffff) as u32);
                entropy >>= 32;
            }
        }
        let mut pool = [0_u32; 4];
        let mut hash_const = INIT_A;
        for (i, slot) in pool.iter_mut().enumerate() {
            let val = entropy_words.get(i).copied().unwrap_or(0);
            *slot = hashmix(val, &mut hash_const);
        }
        for i_src in 0..4 {
            for i_dst in 0..4 {
                if i_src != i_dst {
                    let mixed = hashmix(pool[i_src], &mut hash_const);
                    pool[i_dst] = mix(pool[i_dst], mixed);
                }
            }
        }
        for &ent in entropy_words.iter().skip(4) {
            for slot in &mut pool {
                let mixed = hashmix(ent, &mut hash_const);
                *slot = mix(*slot, mixed);
            }
        }
        Self { pool }
    }

    #[must_use]
    pub fn generate_state_u64_4(&self) -> [u64; 4] {
        let mut words = [0_u32; 8];
        let mut hash_const = INIT_B;
        for (i_dst, slot) in words.iter_mut().enumerate() {
            let mut data_val = self.pool[i_dst % 4];
            data_val ^= hash_const;
            hash_const = hash_const.wrapping_mul(MULT_B);
            data_val = data_val.wrapping_mul(hash_const);
            data_val ^= data_val >> XSHIFT;
            *slot = data_val;
        }
        [
            u64::from(words[0]) | (u64::from(words[1]) << 32),
            u64::from(words[2]) | (u64::from(words[3]) << 32),
            u64::from(words[4]) | (u64::from(words[5]) << 32),
            u64::from(words[6]) | (u64::from(words[7]) << 32),
        ]
    }
}

/// Port of NumPy `Generator(PCG64(seed))` (XSL RR 128/64 + Ziggurat normal).
#[derive(Clone, Debug)]
pub struct Pcg64 {
    state: u128,
    inc: u128,
    has_uint32: bool,
    uinteger: u32,
}

impl Pcg64 {
    #[must_use]
    pub fn from_seed(seed: u64) -> Self {
        let s4 = SeedSequence::from_u64(seed).generate_state_u64_4();
        let initstate = (u128::from(s4[0]) << 64) | u128::from(s4[1]);
        let initseq = (u128::from(s4[2]) << 64) | u128::from(s4[3]);
        let mut rng = Self {
            state: 0,
            inc: (initseq << 1) | 1,
            has_uint32: false,
            uinteger: 0,
        };
        rng.step();
        rng.state = rng.state.wrapping_add(initstate);
        rng.step();
        rng
    }

    #[inline]
    fn step(&mut self) {
        self.state = self
            .state
            .wrapping_mul(PCG_DEFAULT_MULTIPLIER)
            .wrapping_add(self.inc);
    }

    #[inline]
    pub fn next_u64(&mut self) -> u64 {
        self.step();
        let hi = (self.state >> 64) as u64;
        let lo = self.state as u64;
        let rot = (self.state >> 122) as u32;
        (hi ^ lo).rotate_right(rot)
    }

    #[inline]
    pub fn next_u32(&mut self) -> u32 {
        if self.has_uint32 {
            self.has_uint32 = false;
            return self.uinteger;
        }
        let next = self.next_u64();
        self.has_uint32 = true;
        self.uinteger = (next >> 32) as u32;
        next as u32
    }

    #[inline]
    pub fn next_double(&mut self) -> f64 {
        ((self.next_u64() >> 11) as f64) * (1.0_f64 / 9_007_199_254_740_992.0_f64)
    }

    /// Port of NumPy `distributions.c:random_standard_normal`.
    pub fn standard_normal(&mut self) -> f64 {
        loop {
            let mut r = self.next_u64();
            let idx = (r & 0xff) as usize;
            r >>= 8;
            let sign = r & 0x1;
            let rabs = (r >> 1) & 0x000f_ffff_ffff_ffff;
            let mut x = (rabs as f64) * WI_DOUBLE[idx];
            if sign != 0 {
                x = -x;
            }
            if rabs < KI_DOUBLE[idx] {
                return x;
            }
            if idx == 0 {
                loop {
                    let xx = -ZIGGURAT_NOR_INV_R * gxr_math::log1p(-self.next_double());
                    let yy = -gxr_math::log1p(-self.next_double());
                    if yy + yy > xx * xx {
                        return if ((rabs >> 8) & 0x1) != 0 {
                            -(ZIGGURAT_NOR_R + xx)
                        } else {
                            ZIGGURAT_NOR_R + xx
                        };
                    }
                }
            } else if ((FI_DOUBLE[idx - 1] - FI_DOUBLE[idx]) * self.next_double() + FI_DOUBLE[idx])
                < gxr_math::exp(-0.5 * x * x)
            {
                return x;
            }
        }
    }

    #[inline]
    pub fn normal(&mut self, loc: f64, scale: f64) -> f64 {
        loc + scale * self.standard_normal()
    }

    #[inline]
    pub fn uniform(&mut self, low: f64, high: f64) -> f64 {
        low + (high - low) * self.next_double()
    }

    /// Port of `rng.integers(0, rng_excl, dtype=int64)` for `0 < rng < u32::MAX` (`Lemire`).
    pub fn bounded_u32(&mut self, rng: u32) -> u32 {
        let rng_excl = u64::from(rng) + 1;
        let mut m = u64::from(self.next_u32()) * rng_excl;
        let mut leftover = (m & 0xffff_ffff) as u32;
        if u64::from(leftover) < rng_excl {
            let threshold = ((u32::MAX - rng) as u64 % rng_excl) as u32;
            while leftover < threshold {
                m = u64::from(self.next_u32()) * rng_excl;
                leftover = (m & 0xffff_ffff) as u32;
            }
        }
        (m >> 32) as u32
    }

    #[inline]
    pub fn choice_two(&mut self, a: f64, b: f64) -> f64 {
        if self.bounded_u32(1) == 0 { a } else { b }
    }
}
