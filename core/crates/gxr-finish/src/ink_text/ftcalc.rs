//! Fixed-point 64-bit arithmetic, CORDIC vector length, and CBox helpers.
//!
//! Port of FreeType 2.14.3 `src/base/ftcalc.c`, `src/base/fttrigon.c`, and
//! `src/base/ftoutln.c` (`FT_Outline_Get_CBox`, `FT_Vector_Transform`).

/// 2D point or vector in 26.6 fixed-point or font units (`FT_Vector`).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct FtVector {
    pub x: i64,
    pub y: i64,
}

/// 2×2 16.16 fixed-point transformation matrix (`FT_Matrix`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FtMatrix {
    pub xx: i64,
    pub xy: i64,
    pub yx: i64,
    pub yy: i64,
}

impl Default for FtMatrix {
    fn default() -> Self {
        Self {
            xx: 0x10000,
            xy: 0,
            yx: 0,
            yy: 0x10000,
        }
    }
}

/// Bounding box in 26.6 or integer pixel coordinates (`FT_BBox`).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct FtBBox {
    pub x_min: i64,
    pub y_min: i64,
    pub x_max: i64,
    pub y_max: i64,
}

#[inline]
pub fn ft_pix_floor(x: i64) -> i64 {
    x & !63
}

#[inline]
pub fn ft_pix_round(x: i64) -> i64 {
    ft_pix_floor(x.wrapping_add(32))
}

#[inline]
pub fn ft_pix_ceil(x: i64) -> i64 {
    ft_pix_floor(x.wrapping_add(63))
}

/// Pillow `_imagingft.c` `PIXEL(x)`: `((x + 32) & -64) >> 6`.
#[inline]
pub fn pixel_26_6(x: i64) -> i64 {
    (x.wrapping_add(32) & !63) >> 6
}

/// Port of FreeType `FT_MulFix` (`FT_INT64` path): `(a * b) / 0x10000` rounded to nearest.
#[inline]
pub fn ft_mul_fix(a: i64, b: i64) -> i64 {
    let ab = (a as u64).wrapping_mul(b as u64) as i64;
    let phase = 0x8000_i64.wrapping_add(ab >> 63);
    ((ab as u64).wrapping_add(phase as u64) as i64) >> 16
}

#[inline]
fn move_sign(x: i64, sign: &mut i32) -> u64 {
    if x < 0 {
        *sign = -*sign;
        0_u64.wrapping_sub(x as u64)
    } else {
        x as u64
    }
}

/// Port of FreeType `FT_DivFix` (`FT_INT64` path): `(a * 0x10000) / b` rounded to nearest.
#[inline]
pub fn ft_div_fix(a_in: i64, b_in: i64) -> i64 {
    let mut s = 1_i32;
    let a = move_sign(a_in, &mut s);
    let b = move_sign(b_in, &mut s);
    let q = if b > 0 {
        (a.wrapping_shl(16)).wrapping_add(b >> 1) / b
    } else {
        0x7FFF_FFFF_u64
    };
    if s < 0 {
        0_u64.wrapping_sub(q) as i64
    } else {
        q as i64
    }
}

/// Port of FreeType `FT_MulDiv` (`FT_INT64` path): `(a * b) / c` rounded to nearest.
#[inline]
pub fn ft_mul_div(a_in: i64, b_in: i64, c_in: i64) -> i64 {
    let mut s = 1_i32;
    let a = move_sign(a_in, &mut s);
    let b = move_sign(b_in, &mut s);
    let c = move_sign(c_in, &mut s);
    let d = if c > 0 {
        (a.wrapping_mul(b)).wrapping_add(c >> 1) / c
    } else {
        0x7FFF_FFFF_u64
    };
    if s < 0 {
        0_u64.wrapping_sub(d) as i64
    } else {
        d as i64
    }
}

/// Port of FreeType `FT_MulDiv_No_Round` (`FT_INT64` path): `(a * b) / c` truncated toward zero.
#[inline]
pub fn ft_mul_div_no_round(a_in: i64, b_in: i64, c_in: i64) -> i64 {
    let mut s = 1_i32;
    let a = move_sign(a_in, &mut s);
    let b = move_sign(b_in, &mut s);
    let c = move_sign(c_in, &mut s);
    let d = if c > 0 {
        a.wrapping_mul(b) / c
    } else {
        0x7FFF_FFFF_u64
    };
    if s < 0 {
        0_u64.wrapping_sub(d) as i64
    } else {
        d as i64
    }
}

const FT_TRIG_SCALE: u64 = 0xDBD9_5B16;
const FT_TRIG_SAFE_MSB: i32 = 29;
const FT_TRIG_MAX_ITERS: usize = 23;
const FT_TRIG_ARCTAN_TABLE: [i64; 22] = [
    1_740_967, 919_879, 466_945, 234_379, 117_304, 58_666, 29_335, 14_668, 7_334, 3_667, 1_833,
    917, 458, 229, 115, 57, 29, 14, 7, 4, 2, 1,
];

#[inline]
fn ft_msb_u32(z: u32) -> i32 {
    if z == 0 {
        0
    } else {
        (31 - z.leading_zeros()) as i32
    }
}

#[inline]
fn ft_trig_downscale(val_in: i64) -> i64 {
    let mut s = 1_i32;
    let mut val = val_in;
    if val < 0 {
        val = val.wrapping_neg();
        s = -1;
    }
    let scaled = ((val as u64)
        .wrapping_mul(FT_TRIG_SCALE)
        .wrapping_add(0x4000_0000))
        >> 32;
    let res = scaled as i64;
    if s < 0 { -res } else { res }
}

fn ft_trig_prenorm(vec: &mut FtVector) -> i32 {
    let x = vec.x;
    let y = vec.y;
    let mag = (x.wrapping_abs() as u32) | (y.wrapping_abs() as u32);
    let shift = ft_msb_u32(mag);
    if shift <= FT_TRIG_SAFE_MSB {
        let s = FT_TRIG_SAFE_MSB - shift;
        vec.x = ((x as u64) << s) as i64;
        vec.y = ((y as u64) << s) as i64;
        s
    } else {
        let s = shift - FT_TRIG_SAFE_MSB;
        vec.x = x >> s;
        vec.y = y >> s;
        -s
    }
}

fn ft_trig_pseudo_polarize(vec: &mut FtVector) {
    let mut x = vec.x;
    let mut y = vec.y;

    if y > x {
        if y > -x {
            let xtemp = y;
            y = -x;
            x = xtemp;
        } else {
            x = -x;
            y = -y;
        }
    } else if y < -x {
        let xtemp = -y;
        y = x;
        x = xtemp;
    }

    let mut b = 1_i64;
    for i in 1..FT_TRIG_MAX_ITERS {
        let _ = FT_TRIG_ARCTAN_TABLE[i - 1];
        if y > 0 {
            let xtemp = x + ((y + b) >> i);
            y -= (x + b) >> i;
            x = xtemp;
        } else {
            let xtemp = x - ((y + b) >> i);
            y += (x + b) >> i;
            x = xtemp;
        }
        b <<= 1;
    }

    vec.x = x;
}

/// Port of FreeType `FT_Vector_Length` (CORDIC).
pub fn ft_vector_length(vec: FtVector) -> i64 {
    if vec.x == 0 {
        return vec.y.wrapping_abs();
    }
    if vec.y == 0 {
        return vec.x.wrapping_abs();
    }
    let mut v = vec;
    let shift = ft_trig_prenorm(&mut v);
    ft_trig_pseudo_polarize(&mut v);
    v.x = ft_trig_downscale(v.x);
    if shift > 0 {
        (v.x + (1_i64 << (shift - 1))) >> shift
    } else {
        ((v.x as u32 as u64) << (-shift)) as i64
    }
}

/// Port of FreeType `FT_Hypot`.
#[inline]
pub fn ft_hypot(x: i64, y: i64) -> i64 {
    ft_vector_length(FtVector { x, y })
}

/// Port of FreeType `FT_Vector_Transform`.
#[inline]
pub fn ft_vector_transform(v: FtVector, m: &FtMatrix) -> FtVector {
    FtVector {
        x: ft_mul_fix(v.x, m.xx).wrapping_add(ft_mul_fix(v.y, m.xy)),
        y: ft_mul_fix(v.x, m.yx).wrapping_add(ft_mul_fix(v.y, m.yy)),
    }
}

/// Port of FreeType `FT_Outline_Get_CBox`.
pub fn ft_outline_get_cbox(points: &[FtVector]) -> FtBBox {
    if points.is_empty() {
        return FtBBox::default();
    }
    let mut x_min = points[0].x;
    let mut x_max = points[0].x;
    let mut y_min = points[0].y;
    let mut y_max = points[0].y;
    for p in &points[1..] {
        if p.x < x_min {
            x_min = p.x;
        }
        if p.x > x_max {
            x_max = p.x;
        }
        if p.y < y_min {
            y_min = p.y;
        }
        if p.y > y_max {
            y_max = p.y;
        }
    }
    FtBBox {
        x_min,
        y_min,
        x_max,
        y_max,
    }
}

/// Port of FreeType `FT_Glyph_Get_CBox(..., FT_GLYPH_BBOX_PIXELS)` on an outline CBox.
#[inline]
pub fn ft_cbox_pixels(cbox: FtBBox) -> FtBBox {
    FtBBox {
        x_min: ft_pix_floor(cbox.x_min) >> 6,
        y_min: ft_pix_floor(cbox.y_min) >> 6,
        x_max: ft_pix_ceil(cbox.x_max) >> 6,
        y_max: ft_pix_ceil(cbox.y_max) >> 6,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mul_fix_matches_freetype_c_at_64bit_boundaries() {
        let cases: &[(i64, i64, i64)] = &[
            (0, 0, 0),
            (1, 0x10000, 1),
            (-1, 0x10000, -1),
            (1, 0x8000, 1),
            (-1, 0x8000, -1),
            (3, 0x8000, 2),
            (-3, 0x8000, -2),
            (1000, 335_020, 5112),
            (0x7FFF_FFFF, 0x10000, 0x7FFF_FFFF),
            (-0x7FFF_FFFF, 0x10000, -0x7FFF_FFFF),
            (-0x8000_0000, 0x10000, -0x8000_0000),
            (0x7FFF_FFFF, 0x7FFF_FFFF, 70_368_744_112_128),
            (-0x8000_0000, 0x7FFF_FFFF, -70_368_744_144_896),
            (1_i64 << 40, 1_i64 << 16, 1_i64 << 40),
            (-(1_i64 << 40), (1_i64 << 16) + 1, -1_099_528_404_992),
            ((1_i64 << 47) - 1, 0x10001, -140_735_340_871_681),
            (-(1_i64 << 47), 0x10001, 140_735_340_871_680),
        ];
        for &(a, b, expect) in cases {
            assert_eq!(ft_mul_fix(a, b), expect, "ft_mul_fix({a}, {b})");
        }
    }

    #[test]
    fn div_fix_matches_freetype_c_at_64bit_boundaries() {
        let cases: &[(i64, i64, i64)] = &[
            (0, 1000, 0),
            (64, 1000, 4194),
            (5112 * 64, 1000, 21_441_282),
            (-5112 * 64, 1000, -21_441_282),
            (1, 2, 32768),
            (-1, 2, -32768),
            (1, 3, 21845),
            (-1, 3, -21845),
            (100, 0, 0x7FFF_FFFF),
            (-100, 0, -0x7FFF_FFFF),
            (0, 0, 0x7FFF_FFFF),
            (0x7FFF_FFFF, 1, 140_737_488_289_792),
            (-0x8000_0000, 1, -140_737_488_355_328),
            (1_i64 << 46, 3, 1_537_228_672_809_129_301),
            (-(1_i64 << 46), 3, -1_537_228_672_809_129_301),
            (1_i64 << 47, 0x7FFF_FFFF, 4_294_967_298),
            (i64::MIN, -1, 0),
            (1000, -3, -21_845_333),
            (-1000, -3, 21_845_333),
        ];
        for &(a, b, expect) in cases {
            assert_eq!(ft_div_fix(a, b), expect, "ft_div_fix({a}, {b})");
        }
    }

    #[test]
    fn mul_div_matches_freetype_c() {
        let cases: &[(i64, i64, i64, i64)] = &[
            (0x7FFF_FFFF, 0x7FFF_FFFF, 0x7FFF_FFFF, 0x7FFF_FFFF),
            (1_000_000_000, 2_000_000_000, 3_000_000_000, 666_666_667),
            (-1_000_000_000, 2_000_000_000, 3_000_000_000, -666_666_667),
            (1, 1, 2, 1),
            (-1, 1, 2, -1),
            (3, 1, 2, 2),
            (-3, 1, 2, -2),
            (123, 456, 0, 0x7FFF_FFFF),
            (-123, 456, 0, -0x7FFF_FFFF),
        ];
        for &(a, b, c, expect) in cases {
            assert_eq!(ft_mul_div(a, b, c), expect, "ft_mul_div({a}, {b}, {c})");
        }
        assert_eq!(ft_mul_div_no_round(3, 1, 2), 1);
        assert_eq!(ft_mul_div_no_round(-3, 1, 2), -1);
    }

    #[test]
    fn hypot_and_pixel_rounding() {
        assert_eq!(ft_hypot(0, 0), 0);
        assert_eq!(ft_hypot(0x30000, 0x40000), 0x50000);
        assert_eq!(ft_hypot(-0x30000, 0x40000), 0x50000);
        assert_eq!(pixel_26_6(0), 0);
        assert_eq!(pixel_26_6(31), 0);
        assert_eq!(pixel_26_6(32), 1);
        assert_eq!(pixel_26_6(-32), 0);
        assert_eq!(pixel_26_6(-33), -1);
    }
}
