//! `ndimage.label` and `ndimage.binary_dilation` with the default structure: the cross
//! (connectivity 1, `generate_binary_structure(2, 1)`).
//!
//! scipy numbers components `1..=n` in the raster order of each component's first pixel
//! (measured on random masks). The labels are int32.

use gxr_raw::CoreError;

use crate::error::unsupported;
use crate::filter::check_shape;

fn find(parent: &mut [usize], mut x: usize) -> usize {
    let mut root = x;
    while parent[root] != root {
        root = parent[root];
    }
    while parent[x] != root {
        let up = parent[x];
        parent[x] = root;
        x = up;
    }
    root
}

/// Port of `ndimage.label(mask)` → `(labels, count)`; non-zero is foreground.
pub fn label(mask: &[bool], h: usize, w: usize) -> Result<(Vec<i32>, i32), CoreError> {
    check_shape(h, w, mask.len())?;
    let mut provisional = vec![0usize; mask.len()];
    let mut parent: Vec<usize> = vec![0];
    for r in 0..h {
        for c in 0..w {
            let i = r * w + c;
            if !mask[i] {
                continue;
            }
            let up = if r > 0 { provisional[i - w] } else { 0 };
            let left = if c > 0 { provisional[i - 1] } else { 0 };
            provisional[i] = match (up, left) {
                (0, 0) => {
                    parent.push(parent.len());
                    parent.len() - 1
                }
                (a, 0) | (0, a) => a,
                (a, b) => {
                    let (ra, rb) = (find(&mut parent, a), find(&mut parent, b));
                    let keep = ra.min(rb);
                    parent[ra] = keep;
                    parent[rb] = keep;
                    keep
                }
            };
        }
    }
    // Final ids follow the raster order of each component's first pixel.
    let mut final_id = vec![0i32; parent.len()];
    let mut next: i32 = 0;
    let mut labels = vec![0i32; mask.len()];
    for (i, &p) in provisional.iter().enumerate() {
        if p == 0 {
            continue;
        }
        let root = find(&mut parent, p);
        if final_id[root] == 0 {
            next = next
                .checked_add(1)
                .ok_or_else(|| unsupported("bad_input", "连通域太多"))?;
            final_id[root] = next;
        }
        labels[i] = final_id[root];
    }
    Ok((labels, next))
}

fn dilate_once(cur: &[bool], h: usize, w: usize) -> Vec<bool> {
    let mut next = vec![false; cur.len()];
    for r in 0..h {
        for c in 0..w {
            let i = r * w + c;
            next[i] = cur[i]
                || (r > 0 && cur[i - w])
                || (r + 1 < h && cur[i + w])
                || (c > 0 && cur[i - 1])
                || (c + 1 < w && cur[i + 1]);
        }
    }
    next
}

/// Port of `ndimage.binary_dilation(mask, iterations=n)` (`structure=None`, `border_value=0`,
/// `origin=0`). `iterations < 1` repeats until nothing changes, as scipy documents.
pub fn binary_dilation(
    mask: &[bool],
    h: usize,
    w: usize,
    iterations: i64,
) -> Result<Vec<bool>, CoreError> {
    check_shape(h, w, mask.len())?;
    let mut cur = mask.to_vec();
    if iterations < 1 {
        loop {
            let next = dilate_once(&cur, h, w);
            if next == cur {
                return Ok(cur);
            }
            cur = next;
        }
    }
    // More passes than the plane is long change nothing further.
    let limit = u64::try_from(h.saturating_add(w)).unwrap_or(u64::MAX);
    let passes = u64::try_from(iterations)
        .unwrap_or(u64::MAX)
        .min(limit.saturating_add(1));
    for _ in 0..passes {
        let next = dilate_once(&cur, h, w);
        if next == cur {
            break;
        }
        cur = next;
    }
    Ok(cur)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn diagonal_pixels_are_separate_and_ids_follow_first_pixel() {
        // 1 0 1
        // 0 1 1
        let m = [true, false, true, false, true, true];
        let (lab, n) = label(&m, 2, 3).unwrap();
        assert_eq!(n, 2);
        assert_eq!(lab, vec![1, 0, 2, 0, 2, 2]);
    }

    #[test]
    fn two_iterations_grow_a_diamond() {
        let mut m = vec![false; 25];
        m[12] = true;
        let d = binary_dilation(&m, 5, 5, 2).unwrap();
        let on: usize = d.iter().filter(|v| **v).count();
        assert_eq!(on, 13);
        assert!(!d[0] && d[2] && d[10]);
    }
}
