//! `cfftp::passg`: the generic pass for a prime factor above 11. It writes `ch`, then
//! overwrites `cc` (`CX`, `CX2`), so the result stays in `cc`.
//!
//! Fusion follows the source statements: `CX2.r = CH2_0.r + w1.r*CH2_1.r + w2.r*CH2_2.r` is two
//! nested FMAs; `CX2 += a*b + c*d` rounds the inner `fma(a, b, c*d)` and then adds (a compound
//! assignment whose right side is not a bare product does not fuse); `CX2 += a*b` and
//! `CX2 -= a*b` fuse into `fma(±a, b, CX2)`.

use super::cx::{Cx, Real, fma, pm, special_mul};

#[allow(clippy::too_many_arguments)]
pub fn passg<const F: bool, T: Real>(
    ido: usize,
    ip: usize,
    l1: usize,
    cc: &mut [Cx<T>],
    ch: &mut [Cx<T>],
    wa: &[Cx<T>],
    csarr: &[Cx<T>],
) {
    let cdim = ip;
    let ipph = ip.div_ceil(2);
    let idl1 = ido * l1;
    let chi = |a: usize, b: usize, c: usize| a + ido * (b + l1 * c);
    let cci = |a: usize, b: usize, c: usize| a + ido * (b + cdim * c);
    let cxi = chi; // CX and CH share the layout
    let x2 = |a: usize, b: usize| a + idl1 * b; // CX2 and CH2

    // `wal[i] = (csarr[i].r, fwd ? -csarr[i].i : csarr[i].i)` for `0 < i < ip`, read in place
    // so the pass allocates nothing (`wal[0]` is never read: `iwal` stays in `1..ip`).
    let wal = |i: usize| {
        let c = csarr[i];
        Cx::new(c.r, if F { -c.i } else { c.i })
    };

    for k in 0..l1 {
        for i in 0..ido {
            ch[chi(i, k, 0)] = cc[cci(i, 0, k)];
        }
    }
    for j in 1..ipph {
        let jc = ip - j;
        for k in 0..l1 {
            for i in 0..ido {
                let (a, b) = pm(cc[cci(i, j, k)], cc[cci(i, jc, k)]);
                ch[chi(i, k, j)] = a;
                ch[chi(i, k, jc)] = b;
            }
        }
    }
    for k in 0..l1 {
        for i in 0..ido {
            let mut tmp = ch[chi(i, k, 0)];
            for j in 1..ipph {
                tmp = tmp.add(ch[chi(i, k, j)]);
            }
            cc[cxi(i, k, 0)] = tmp;
        }
    }
    for l in 1..ipph {
        let lc = ip - l;
        let (w1, w2) = (wal(l), wal(2 * l));
        // j = 0
        for ik in 0..idl1 {
            let (h0, h1, h2) = (ch[x2(ik, 0)], ch[x2(ik, 1)], ch[x2(ik, 2)]);
            let (hm1, hm2) = (ch[x2(ik, ip - 1)], ch[x2(ik, ip - 2)]);
            cc[x2(ik, l)] = Cx::new(
                fma(w2.r, h2.r, fma(w1.r, h1.r, h0.r)),
                fma(w2.r, h2.i, fma(w1.r, h1.i, h0.i)),
            );
            cc[x2(ik, lc)] = Cx::new(
                fma(-w1.i, hm1.i, -(w2.i * hm2.i)),
                fma(w1.i, hm1.r, w2.i * hm2.r),
            );
        }

        let mut iwal = 2 * l;
        let mut j = 3usize;
        let mut jc = ip - 3;
        while j + 1 < ipph {
            iwal += l;
            if iwal > ip {
                iwal -= ip;
            }
            let xwal = wal(iwal);
            iwal += l;
            if iwal > ip {
                iwal -= ip;
            }
            let xwal2 = wal(iwal);
            for ik in 0..idl1 {
                let (a, b) = (ch[x2(ik, j)], ch[x2(ik, j + 1)]);
                let (c, d) = (ch[x2(ik, jc)], ch[x2(ik, jc - 1)]);
                let v = cc[x2(ik, l)];
                cc[x2(ik, l)] = Cx::new(
                    v.r + fma(a.r, xwal.r, b.r * xwal2.r),
                    v.i + fma(a.i, xwal.r, b.i * xwal2.r),
                );
                let v = cc[x2(ik, lc)];
                cc[x2(ik, lc)] = Cx::new(
                    v.r - fma(c.i, xwal.i, d.i * xwal2.i),
                    v.i + fma(c.r, xwal.i, d.r * xwal2.i),
                );
            }
            j += 2;
            jc -= 2;
        }
        while j < ipph {
            iwal += l;
            if iwal > ip {
                iwal -= ip;
            }
            let xwal = wal(iwal);
            for ik in 0..idl1 {
                let (a, c) = (ch[x2(ik, j)], ch[x2(ik, jc)]);
                let v = cc[x2(ik, l)];
                cc[x2(ik, l)] = Cx::new(fma(a.r, xwal.r, v.r), fma(a.i, xwal.r, v.i));
                let v = cc[x2(ik, lc)];
                cc[x2(ik, lc)] = Cx::new(fma(-c.i, xwal.i, v.r), fma(c.r, xwal.i, v.i));
            }
            j += 1;
            jc -= 1;
        }
    }

    // shuffling and twiddling
    if ido == 1 {
        for j in 1..ipph {
            let jc = ip - j;
            for ik in 0..idl1 {
                let (a, b) = pm(cc[x2(ik, j)], cc[x2(ik, jc)]);
                cc[x2(ik, j)] = a;
                cc[x2(ik, jc)] = b;
            }
        }
    } else {
        for j in 1..ipph {
            let jc = ip - j;
            for k in 0..l1 {
                let (a, b) = pm(cc[cxi(0, k, j)], cc[cxi(0, k, jc)]);
                cc[cxi(0, k, j)] = a;
                cc[cxi(0, k, jc)] = b;
                for i in 1..ido {
                    let (x1, xx2) = pm(cc[cxi(i, k, j)], cc[cxi(i, k, jc)]);
                    cc[cxi(i, k, j)] = special_mul::<F, T>(x1, wa[(j - 1) * (ido - 1) + i - 1]);
                    cc[cxi(i, k, jc)] = special_mul::<F, T>(xx2, wa[(jc - 1) * (ido - 1) + i - 1]);
                }
            }
        }
    }
}
