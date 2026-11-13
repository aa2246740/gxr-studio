//! The type-28 matrix row and the BT.601 helpers the prediction path uses.
//!
//! Port of gxr_studio/engine/matrix_model.py (runtime subset).

use gxr_raw::CoreError;

use crate::cplx::{C, J};

/// `CENTRE`.
pub const CENTRE: f64 = 128.0;

/// Port of gxr_studio/engine/matrix_model.py:31 decode_coef
pub fn decode_coef(u: u16) -> Result<i32, CoreError> {
    if u & !0x0FFF != 0 {
        return Err(CoreError::new(
            "value",
            format!("coefficient word {u:#06x} has bits above 11"),
        ));
    }
    let m = i32::from(u & 0x7FF);
    Ok(if u & 0x800 != 0 { -m } else { m })
}

/// One 14-word row: nine Q10 coefficients and the five tail words.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct MatrixRow {
    pub coef: [[i32; 3]; 3],
    pub tail: [u16; 5],
}

impl MatrixRow {
    /// Port of gxr_studio/engine/matrix_model.py:65 MatrixRow.from_bytes
    pub fn from_bytes(b: &[u8]) -> Result<Self, CoreError> {
        if b.len() != 28 {
            return Err(CoreError::new("value", "a matrix row is 28 bytes"));
        }
        let mut words = [0u16; 14];
        for (i, w) in words.iter_mut().enumerate() {
            *w = u16::from_be_bytes([b[2 * i], b[2 * i + 1]]);
        }
        let mut coef = [[0i32; 3]; 3];
        for i in 0..9 {
            coef[i / 3][i % 3] = decode_coef(words[i])?;
        }
        let mut tail = [0u16; 5];
        tail.copy_from_slice(&words[9..]);
        Ok(Self { coef, tail })
    }

    /// Port of gxr_studio/engine/matrix_model.py:104 MatrixRow.effective
    ///
    /// Only `vivid_pct == 100` is used; the chroma rows are multiplied by `100.0 / 100.0`.
    pub fn effective(&self) -> [[f64; 3]; 3] {
        let mut m = [[0.0; 3]; 3];
        let scale = 100.0f64 / 100.0;
        for (i, row) in m.iter_mut().enumerate() {
            for (j, v) in row.iter_mut().enumerate() {
                *v = f64::from(self.coef[i][j]) / 1024.0;
                if i > 0 {
                    *v *= scale;
                }
            }
        }
        m
    }

    /// `self.offsets`: tail words 0..3 as float.
    pub fn offsets(&self) -> [f64; 3] {
        [f64::from(self.tail[0]), f64::from(self.tail[1]), f64::from(self.tail[2])]
    }

    /// Port of gxr_studio/engine/matrix_model.py:111 MatrixRow.apply
    pub fn apply(&self, m: &[[f64; 3]; 3], rgbp: [f64; 3]) -> (f64, C) {
        let ycc = matvec(m, rgbp);
        let off = self.offsets();
        let y = ycc[0] + off[0];
        let cb = C::real(ycc[1] + off[1] - CENTRE);
        let cr = J.mul(C::real(ycc[2] + off[2] - CENTRE));
        (y, cb.add(cr))
    }
}

/// `p @ m.T` for one row, left to right: `p0*m0 + p1*m1 + p2*m2`.
pub fn matvec(m: &[[f64; 3]; 3], p: [f64; 3]) -> [f64; 3] {
    let mut out = [0.0; 3];
    for (o, row) in out.iter_mut().zip(m) {
        let mut s = p[0] * row[0];
        s = s + p[1] * row[1];
        s = s + p[2] * row[2];
        *o = s;
    }
    out
}

/// Port of gxr_studio/engine/matrix_model.py:170 rgb_from_ycc
pub fn rgb_from_ycc(y: f64, c: C) -> [f64; 3] {
    let (cb, cr) = (c.re, c.im);
    [y + 1.402 * cr, y - 0.344136 * cb - 0.714136 * cr, y + 1.772 * cb]
}

#[cfg(test)]
mod tests {
    use super::{MatrixRow, decode_coef};

    #[test]
    fn coefficient_words_follow_sign_magnitude() {
        assert_eq!(decode_coef(0x0400).unwrap(), 1024);
        assert_eq!(decode_coef(0x0C00).unwrap(), -1024);
        let err = decode_coef(0x1000).unwrap_err();
        assert_eq!(err.message, "coefficient word 0x1000 has bits above 11");
        assert_eq!(MatrixRow::from_bytes(&[0; 27]).unwrap_err().message, "a matrix row is 28 bytes");
    }
}
