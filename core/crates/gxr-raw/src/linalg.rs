//! 3×3 float64 linear algebra for `colorimetry.rs`, standing in for numpy's `@`, `np.diag`,
//! `np.linalg.solve` and `np.linalg.inv`.
//!
//! `solve` and `inv` are LAPACK `dgesv` (numpy's `inv` is `gesv` against the identity): LU with
//! partial pivoting (largest absolute value, first index wins ties; a NaN never wins), pivots
//! scaled by their reciprocal as `dgetf2` does, then `dtrsm`-style substitution. numpy's BLAS may
//! order or fuse the additions of a product differently, which is why scene values are compared
//! with a 1e-12 relative tolerance.

pub type Mat3 = [[f64; 3]; 3];
pub type Vec3 = [f64; 3];

/// LAPACK reported an exactly zero pivot: numpy raises `LinAlgError("Singular matrix")`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Singular;

pub const EYE: Mat3 = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]];

/// `a @ b`.
pub fn matmul(a: &Mat3, b: &Mat3) -> Mat3 {
    let mut c = [[0.0; 3]; 3];
    for i in 0..3 {
        for j in 0..3 {
            c[i][j] = a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j];
        }
    }
    c
}

/// `a @ v` (also `v @ a.T`).
pub fn matvec(a: &Mat3, v: &Vec3) -> Vec3 {
    [
        a[0][0] * v[0] + a[0][1] * v[1] + a[0][2] * v[2],
        a[1][0] * v[0] + a[1][1] * v[1] + a[1][2] * v[2],
        a[2][0] * v[0] + a[2][1] * v[1] + a[2][2] * v[2],
    ]
}

/// `np.diag(v)`.
pub fn diag(v: &Vec3) -> Mat3 {
    [[v[0], 0.0, 0.0], [0.0, v[1], 0.0], [0.0, 0.0, v[2]]]
}

/// `g * a + (1 - g) * b`, element by element, in that order.
pub fn blend(g: f64, a: &Mat3, b: &Mat3) -> Mat3 {
    let h = 1.0 - g;
    let mut c = [[0.0; 3]; 3];
    for i in 0..3 {
        for j in 0..3 {
            c[i][j] = g * a[i][j] + h * b[i][j];
        }
    }
    c
}

/// `m / s`.
pub fn div_scalar(m: &Mat3, s: f64) -> Mat3 {
    m.map(|row| row.map(|v| v / s))
}

/// `np.max` of a vector (NaN propagates).
pub fn max3(v: &Vec3) -> f64 {
    if v.iter().any(|x| x.is_nan()) {
        return f64::NAN;
    }
    v[0].max(v[1]).max(v[2])
}

/// `dgetf2`: in-place LU with partial pivoting; returns the pivot rows or `Singular`.
#[allow(clippy::needless_range_loop)]
fn getrf(a: &mut Mat3) -> Result<[usize; 3], Singular> {
    let mut ipiv = [0usize; 3];
    let mut singular = false;
    for j in 0..3 {
        let mut p = j;
        let mut best = a[j][j].abs();
        for (i, row) in a.iter().enumerate().skip(j + 1) {
            if row[j].abs() > best {
                best = row[j].abs();
                p = i;
            }
        }
        ipiv[j] = p;
        if a[p][j] == 0.0 {
            singular = true;
        } else {
            a.swap(j, p);
            let pivot = a[j][j];
            if pivot.abs() >= f64::MIN_POSITIVE {
                let r = 1.0 / pivot;
                for row in a.iter_mut().skip(j + 1) {
                    row[j] *= r;
                }
            } else {
                for row in a.iter_mut().skip(j + 1) {
                    row[j] /= pivot;
                }
            }
        }
        for i in j + 1..3 {
            let l = a[i][j];
            for k in j + 1..3 {
                let t = -a[j][k];
                a[i][k] += l * t;
            }
        }
    }
    if singular { Err(Singular) } else { Ok(ipiv) }
}

/// `dgetrs` for one right-hand side.
fn getrs(lu: &Mat3, ipiv: &[usize; 3], b: &Vec3) -> Vec3 {
    let mut x = *b;
    for (j, &p) in ipiv.iter().enumerate() {
        x.swap(j, p);
    }
    for k in 0..3 {
        if x[k] != 0.0 {
            for i in k + 1..3 {
                x[i] -= x[k] * lu[i][k];
            }
        }
    }
    for k in (0..3).rev() {
        if x[k] != 0.0 {
            x[k] /= lu[k][k];
            for i in 0..k {
                x[i] -= x[k] * lu[i][k];
            }
        }
    }
    x
}

/// `np.linalg.solve(a, b)`.
pub fn solve(a: &Mat3, b: &Vec3) -> Result<Vec3, Singular> {
    let mut lu = *a;
    let ipiv = getrf(&mut lu)?;
    Ok(getrs(&lu, &ipiv, b))
}

/// `np.linalg.inv(a)` (`gesv` against the identity).
pub fn inv(a: &Mat3) -> Result<Mat3, Singular> {
    let mut lu = *a;
    let ipiv = getrf(&mut lu)?;
    let mut out = [[0.0; 3]; 3];
    for (j, e) in EYE.iter().enumerate() {
        let col = getrs(&lu, &ipiv, e);
        for i in 0..3 {
            out[i][j] = col[i];
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn solve_and_inverse_round_trip() {
        let a = [[2.0, 1.0, 1.0], [4.0, -6.0, 0.0], [-2.0, 7.0, 2.0]];
        let x = solve(&a, &[5.0, -2.0, 9.0]).unwrap();
        for (got, want) in x.iter().zip([1.0, 1.0, 2.0]) {
            assert!((got - want).abs() < 1e-12);
        }
        let ai = inv(&a).unwrap();
        let id = matmul(&a, &ai);
        for i in 0..3 {
            for j in 0..3 {
                assert!((id[i][j] - EYE[i][j]).abs() < 1e-12);
            }
        }
        let singular = [[1.0, 2.0, 3.0], [2.0, 4.0, 6.0], [0.0, 0.0, 1.0]];
        assert_eq!(inv(&singular), Err(Singular));
    }
}
