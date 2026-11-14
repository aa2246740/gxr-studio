//! pocketfft's `fftblue<T>` (Bluestein over a `cfftp` of `good_size_cmplx(2n-1)`), the
//! `util` helpers it needs, and `pocketfft_c`'s choice between the two.

use gxr_raw::CoreError;

use super::cfftp::Cfftp;
use super::cx::{Cx, Real, special_mul};
use super::twiddle::{SinCos, Trig};
use crate::size;

/// `util::largest_prime_factor`.
pub fn largest_prime_factor(mut n: u64) -> u64 {
    let mut res = 1;
    while n & 1 == 0 {
        res = 2;
        n >>= 1;
    }
    let mut x = 3u64;
    while x * x <= n {
        while n.is_multiple_of(x) {
            res = x;
            n /= x;
        }
        x += 2;
    }
    if n > 1 {
        res = n;
    }
    res
}

/// `util::cost_guess` (double arithmetic, the 1.1 penalty for factors above 5).
pub fn cost_guess(mut n: u64) -> f64 {
    const LFP: f64 = 1.1;
    let ni = n;
    let mut result = 0.0f64;
    while n & 1 == 0 {
        result += 2.0;
        n >>= 1;
    }
    let mut x = 3u64;
    while x * x <= n {
        while n.is_multiple_of(x) {
            result += if x <= 5 { x as f64 } else { LFP * x as f64 };
            n /= x;
        }
        x += 2;
    }
    if n > 1 {
        result += if n <= 5 { n as f64 } else { LFP * n as f64 };
    }
    result * ni as f64
}

/// `util::good_size_cmplx`: the smallest `2^a 3^b 5^c 7^d 11^e >= n`.
pub fn good_size_cmplx(n: u64) -> u64 {
    if n <= 12 {
        return n;
    }
    let mut bestfac = 2 * n;
    let mut f11 = 1u64;
    while f11 < bestfac {
        let mut f117 = f11;
        while f117 < bestfac {
            let mut f1175 = f117;
            while f1175 < bestfac {
                let mut x = f1175;
                while x < n {
                    x *= 2;
                }
                loop {
                    if x < n {
                        x *= 3;
                    } else if x > n {
                        if x < bestfac {
                            bestfac = x;
                        }
                        if x & 1 == 1 {
                            break;
                        }
                        x >>= 1;
                    } else {
                        return n;
                    }
                }
                f1175 *= 5;
            }
            f117 *= 7;
        }
        f11 *= 11;
    }
    bestfac
}

/// `pocketfft_c(length)`: Bluestein when the largest prime factor is large and the
/// estimated cost of the padded transform, times 1.5, is lower.
pub fn use_bluestein(length: u64) -> bool {
    let tmp = if length < 50 {
        0
    } else {
        largest_prime_factor(length)
    };
    if u128::from(tmp) * u128::from(tmp) <= u128::from(length) {
        return false;
    }
    let comp1 = cost_guess(length);
    let mut comp2 = 2.0 * cost_guess(good_size_cmplx(2 * length - 1));
    comp2 *= 1.5; // fudge factor that appears to give good overall performance
    comp2 < comp1
}

pub struct Blue<T> {
    n: usize,
    n2: usize,
    plan: Cfftp<T>,
    bk: Vec<Cx<T>>,
    bkf: Vec<Cx<T>>,
}

impl<T: Real> Blue<T> {
    /// `fftblue(length)`.
    pub fn new(n: usize, trig: Trig) -> Result<Self, CoreError> {
        let n64 = size::dim(n);
        let n2 = size::count(&[good_size_cmplx(2 * n64 - 1)])?;
        let plan = Cfftp::new(n2, trig)?;
        // initialize b_k
        let tmp = SinCos::new(2 * n64, trig)?;
        let mut bk = size::with_capacity(n)?;
        bk.push(Cx::new(T::ONE, T::ZERO));
        let mut coeff = 0u64;
        for m in 1..n64 {
            coeff += 2 * m - 1;
            if coeff >= 2 * n64 {
                coeff -= 2 * n64;
            }
            bk.push(tmp.get(coeff));
        }
        // initialize the zero-padded, Fourier transformed b_k. Add normalisation.
        let xn2 = T::recip_count(n2);
        let mut tbkf = size::filled(n2, Cx::ZERO)?;
        tbkf[0] = bk[0].scale(xn2);
        for m in 1..n {
            let v = bk[m].scale(xn2);
            tbkf[m] = v;
            tbkf[n2 - m] = v;
        }
        let mut scratch = size::filled(n2, Cx::ZERO)?;
        plan.exec(&mut tbkf, &mut scratch, T::ONE, true);
        tbkf.truncate(n2 / 2 + 1);
        Ok(Self {
            n,
            n2,
            plan,
            bk,
            bkf: tbkf,
        })
    }

    /// `fftblue::fft<fwd>(c, fct)`; `akf` and `scratch` hold `n2` values each.
    pub fn exec(
        &self,
        c: &mut [Cx<T>],
        akf: &mut [Cx<T>],
        scratch: &mut [Cx<T>],
        fct: T,
        fwd: bool,
    ) {
        if fwd {
            self.fft::<true>(c, akf, scratch, fct);
        } else {
            self.fft::<false>(c, akf, scratch, fct);
        }
    }

    fn fft<const F: bool>(
        &self,
        c: &mut [Cx<T>],
        akf: &mut [Cx<T>],
        scratch: &mut [Cx<T>],
        fct: T,
    ) {
        let (n, n2) = (self.n, self.n2);
        // initialize a_k and FFT it
        for m in 0..n {
            akf[m] = special_mul::<F, T>(c[m], self.bk[m]);
        }
        // `akf[0]*T0(0)`: -0 or NaN follow akf[0].
        let zero = akf[0].scale(T::ZERO);
        for v in &mut akf[n..n2] {
            *v = zero;
        }
        self.plan.exec(akf, scratch, T::ONE, true);
        // do the convolution: `akf[m].special_mul<!fwd>(bkf[m])`
        let conv = |a: Cx<T>, b: Cx<T>| {
            if F {
                special_mul::<false, T>(a, b)
            } else {
                special_mul::<true, T>(a, b)
            }
        };
        akf[0] = conv(akf[0], self.bkf[0]);
        for m in 1..n2.div_ceil(2) {
            akf[m] = conv(akf[m], self.bkf[m]);
            akf[n2 - m] = conv(akf[n2 - m], self.bkf[m]);
        }
        if n2 & 1 == 0 {
            akf[n2 / 2] = conv(akf[n2 / 2], self.bkf[n2 / 2]);
        }
        // inverse FFT
        self.plan.exec(akf, scratch, T::ONE, false);
        // multiply by b_k
        for m in 0..n {
            c[m] = special_mul::<F, T>(akf[m], self.bk[m]).scale(fct);
        }
    }

    pub fn n2(&self) -> usize {
        self.n2
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plan_choice_follows_pocketfft() {
        assert_eq!(good_size_cmplx(97), 98);
        assert_eq!(good_size_cmplx(177), 180);
        assert_eq!(good_size_cmplx(1423), 1440);
        let blue: Vec<u64> = (1..200).filter(|&n| use_bluestein(n)).collect();
        assert_eq!(blue[..6], [89, 101, 103, 107, 113, 127]);
        assert!(!use_bluestein(53) && !use_bluestein(97) && use_bluestein(1031));
        assert!(!use_bluestein(712) && !use_bluestein(1072));
        assert_eq!(largest_prime_factor(1072), 67);
    }
}
