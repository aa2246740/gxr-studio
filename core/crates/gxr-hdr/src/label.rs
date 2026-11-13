//! `ndimage.label` (cross, connectivity 1) and `binary_dilation` (same cross).
//!
//! Labels are assigned in C order. A new component takes the next integer.
//! When the upper and left neighbours disagree, both roots are joined to the
//! smaller one, which is what scipy's equivalence table resolves to. Gaps are
//! left in place: the ids are not renumbered.

use gxr_raw::CoreError;

use crate::error::container;
use crate::filter::Plane;

fn at(h: usize, w: usize, len: usize) -> Result<(), CoreError> {
    Plane::shape_ok(h, w, len)
}

fn foreground_f64(v: f64) -> bool {
    v != 0.0
}

/// Port of `ndimage.label(..., structure=None)` → `(labels, count)`.
///
/// Labels are `i32`. The count is the number of ids that were issued, which
/// can be higher than the number of components after merges; callers that only
/// index `bincount` still see the right size on each pixel.
pub fn label_f64(src: &[f64], h: usize, w: usize) -> Result<(Vec<i32>, i32), CoreError> {
    at(h, w, src.len())?;
    label_mask(h, w, |i| foreground_f64(src[i]))
}

/// Same labelling for a 0 / non-zero byte mask.
pub fn label_u8(src: &[u8], h: usize, w: usize) -> Result<(Vec<i32>, i32), CoreError> {
    at(h, w, src.len())?;
    label_mask(h, w, |i| src[i] != 0)
}

fn label_mask(h: usize, w: usize, on: impl Fn(usize) -> bool) -> Result<(Vec<i32>, i32), CoreError> {
    let n = h * w;
    let mut labels = vec![0i32; n];
    let mut parent = vec![0i32];
    let mut next: i32 = 0;
    let find = |parent: &mut [i32], mut x: i32| -> i32 {
        let mut root = x;
        while parent[root as usize] != root {
            root = parent[root as usize];
        }
        while parent[x as usize] != root {
            let up = parent[x as usize];
            parent[x as usize] = root;
            x = up;
        }
        root
    };
    for r in 0..h {
        for c in 0..w {
            let i = r * w + c;
            if !on(i) {
                continue;
            }
            let mut neigh = [0i32; 2];
            let mut nneigh = 0;
            if r > 0 && labels[(r - 1) * w + c] != 0 {
                neigh[nneigh] = labels[(r - 1) * w + c];
                nneigh += 1;
            }
            if c > 0 && labels[r * w + c - 1] != 0 {
                neigh[nneigh] = labels[r * w + c - 1];
                nneigh += 1;
            }
            if nneigh == 0 {
                next = next.checked_add(1).ok_or_else(|| container("连通域太多"))?;
                parent.push(next);
                labels[i] = next;
            } else {
                let mut roots = [0i32; 2];
                let mut best = i32::MAX;
                for k in 0..nneigh {
                    let root = find(&mut parent, neigh[k]);
                    roots[k] = root;
                    if root < best {
                        best = root;
                    }
                }
                labels[i] = best;
                for k in 0..nneigh {
                    parent[roots[k] as usize] = best;
                }
            }
        }
    }
    for label in &mut labels {
        if *label != 0 {
            *label = find(&mut parent, *label);
        }
    }
    Ok((labels, next))
}

/// Port of `ndimage.binary_dilation(..., structure=None, iterations, border_value=0, origin=0)`.
///
/// The default structure is the radius-1 cross. `iterations=2` grows a Manhattan diamond.
pub fn binary_dilation(src: &[u8], h: usize, w: usize, iterations: i64) -> Result<Vec<u8>, CoreError> {
    at(h, w, src.len())?;
    if iterations < 0 {
        return Err(container("膨胀次数不能为负"));
    }
    if iterations > 64 {
        return Err(container("膨胀次数太大"));
    }
    let mut cur: Vec<u8> = src.iter().map(|&v| u8::from(v != 0)).collect();
    for _ in 0..iterations {
        let mut next = vec![0u8; cur.len()];
        for r in 0..h {
            for c in 0..w {
                let i = r * w + c;
                let mut on = cur[i] != 0;
                if r > 0 {
                    on |= cur[(r - 1) * w + c] != 0;
                }
                if r + 1 < h {
                    on |= cur[(r + 1) * w + c] != 0;
                }
                if c > 0 {
                    on |= cur[i - 1] != 0;
                }
                if c + 1 < w {
                    on |= cur[i + 1] != 0;
                }
                next[i] = u8::from(on);
            }
        }
        cur = next;
    }
    Ok(cur)
}
