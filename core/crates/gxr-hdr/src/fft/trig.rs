//! Correctly rounded `cos` and `sin` of a double in `[0, π/4]`, for the twiddle tables.
//!
//! pocketfft's `sincos_2pibyn` calls the host `std::cos` / `std::sin` in double. Neither
//! Apple's libSystem nor the `libm` crate is correctly rounded on every angle it asks for,
//! and on a few table lengths that last bit survives into the float twiddle. PORT-WAVE-1
//! ("platform libm results") makes the correctly rounded value the oracle, so this module
//! computes it: a Taylor series in double-double arithmetic (about 100 good bits), then one
//! rounding to double. Every argument `calc` passes is `x * ang` with `x <= n` and
//! `ang = (π/4) / n`, so it lies in `[0, π/4 (1 + 2⁻⁵²)]`; no range reduction is needed.
//! Only `+ - *` and `libm::fma` are used, so native and wasm32 give the same bits.

/// An unevaluated sum `hi + lo` with `|lo| <= ulp(hi) / 2`.
#[derive(Clone, Copy)]
struct Dd {
    hi: f64,
    lo: f64,
}

fn two_sum(a: f64, b: f64) -> Dd {
    let s = a + b;
    let bb = s - a;
    Dd {
        hi: s,
        lo: (a - (s - bb)) + (b - bb),
    }
}

fn quick_two_sum(a: f64, b: f64) -> Dd {
    let s = a + b;
    Dd {
        hi: s,
        lo: b - (s - a),
    }
}

fn add(a: Dd, b: Dd) -> Dd {
    let s = two_sum(a.hi, b.hi);
    let t = two_sum(a.lo, b.lo);
    let s = quick_two_sum(s.hi, s.lo + t.hi);
    quick_two_sum(s.hi, s.lo + t.lo)
}

fn mul(a: Dd, b: Dd) -> Dd {
    let p = a.hi * b.hi;
    let e = libm::fma(a.hi, b.hi, -p);
    quick_two_sum(p, e + (a.hi * b.lo + a.lo * b.hi))
}

/// `a / d` for a small positive integer `d` (exact as a double).
fn div_int(a: Dd, d: f64) -> Dd {
    let q1 = a.hi / d;
    let p = q1 * d;
    let e = libm::fma(q1, d, -p);
    let r = ((a.hi - p) - e) + a.lo;
    quick_two_sum(q1, r / d)
}

/// `Σ (-1)^j x^(2j + start) / (2j + start)!`, summed until the terms vanish.
fn series(x: f64, start: u32) -> f64 {
    let xx = Dd {
        hi: x * x,
        lo: libm::fma(x, x, -(x * x)),
    };
    let mut term = if start == 0 {
        Dd { hi: 1.0, lo: 0.0 }
    } else {
        Dd { hi: x, lo: 0.0 }
    };
    let mut sum = term;
    let mut k = f64::from(start);
    // x <= π/4: the 30th term is below 2⁻¹²⁰ of the first.
    for _ in 0..30 {
        term = div_int(mul(term, xx), (k + 1.0) * (k + 2.0));
        term = Dd {
            hi: -term.hi,
            lo: -term.lo,
        };
        k += 2.0;
        sum = add(sum, term);
    }
    sum.hi + sum.lo
}

/// Correctly rounded `cos(x)` for `0 <= x <= π/4`.
pub fn cos(x: f64) -> f64 {
    series(x, 0)
}

/// Correctly rounded `sin(x)` for `0 <= x <= π/4`.
pub fn sin(x: f64) -> f64 {
    if x == 0.0 {
        return x;
    }
    series(x, 1)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_points_and_known_roundings() {
        assert_eq!(cos(0.0), 1.0);
        assert_eq!(sin(0.0).to_bits(), 0);
        // cos(π/4) and sin(π/4) of the double nearest π/4 (values from Decimal, 80 digits).
        let q = core::f64::consts::FRAC_PI_4;
        assert_eq!(cos(q).to_bits(), 0x3fe6_a09e_667f_3bcd);
        assert_eq!(sin(q).to_bits(), 0x3fe6_a09e_667f_3bcc);
    }
}
