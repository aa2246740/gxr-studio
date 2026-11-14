//! complex128 arithmetic in the form numpy's loops use, without fused multiply-add.
//!
//! numpy has no mixed real/complex loops: a float64 operand is promoted to `x + 0j` and the
//! complex loop runs. [`C::real`] is that promotion, so signed zeros come out as numpy's do.

/// One complex128 value.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct C {
    pub re: f64,
    pub im: f64,
}

/// `1j`.
pub const J: C = C { re: 0.0, im: 1.0 };

impl C {
    pub const fn new(re: f64, im: f64) -> Self {
        Self { re, im }
    }

    /// A float64 operand promoted to complex128.
    pub const fn real(x: f64) -> Self {
        Self { re: x, im: 0.0 }
    }

    pub fn plus(self, o: Self) -> Self {
        Self::new(self.re + o.re, self.im + o.im)
    }

    pub fn minus(self, o: Self) -> Self {
        Self::new(self.re - o.re, self.im - o.im)
    }

    pub fn negate(self) -> Self {
        Self::new(-self.re, -self.im)
    }

    /// numpy `multiply`: `(ar*br - ai*bi, ar*bi + ai*br)`, each product rounded.
    pub fn times(self, o: Self) -> Self {
        let rr = self.re * o.re;
        let ii = self.im * o.im;
        let ri = self.re * o.im;
        let ir = self.im * o.re;
        Self::new(rr - ii, ri + ir)
    }

    /// numpy `divide` (`loops.c.src` complex divide): Smith's choice of branch, then a
    /// reciprocal scale. Measured on numpy 2.4.3: this form with fused products matched
    /// 4000/4000 draws, so the unfused form here differs only by the fusing.
    pub fn over(self, o: Self) -> Self {
        let (in1r, in1i, in2r, in2i) = (self.re, self.im, o.re, o.im);
        let in2r_abs = in2r.abs();
        let in2i_abs = in2i.abs();
        if in2r_abs >= in2i_abs {
            if in2r_abs == 0.0 && in2i_abs == 0.0 {
                return Self::new(in1r / in2r_abs, in1i / in2r_abs);
            }
            let rat = in2i / in2r;
            let scl = 1.0 / (in2r + in2i * rat);
            Self::new((in1r + in1i * rat) * scl, (in1i - in1r * rat) * scl)
        } else {
            let rat = in2r / in2i;
            let scl = 1.0 / (in2i + in2r * rat);
            Self::new((in1r * rat + in1i) * scl, (in1i * rat - in1r) * scl)
        }
    }

    /// `np.abs` of a complex: `hypot` (libm, PORT-WAVE-1 A.6).
    pub fn abs(self) -> f64 {
        gxr_math::hypot(self.re, self.im)
    }

    /// `np.angle`: `atan2(imag, real)`.
    pub fn angle(self) -> f64 {
        gxr_math::atan2(self.im, self.re)
    }

    /// `np.exp` of a complex (`cexp`): `exp(re) * (cos(im), sin(im))`, and `exp(re) + 0j`
    /// when the imaginary part is zero.
    pub fn exp(self) -> Self {
        let e = gxr_math::exp(self.re);
        if self.im == 0.0 {
            return Self::new(e, self.im);
        }
        Self::new(e * gxr_math::cos(self.im), e * gxr_math::sin(self.im))
    }
}

#[cfg(test)]
mod tests {
    use super::C;

    #[test]
    fn division_uses_the_reciprocal_scale() {
        let a = C::new(3.0, 4.0);
        let b = C::new(1.0, 2.0);
        let q = a.over(b);
        assert_eq!(q, C::new(2.2, -0.4));
        let z = C::new(1.0, 1.0).over(C::real(0.0));
        assert!(z.re.is_infinite() && z.im.is_infinite());
        assert_eq!(
            C::new(5.0, -3.0).over(C::real(32.0)),
            C::new(5.0 / 32.0, -3.0 / 32.0)
        );
    }

    #[test]
    fn promotion_keeps_numpy_signed_zeros() {
        let p = super::J.times(C::real(-2.0));
        assert_eq!(p.re.to_bits(), (-0.0f64).to_bits());
        assert_eq!(p.im, -2.0);
    }
}
