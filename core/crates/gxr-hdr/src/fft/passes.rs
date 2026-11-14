//! `cfftp::pass2/3/4/5/7/8/11`, line for line. `cc` is read with `CC(a,b,c) =
//! cc[a + ido*(b + cdim*c)]`, `ch` is written with `CH(a,b,c) = ch[a + ido*(b + l1*c)]`, and
//! `WA(x,i) = wa[i-1 + x*(ido-1)]`. Scalar sums inside one statement fuse as described in
//! `cx`; complex `+`/`*` between `cmplx` values never do. The `i == 0` branch of each loop
//! is pocketfft's `ido == 1` / first-column code.
// The radix constants are pocketfft's `long double` literals, digit for digit.
#![allow(clippy::excessive_precision)]

use super::cx::{Cx, Real, constant, fma, pm, pm_in_place, rotx90, signed, special_mul};

struct Ix {
    ido: usize,
    l1: usize,
    cdim: usize,
}

impl Ix {
    fn ch(&self, a: usize, b: usize, c: usize) -> usize {
        a + self.ido * (b + self.l1 * c)
    }
    fn cc(&self, a: usize, b: usize, c: usize) -> usize {
        a + self.ido * (b + self.cdim * c)
    }
    fn wa(&self, x: usize, i: usize) -> usize {
        i - 1 + x * (self.ido - 1)
    }
}

pub fn pass2<const F: bool, T: Real>(
    ido: usize,
    l1: usize,
    cc: &[Cx<T>],
    ch: &mut [Cx<T>],
    wa: &[Cx<T>],
) {
    let x = Ix { ido, l1, cdim: 2 };
    for k in 0..l1 {
        ch[x.ch(0, k, 0)] = cc[x.cc(0, 0, k)].add(cc[x.cc(0, 1, k)]);
        ch[x.ch(0, k, 1)] = cc[x.cc(0, 0, k)].sub(cc[x.cc(0, 1, k)]);
        for i in 1..ido {
            ch[x.ch(i, k, 0)] = cc[x.cc(i, 0, k)].add(cc[x.cc(i, 1, k)]);
            ch[x.ch(i, k, 1)] =
                special_mul::<F, T>(cc[x.cc(i, 0, k)].sub(cc[x.cc(i, 1, k)]), wa[x.wa(0, i)]);
        }
    }
}

pub fn pass3<const F: bool, T: Real>(
    ido: usize,
    l1: usize,
    cc: &[Cx<T>],
    ch: &mut [Cx<T>],
    wa: &[Cx<T>],
) {
    let tw1r: T = constant::<T>(-0.5);
    let tw1i = signed::<F, T>(0.866_025_403_784_438_646_763_723_170_752_936_2);
    let x = Ix { ido, l1, cdim: 3 };
    for k in 0..l1 {
        for i in 0..ido {
            // POCKETFFT_PREP3(i)
            let t0 = cc[x.cc(i, 0, k)];
            let (t1, t2) = pm(cc[x.cc(i, 1, k)], cc[x.cc(i, 2, k)]);
            ch[x.ch(i, k, 0)] = t0.add(t1);
            // POCKETFFT_PARTSTEP3a / 3b: `ca = t0 + t1*twr` (cmplx operators, not fused)
            let ca = t0.add(t1.scale(tw1r));
            let cb = Cx::new(-t2.i * tw1i, t2.r * tw1i);
            if i == 0 {
                let (u1, u2) = pm(ca, cb);
                ch[x.ch(0, k, 1)] = u1;
                ch[x.ch(0, k, 2)] = u2;
            } else {
                ch[x.ch(i, k, 1)] = special_mul::<F, T>(ca.add(cb), wa[x.wa(0, i)]);
                ch[x.ch(i, k, 2)] = special_mul::<F, T>(ca.sub(cb), wa[x.wa(1, i)]);
            }
        }
    }
}

pub fn pass4<const F: bool, T: Real>(
    ido: usize,
    l1: usize,
    cc: &[Cx<T>],
    ch: &mut [Cx<T>],
    wa: &[Cx<T>],
) {
    let x = Ix { ido, l1, cdim: 4 };
    for k in 0..l1 {
        for i in 0..ido {
            let (t2, t1) = pm(cc[x.cc(i, 0, k)], cc[x.cc(i, 2, k)]);
            let (t3, t4) = pm(cc[x.cc(i, 1, k)], cc[x.cc(i, 3, k)]);
            let t4 = rotx90::<F, T>(t4);
            if i == 0 {
                let (a, b) = pm(t2, t3);
                ch[x.ch(0, k, 0)] = a;
                ch[x.ch(0, k, 2)] = b;
                let (a, b) = pm(t1, t4);
                ch[x.ch(0, k, 1)] = a;
                ch[x.ch(0, k, 3)] = b;
            } else {
                ch[x.ch(i, k, 0)] = t2.add(t3);
                ch[x.ch(i, k, 1)] = special_mul::<F, T>(t1.add(t4), wa[x.wa(0, i)]);
                ch[x.ch(i, k, 2)] = special_mul::<F, T>(t2.sub(t3), wa[x.wa(1, i)]);
                ch[x.ch(i, k, 3)] = special_mul::<F, T>(t1.sub(t4), wa[x.wa(2, i)]);
            }
        }
    }
}

/// `ca`/`cb` of POCKETFFT_PARTSTEP5a: `ca.r = t0.r + twar*t1.r + twbr*t2.r`,
/// `cb.i = twai*t4.r ± twbi*t3.r`, `cb.r = -(twai*t4.i ± twbi*t3.i)` (`twbi` carries the sign).
fn step5<T: Real>(t: [Cx<T>; 5], twar: T, twbr: T, twai: T, twbi: T) -> (Cx<T>, Cx<T>) {
    let [t0, t1, t2, t3, t4] = t;
    let ca = Cx::new(
        fma(twbr, t2.r, fma(twar, t1.r, t0.r)),
        fma(twbr, t2.i, fma(twar, t1.i, t0.i)),
    );
    let cb = Cx::new(-fma(twai, t4.i, twbi * t3.i), fma(twai, t4.r, twbi * t3.r));
    (ca, cb)
}

pub fn pass5<const F: bool, T: Real>(
    ido: usize,
    l1: usize,
    cc: &[Cx<T>],
    ch: &mut [Cx<T>],
    wa: &[Cx<T>],
) {
    let tw1r = constant::<T>(0.309_016_994_374_947_424_102_293_417_182_819_1);
    let tw1i = signed::<F, T>(0.951_056_516_295_153_572_116_439_333_379_382_1);
    let tw2r = constant::<T>(-0.809_016_994_374_947_424_102_293_417_182_819_1);
    let tw2i = signed::<F, T>(0.587_785_252_292_473_129_168_705_954_639_072_8);
    let x = Ix { ido, l1, cdim: 5 };
    for k in 0..l1 {
        for i in 0..ido {
            // POCKETFFT_PREP5(i)
            let t0 = cc[x.cc(i, 0, k)];
            let (t1, t4) = pm(cc[x.cc(i, 1, k)], cc[x.cc(i, 4, k)]);
            let (t2, t3) = pm(cc[x.cc(i, 2, k)], cc[x.cc(i, 3, k)]);
            ch[x.ch(i, k, 0)] = Cx::new(t0.r + t1.r + t2.r, t0.i + t1.i + t2.i);
            let t = [t0, t1, t2, t3, t4];
            let steps = [
                (1, 4, step5(t, tw1r, tw2r, tw1i, tw2i)),
                (2, 3, step5(t, tw2r, tw1r, tw2i, -tw1i)),
            ];
            for (u1, u2, (ca, cb)) in steps {
                if i == 0 {
                    let (a, b) = pm(ca, cb);
                    ch[x.ch(0, k, u1)] = a;
                    ch[x.ch(0, k, u2)] = b;
                } else {
                    ch[x.ch(i, k, u1)] = special_mul::<F, T>(ca.add(cb), wa[x.wa(u1 - 1, i)]);
                    ch[x.ch(i, k, u2)] = special_mul::<F, T>(ca.sub(cb), wa[x.wa(u2 - 1, i)]);
                }
            }
        }
    }
}

/// POCKETFFT_PARTSTEP7a0: `(ca + cb, ca - cb)` with
/// `ca.r = t1.r + x1*t2.r + x2*t3.r + x3*t4.r`, `cb.i = y1*t7.r ± y2*t6.r ± y3*t5.r`,
/// `cb.r = -(y1*t7.i ± y2*t6.i ± y3*t5.i)`.
fn step7<T: Real>(t: &[Cx<T>; 8], xs: [T; 3], ys: [T; 3]) -> (Cx<T>, Cx<T>) {
    let [_, t1, t2, t3, t4, t5, t6, t7] = *t;
    let [x1, x2, x3] = xs;
    let [y1, y2, y3] = ys;
    let ca = Cx::new(
        fma(x3, t4.r, fma(x2, t3.r, fma(x1, t2.r, t1.r))),
        fma(x3, t4.i, fma(x2, t3.i, fma(x1, t2.i, t1.i))),
    );
    let cb = Cx::new(
        -fma(y3, t5.i, fma(y1, t7.i, y2 * t6.i)),
        fma(y3, t5.r, fma(y1, t7.r, y2 * t6.r)),
    );
    pm(ca, cb)
}

pub fn pass7<const F: bool, T: Real>(
    ido: usize,
    l1: usize,
    cc: &[Cx<T>],
    ch: &mut [Cx<T>],
    wa: &[Cx<T>],
) {
    let tw1r = constant::<T>(0.623_489_801_858_733_530_525_004_884_004_239_8);
    let tw1i = signed::<F, T>(0.781_831_482_468_029_808_708_444_526_674_057_8);
    let tw2r = constant::<T>(-0.222_520_933_956_314_404_288_902_564_496_794_8);
    let tw2i = signed::<F, T>(0.974_927_912_181_823_607_018_131_682_993_931_2);
    let tw3r = constant::<T>(-0.900_968_867_902_419_126_236_102_319_507_445_1);
    let tw3i = signed::<F, T>(0.433_883_739_117_558_120_475_768_332_848_359);
    let x = Ix { ido, l1, cdim: 7 };
    for k in 0..l1 {
        for i in 0..ido {
            // POCKETFFT_PREP7(i)
            let t1 = cc[x.cc(i, 0, k)];
            let (t2, t7) = pm(cc[x.cc(i, 1, k)], cc[x.cc(i, 6, k)]);
            let (t3, t6) = pm(cc[x.cc(i, 2, k)], cc[x.cc(i, 5, k)]);
            let (t4, t5) = pm(cc[x.cc(i, 3, k)], cc[x.cc(i, 4, k)]);
            ch[x.ch(i, k, 0)] = Cx::new(t1.r + t2.r + t3.r + t4.r, t1.i + t2.i + t3.i + t4.i);
            let t = [Cx::ZERO, t1, t2, t3, t4, t5, t6, t7];
            let steps = [
                (1, 6, step7(&t, [tw1r, tw2r, tw3r], [tw1i, tw2i, tw3i])),
                (2, 5, step7(&t, [tw2r, tw3r, tw1r], [tw2i, -tw3i, -tw1i])),
                (3, 4, step7(&t, [tw3r, tw1r, tw2r], [tw3i, -tw1i, tw2i])),
            ];
            for (u1, u2, (da, db)) in steps {
                if i == 0 {
                    ch[x.ch(0, k, u1)] = da;
                    ch[x.ch(0, k, u2)] = db;
                } else {
                    ch[x.ch(i, k, u1)] = special_mul::<F, T>(da, wa[x.wa(u1 - 1, i)]);
                    ch[x.ch(i, k, u2)] = special_mul::<F, T>(db, wa[x.wa(u2 - 1, i)]);
                }
            }
        }
    }
}

/// `hsqt2 = T0(0.707106781186547524400844362104849L)`: the same double as `FRAC_1_SQRT_2`.
const HSQT2: f64 = core::f64::consts::FRAC_1_SQRT_2;

/// `ROTX45<fwd>`.
fn rotx45<const F: bool, T: Real>(a: Cx<T>) -> Cx<T> {
    let h: T = constant::<T>(HSQT2);
    if F {
        Cx::new(h * (a.r + a.i), h * (a.i - a.r))
    } else {
        Cx::new(h * (a.r - a.i), h * (a.i + a.r))
    }
}

/// `ROTX135<fwd>`.
fn rotx135<const F: bool, T: Real>(a: Cx<T>) -> Cx<T> {
    let h: T = constant::<T>(HSQT2);
    if F {
        Cx::new(h * (a.i - a.r), h * (-a.r - a.i))
    } else {
        Cx::new(h * (-a.r - a.i), h * (a.r - a.i))
    }
}

pub fn pass8<const F: bool, T: Real>(
    ido: usize,
    l1: usize,
    cc: &[Cx<T>],
    ch: &mut [Cx<T>],
    wa: &[Cx<T>],
) {
    let x = Ix { ido, l1, cdim: 8 };
    for k in 0..l1 {
        {
            let c = |j: usize| cc[x.cc(0, j, k)];
            let (mut a1, mut a5) = pm(c(1), c(5));
            let (mut a3, mut a7) = pm(c(3), c(7));
            pm_in_place(&mut a1, &mut a3);
            a3 = rotx90::<F, T>(a3);
            a7 = rotx90::<F, T>(a7);
            pm_in_place(&mut a5, &mut a7);
            a5 = rotx45::<F, T>(a5);
            a7 = rotx135::<F, T>(a7);
            let (a0, a4) = pm(c(0), c(4));
            let (a2, a6) = pm(c(2), c(6));
            let mut put = |u: usize, v: usize, (p, q): (Cx<T>, Cx<T>)| {
                ch[x.ch(0, k, u)] = p;
                ch[x.ch(0, k, v)] = q;
            };
            put(0, 4, pm(a0.add(a2), a1));
            put(2, 6, pm(a0.sub(a2), a3));
            let a6 = rotx90::<F, T>(a6);
            put(1, 5, pm(a4.add(a6), a5));
            put(3, 7, pm(a4.sub(a6), a7));
        }
        for i in 1..ido {
            let c = |j: usize| cc[x.cc(i, j, k)];
            let w = |j: usize| wa[x.wa(j, i)];
            let (mut a1, mut a5) = pm(c(1), c(5));
            let (mut a3, mut a7) = pm(c(3), c(7));
            a7 = rotx90::<F, T>(a7);
            pm_in_place(&mut a1, &mut a3);
            a3 = rotx90::<F, T>(a3);
            pm_in_place(&mut a5, &mut a7);
            a5 = rotx45::<F, T>(a5);
            a7 = rotx135::<F, T>(a7);
            let (mut a0, mut a4) = pm(c(0), c(4));
            let (mut a2, mut a6) = pm(c(2), c(6));
            pm_in_place(&mut a0, &mut a2);
            ch[x.ch(i, k, 0)] = a0.add(a1);
            ch[x.ch(i, k, 4)] = special_mul::<F, T>(a0.sub(a1), w(3));
            ch[x.ch(i, k, 2)] = special_mul::<F, T>(a2.add(a3), w(1));
            ch[x.ch(i, k, 6)] = special_mul::<F, T>(a2.sub(a3), w(5));
            a6 = rotx90::<F, T>(a6);
            pm_in_place(&mut a4, &mut a6);
            ch[x.ch(i, k, 1)] = special_mul::<F, T>(a4.add(a5), w(0));
            ch[x.ch(i, k, 5)] = special_mul::<F, T>(a4.sub(a5), w(4));
            ch[x.ch(i, k, 3)] = special_mul::<F, T>(a6.add(a7), w(2));
            ch[x.ch(i, k, 7)] = special_mul::<F, T>(a6.sub(a7), w(6));
        }
    }
}

/// POCKETFFT_PARTSTEP11a0. `ca = t1 + t2*x1 + … + t6*x5` uses the `cmplx` operators, so every
/// product is rounded before it is added; `cb` is one scalar statement per part, so it fuses.
fn step11<T: Real>(t: &[Cx<T>; 12], xs: [T; 5], ys: [T; 5]) -> (Cx<T>, Cx<T>) {
    let mut ca = t[1];
    for (j, &xj) in xs.iter().enumerate() {
        ca = ca.add(t[2 + j].scale(xj));
    }
    // y1*t11 ± y2*t10 ± y3*t9 ± y4*t8 ± y5*t7, left to right
    let chain = |p: fn(Cx<T>) -> T| {
        let mut s = fma(ys[0], p(t[11]), ys[1] * p(t[10]));
        for (j, &yj) in ys.iter().enumerate().skip(2) {
            s = fma(yj, p(t[11 - j]), s);
        }
        s
    };
    let cb = Cx::new(-chain(|c| c.i), chain(|c| c.r));
    pm(ca, cb)
}

pub fn pass11<const F: bool, T: Real>(
    ido: usize,
    l1: usize,
    cc: &[Cx<T>],
    ch: &mut [Cx<T>],
    wa: &[Cx<T>],
) {
    let r = [
        constant::<T>(0.841_253_532_831_181_168_861_811_648_919_367_7),
        constant::<T>(0.415_415_013_001_886_425_529_274_149_229_623_2),
        constant::<T>(-0.142_314_838_273_285_140_443_792_668_616_369_7),
        constant::<T>(-0.654_860_733_945_285_064_056_925_072_466_293_6),
        constant::<T>(-0.959_492_973_614_497_389_890_368_057_066_327_7),
    ];
    let q = [
        signed::<F, T>(0.540_640_817_455_597_582_107_635_954_318_691_7),
        signed::<F, T>(0.909_631_995_354_518_371_411_715_383_079_028_5),
        signed::<F, T>(0.989_821_441_880_932_732_376_092_037_776_718_8),
        signed::<F, T>(0.755_749_574_354_258_283_774_035_843_972_344_4),
        signed::<F, T>(0.281_732_556_841_429_697_711_417_915_346_616_9),
    ];
    // The five POCKETFFT_PARTSTEP11 lines: (u1, u2, which twNr, which ±twNi).
    let rows: [(usize, usize, [usize; 5], [i8; 5]); 5] = [
        (1, 10, [1, 2, 3, 4, 5], [1, 2, 3, 4, 5]),
        (2, 9, [2, 4, 5, 3, 1], [2, 4, -5, -3, -1]),
        (3, 8, [3, 5, 2, 1, 4], [3, -5, -2, 1, 4]),
        (4, 7, [4, 3, 1, 5, 2], [4, -3, 1, 5, -2]),
        (5, 6, [5, 1, 4, 2, 3], [5, -1, 4, -2, 3]),
    ];
    let x = Ix { ido, l1, cdim: 11 };
    for k in 0..l1 {
        for i in 0..ido {
            // POCKETFFT_PREP11(i)
            let c = |j: usize| cc[x.cc(i, j, k)];
            let mut t = [Cx::ZERO; 12];
            t[1] = c(0);
            for j in 1..=5 {
                let (p, m) = pm(c(j), c(11 - j));
                t[j + 1] = p;
                t[12 - j] = m;
            }
            let mut s0 = t[1];
            for v in &t[2..=6] {
                s0 = Cx::new(s0.r + v.r, s0.i + v.i);
            }
            ch[x.ch(i, k, 0)] = s0;
            for (u1, u2, xo, yo) in rows {
                let xs = xo.map(|j| r[j - 1]);
                let ys = yo.map(|j| {
                    let v = q[usize::from(j.unsigned_abs()) - 1];
                    if j < 0 { -v } else { v }
                });
                let (da, db) = step11(&t, xs, ys);
                if i == 0 {
                    ch[x.ch(0, k, u1)] = da;
                    ch[x.ch(0, k, u2)] = db;
                } else {
                    ch[x.ch(i, k, u1)] = special_mul::<F, T>(da, wa[x.wa(u1 - 1, i)]);
                    ch[x.ch(i, k, u2)] = special_mul::<F, T>(db, wa[x.wa(u2 - 1, i)]);
                }
            }
        }
    }
}
