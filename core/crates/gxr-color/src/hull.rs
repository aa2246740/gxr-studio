//! The 2-D convex hull `bake_chroma` uses as a mask, in place of `scipy.spatial.ConvexHull`.
//!
//! A monotone chain over the eight cube corners. Each edge becomes Qhull's facet equation:
//! the normal is `(p1.y - p0.y, p0.x - p1.x)` divided by its length (Qhull's
//! `qh_sethyperplane_det` for dimension 2, which matched scipy's `equations` bit for bit on
//! 1800 synthetic facets), oriented outward; the offset is `-(p0 . n)`. Qhull computes that
//! offset with a fused multiply-add; it is written plainly here (CONVENTIONS §4).

use gxr_raw::CoreError;

/// `[n0, n1, offset]` per facet: inside is `n . x + offset <= 0`.
pub type Equation = [f64; 3];

fn cross(o: [f64; 2], a: [f64; 2], b: [f64; 2]) -> f64 {
    (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
}

/// Facet equations of the hull of `points`, counter-clockwise from the lowest-leftmost point.
pub fn equations(points: &[[f64; 2]]) -> Result<Vec<Equation>, CoreError> {
    let mut pts: Vec<[f64; 2]> = points.to_vec();
    if pts.iter().any(|p| !p[0].is_finite() || !p[1].is_finite()) {
        return Err(flat());
    }
    pts.sort_by(|a, b| a[0].total_cmp(&b[0]).then(a[1].total_cmp(&b[1])));
    pts.dedup();
    if pts.len() < 3 {
        return Err(flat());
    }
    let mut lower: Vec<[f64; 2]> = Vec::new();
    for &p in &pts {
        while lower.len() >= 2 && cross(lower[lower.len() - 2], lower[lower.len() - 1], p) <= 0.0 {
            lower.pop();
        }
        lower.push(p);
    }
    let mut upper: Vec<[f64; 2]> = Vec::new();
    for &p in pts.iter().rev() {
        while upper.len() >= 2 && cross(upper[upper.len() - 2], upper[upper.len() - 1], p) <= 0.0 {
            upper.pop();
        }
        upper.push(p);
    }
    lower.pop();
    upper.pop();
    let ring: Vec<[f64; 2]> = lower.into_iter().chain(upper).collect();
    if ring.len() < 3 {
        return Err(flat());
    }
    let mut out = Vec::with_capacity(ring.len());
    for i in 0..ring.len() {
        let p0 = ring[i];
        let p1 = ring[(i + 1) % ring.len()];
        let n0 = p1[1] - p0[1];
        let n1 = p0[0] - p1[0];
        let norm = (n0 * n0 + n1 * n1).sqrt();
        let (u0, u1) = (n0 / norm, n1 / norm);
        let offset = -(p0[0] * u0 + p0[1] * u1);
        out.push([u0, u1, offset]);
    }
    Ok(out)
}

/// scipy raises `QhullError` (QH6154) when the eight corners span no area. Its long text is
/// not reproduced; the kind is stable.
fn flat() -> CoreError {
    CoreError::new("qhull", "QH6154 Qhull precision error: initial simplex is flat")
}

#[cfg(test)]
mod tests {
    use super::equations;

    #[test]
    fn square_has_four_outward_unit_normals() {
        let eq = equations(&[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.5, 0.5], [1.0, 0.5]]).unwrap();
        assert_eq!(eq.len(), 4);
        for e in &eq {
            assert!(e[0] * 0.5 + e[1] * 0.5 + e[2] < 0.0);
            assert!((e[0] * e[0] + e[1] * e[1] - 1.0).abs() < 1e-15);
        }
        assert!(equations(&[[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]]).is_err());
    }
}
