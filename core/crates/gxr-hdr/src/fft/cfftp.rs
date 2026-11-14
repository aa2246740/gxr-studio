//! pocketfft's `cfftp<T>`: the mixed-radix complex FFT (FFTPACK layout), `T` float or double.
//!
//! Factors come out as `factorize` orders them (8s, then 4s, a 2 swapped to the front, then
//! odd primes in increasing order), each pass reads one buffer and writes the other, and the
//! scale `fct` is applied at the end exactly as `pass_all` does.

use gxr_raw::CoreError;

use super::alloc;
use super::cx::{Cx, Real};
use super::twiddle::{SinCos, Trig};
use super::{passes, passg};
use crate::size;

struct Factor<T> {
    fct: usize,
    /// `(ip-1)*(ido-1)` twiddles, `tw[(j-1)*(ido-1)+i-1] = w[j*l1*i]`.
    tw: Vec<Cx<T>>,
    /// `ip > 11` only: `tws[j] = w[j*l1*ido]`.
    tws: Vec<Cx<T>>,
}

pub struct Cfftp<T> {
    length: usize,
    fact: Vec<Factor<T>>,
}

/// `cfftp::factorize`.
fn factorize(length: usize) -> Result<Vec<usize>, CoreError> {
    // At most 64 factors of a usize; reserved once, so `push` never reallocates.
    let mut fact = alloc::with_capacity(64)?;
    let mut len = length;
    while len & 7 == 0 {
        fact.push(8);
        len >>= 3;
    }
    while len & 3 == 0 {
        fact.push(4);
        len >>= 2;
    }
    if len & 1 == 0 {
        len >>= 1;
        // factor 2 should be at the front of the factor list
        fact.push(2);
        let last = fact.len() - 1;
        fact.swap(0, last);
    }
    let mut divisor = 3usize;
    while divisor * divisor <= len {
        while len.is_multiple_of(divisor) {
            fact.push(divisor);
            len /= divisor;
        }
        divisor += 2;
    }
    if len > 1 {
        fact.push(len);
    }
    Ok(fact)
}

impl<T: Real> Cfftp<T> {
    /// `cfftp(length)`; `length` already passed the shared count limit.
    pub fn new(length: usize, trig: Trig) -> Result<Self, CoreError> {
        if length == 1 {
            return Ok(Self {
                length,
                fact: Vec::new(),
            });
        }
        let factors = factorize(length)?;
        let twiddle = SinCos::new(size::dim(length), trig)?;
        let mut fact = alloc::with_capacity(factors.len())?;
        let mut l1 = 1usize;
        for ip in factors {
            let ido = length / (l1 * ip);
            let mut tw = alloc::with_capacity((ip - 1) * (ido - 1))?;
            for j in 1..ip {
                for i in 1..ido {
                    tw.push(twiddle.get(size::dim(j * l1 * i)));
                }
            }
            let mut tws = Vec::new();
            if ip > 11 {
                tws = alloc::with_capacity(ip)?;
                for j in 0..ip {
                    tws.push(twiddle.get(size::dim(j * l1 * ido)));
                }
            }
            fact.push(Factor { fct: ip, tw, tws });
            l1 *= ip;
        }
        Ok(Self { length, fact })
    }

    pub fn len(&self) -> usize {
        self.length
    }

    /// `exec(c, fct, fwd)`. `ch` is scratch of the same length.
    pub fn exec(&self, c: &mut [Cx<T>], ch: &mut [Cx<T>], fct: T, fwd: bool) {
        if fwd {
            self.pass_all::<true>(c, ch, fct);
        } else {
            self.pass_all::<false>(c, ch, fct);
        }
    }

    /// `pass_all<fwd>(c, fct)`.
    fn pass_all<const F: bool>(&self, c: &mut [Cx<T>], ch: &mut [Cx<T>], fct: T) {
        let length = self.length;
        if length == 1 {
            c[0] = c[0].scale(fct);
            return;
        }
        // `in_c`: the current data (p1) is `c`, not `ch`.
        let mut in_c = true;
        let mut l1 = 1usize;
        for f in &self.fact {
            let ip = f.fct;
            let ido = length / (ip * l1);
            let (p1, p2): (&mut [Cx<T>], &mut [Cx<T>]) = if in_c {
                (&mut *c, &mut *ch)
            } else {
                (&mut *ch, &mut *c)
            };
            match ip {
                4 => passes::pass4::<F, T>(ido, l1, p1, p2, &f.tw),
                8 => passes::pass8::<F, T>(ido, l1, p1, p2, &f.tw),
                2 => passes::pass2::<F, T>(ido, l1, p1, p2, &f.tw),
                3 => passes::pass3::<F, T>(ido, l1, p1, p2, &f.tw),
                5 => passes::pass5::<F, T>(ido, l1, p1, p2, &f.tw),
                7 => passes::pass7::<F, T>(ido, l1, p1, p2, &f.tw),
                11 => passes::pass11::<F, T>(ido, l1, p1, p2, &f.tw),
                // passg leaves its result in p1 (it swaps twice).
                _ => passg::passg::<F, T>(ido, ip, l1, p1, p2, &f.tw, &f.tws),
            }
            if ip <= 11 {
                in_c = !in_c;
            }
            l1 *= ip;
        }
        if !in_c {
            if fct != T::ONE {
                for (o, v) in c.iter_mut().zip(ch.iter()) {
                    *o = v.scale(fct);
                }
            } else {
                c.copy_from_slice(ch);
            }
        } else if fct != T::ONE {
            for v in c.iter_mut() {
                *v = v.scale(fct);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn factor_order_matches_pocketfft() {
        assert_eq!(factorize(2).unwrap(), [2]);
        assert_eq!(factorize(16).unwrap(), [2, 8]);
        assert_eq!(factorize(32).unwrap(), [8, 4]);
        assert_eq!(factorize(48).unwrap(), [2, 8, 3]);
        assert_eq!(factorize(712).unwrap(), [8, 89]);
        assert_eq!(factorize(1072).unwrap(), [2, 8, 67]);
        assert_eq!(factorize(90).unwrap(), [2, 3, 3, 5]);
        assert_eq!(factorize(97).unwrap(), [97]);
    }
}
