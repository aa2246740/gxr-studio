//! Index extension for scipy.ndimage `mode="reflect"` and `mode="nearest"`.
//!
//! scipy's `reflect` repeats the edge sample (`d c b a | a b c d | d c b a`): index −1 of
//! a length-4 axis is 0, and the pattern repeats every `2n`. `mirror` is the other one,
//! and this port does not use it.

/// scipy `mode="reflect"` for an axis of length `n >= 1`.
pub fn reflect(index: i64, n: i64) -> i64 {
    if n <= 1 {
        return 0;
    }
    let period = 2 * n;
    let m = index.rem_euclid(period);
    if m >= n { period - 1 - m } else { m }
}

/// scipy `mode="nearest"`: clamp into `0 .. n`.
pub fn nearest(index: i64, n: i64) -> i64 {
    index.clamp(0, (n - 1).max(0))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reflect_repeats_the_edge() {
        let got: Vec<i64> = (-5..9).map(|i| reflect(i, 4)).collect();
        assert_eq!(got, vec![3, 3, 2, 1, 0, 0, 1, 2, 3, 3, 2, 1, 0, 0]);
        assert_eq!(reflect(-7, 1), 0);
    }
}
