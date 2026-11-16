//! Ellipse, arc, pieslice, and rounded_rectangle rasterizers (Pillow `Draw.c` + `ImageDraw.py`).

use gxr_raw::CoreError;

use crate::ink::draw::{
    bad_coords, check_mode, draw_rect_fill_i64, draw_rectangle, float_to_c_int, hline_px_i64,
    same_ink,
};
use crate::ink::draw_ellipse::{EllipseState, draw_ellipse, draw_ellipse_outline};
use crate::ink::image::InkImage;

const PI: f64 = std::f64::consts::PI;

#[inline]
fn round_ties_even_i32(v: f64) -> Result<i32, CoreError> {
    if !v.is_finite() {
        return Err(bad_coords("coordinates must be finite"));
    }
    let r = v.round_ties_even();
    if r < f64::from(i32::MIN) || r > f64::from(i32::MAX) {
        return Err(bad_coords("coordinate out of range"));
    }
    Ok(r as i32)
}

#[inline]
fn lround_f64(v: f64) -> Result<i32, CoreError> {
    if !v.is_finite() {
        return Err(bad_coords("arc intersection out of range"));
    }
    let r = v.round();
    if r < f64::from(i32::MIN) || r > f64::from(i32::MAX) {
        return Err(bad_coords("arc intersection out of range"));
    }
    Ok(r as i32)
}

/// Port of Pillow `ImageDraw.rounded_rectangle`.
#[allow(clippy::too_many_arguments)]
pub fn draw_rounded_rectangle(
    im: &mut InkImage,
    xy: [f64; 4],
    radius: f64,
    fill: Option<[u8; 3]>,
    outline: Option<[u8; 3]>,
    width: i32,
    corners: [bool; 4],
) -> Result<(), CoreError> {
    check_mode(im)?;
    if xy.iter().any(|v| !v.is_finite()) {
        return Err(bad_coords("coordinates must be finite"));
    }
    if !radius.is_finite() {
        return Err(bad_coords("radius must be finite"));
    }
    if xy[2] < xy[0] {
        return Err(bad_coords("x1 must be greater than or equal to x0"));
    }
    if xy[3] < xy[1] {
        return Err(bad_coords("y1 must be greater than or equal to y0"));
    }

    let mut d = radius * 2.0_f64;
    let x0 = i64::from(round_ties_even_i32(xy[0])?);
    let y0 = i64::from(round_ties_even_i32(xy[1])?);
    let x1 = i64::from(round_ties_even_i32(xy[2])?);
    let y1 = i64::from(round_ties_even_i32(xy[3])?);
    let dx = x1 - x0;
    let dy = y1 - y0;

    let mut full_x = false;
    let mut full_y = false;
    if corners.iter().all(|&c| c) {
        full_x = d >= ((dx - 1) as f64);
        if full_x {
            d = dx as f64;
        }
        full_y = d >= ((dy - 1) as f64);
        if full_y {
            d = dy as f64;
        }
        if full_x && full_y {
            if let Some(fill_ink) = fill {
                draw_ellipse(im, xy, fill_ink)?;
            }
            if let Some(out_ink) = outline
                && !fill.is_some_and(|f| same_ink(im.mode, out_ink, f))
                && width != 0
            {
                draw_ellipse_outline(im, xy, out_ink, width)?;
            }
            return Ok(());
        }
    }

    if d == 0.0_f64 || !corners.iter().any(|&c| c) {
        return draw_rectangle(im, xy, fill, outline, width);
    }

    let r_f = (d / 2.0_f64).floor();
    if !r_f.is_finite() || r_f < f64::from(i32::MIN) || r_f > f64::from(i32::MAX) {
        return Err(bad_coords("radius out of range"));
    }
    let r = r_f as i64;
    let x0f = x0 as f64;
    let y0f = y0 as f64;
    let x1f = x1 as f64;
    let y1f = y1 as f64;

    let corner_parts = || -> Vec<([f64; 4], f64, f64)> {
        if full_x {
            vec![
                ([x0f, y0f, x0f + d, y0f + d], 180.0, 360.0),
                ([x0f, y1f - d, x0f + d, y1f], 0.0, 180.0),
            ]
        } else if full_y {
            vec![
                ([x0f, y0f, x0f + d, y0f + d], 90.0, 270.0),
                ([x1f - d, y0f, x1f, y0f + d], 270.0, 90.0),
            ]
        } else {
            let all = [
                ([x0f, y0f, x0f + d, y0f + d], 180.0, 270.0),
                ([x1f - d, y0f, x1f, y0f + d], 270.0, 360.0),
                ([x1f - d, y1f - d, x1f, y1f], 0.0, 90.0),
                ([x0f, y1f - d, x0f + d, y1f], 90.0, 180.0),
            ];
            all.into_iter()
                .enumerate()
                .filter(|&(i, _)| corners[i])
                .map(|(_, p)| p)
                .collect()
        }
    };

    let checked_fill_rect =
        |im: &mut InkImage, ax0: i64, ay0: i64, ax1: i64, ay1: i64, ink: [u8; 3]| {
            if ax1 < ax0 {
                return Err(bad_coords("x1 must be greater than or equal to x0"));
            }
            if ay1 < ay0 {
                return Err(bad_coords("y1 must be greater than or equal to y0"));
            }
            draw_rect_fill_i64(im, ax0, ay0, ax1, ay1, ink);
            Ok(())
        };

    if let Some(fill_ink) = fill {
        for (part, s, e) in corner_parts() {
            draw_pieslice_fill(im, part, s, e, fill_ink)?;
        }
        if full_x {
            checked_fill_rect(im, x0, y0 + r + 1, x1, y1 - r - 1, fill_ink)?;
        } else if x1 - r > x0 + r + 1 {
            checked_fill_rect(im, x0 + r + 1, y0, x1 - r - 1, y1, fill_ink)?;
        }
        if !full_x && !full_y {
            let mut ly0 = y0;
            let mut ly1 = y1;
            if corners[0] {
                ly0 += r + 1;
            }
            if corners[3] {
                ly1 -= r + 1;
            }
            checked_fill_rect(im, x0, ly0, x0 + r, ly1, fill_ink)?;

            let mut ry0 = y0;
            let mut ry1 = y1;
            if corners[1] {
                ry0 += r + 1;
            }
            if corners[2] {
                ry1 -= r + 1;
            }
            checked_fill_rect(im, x1 - r, ry0, x1, ry1, fill_ink)?;
        }
    }

    if let Some(out_ink) = outline
        && !fill.is_some_and(|f| same_ink(im.mode, out_ink, f))
        && width != 0
    {
        for (part, s, e) in corner_parts() {
            draw_arc_outline(im, part, s, e, out_ink, width)?;
        }
        let w_i64 = i64::from(width);
        if !full_x {
            let mut tx0 = x0;
            let mut tx1 = x1;
            if corners[0] {
                tx0 += r + 1;
            }
            if corners[1] {
                tx1 -= r + 1;
            }
            checked_fill_rect(im, tx0, y0, tx1, y0 + w_i64 - 1, out_ink)?;

            let mut bx0 = x0;
            let mut bx1 = x1;
            if corners[3] {
                bx0 += r + 1;
            }
            if corners[2] {
                bx1 -= r + 1;
            }
            checked_fill_rect(im, bx0, y1 - w_i64 + 1, bx1, y1, out_ink)?;
        }
        if !full_y {
            let mut ly0 = y0;
            let mut ly1 = y1;
            if corners[0] {
                ly0 += r + 1;
            }
            if corners[3] {
                ly1 -= r + 1;
            }
            checked_fill_rect(im, x0, ly0, x0 + w_i64 - 1, ly1, out_ink)?;

            let mut ry0 = y0;
            let mut ry1 = y1;
            if corners[1] {
                ry0 += r + 1;
            }
            if corners[2] {
                ry1 -= r + 1;
            }
            checked_fill_rect(im, x1 - w_i64 + 1, ry0, x1, ry1, out_ink)?;
        }
    }

    Ok(())
}

#[derive(Clone, Copy)]
enum ClipNode {
    HalfPlane { a: f64, b: f64, c: f64 },
    And(usize, usize),
    Or(usize, usize),
}

fn clip_tree_transpose(nodes: &mut [ClipNode], idx: usize) {
    match nodes[idx] {
        ClipNode::HalfPlane { a, b, c } => {
            nodes[idx] = ClipNode::HalfPlane { a: b, b: a, c };
        }
        ClipNode::And(l, r) | ClipNode::Or(l, r) => {
            clip_tree_transpose(nodes, l);
            clip_tree_transpose(nodes, r);
        }
    }
}

/// FMA helper matching `_clip_tree_do_clip` at `0x1e494` in `_imaging.cpython-314-darwin.so`.
#[inline]
#[allow(clippy::disallowed_methods)]
fn halfplane_by_plus_c(b: f64, y: i32, c: f64) -> f64 {
    b.mul_add(f64::from(y), c)
}

/// FMA helper matching `_clip_tree_do_clip` at `0x1e4f0..1e4f8` and `0x1e520..1e528` in
/// `_imaging.cpython-314-darwin.so`.
#[inline]
#[allow(clippy::disallowed_methods)]
fn eval_halfplane_fma(a: f64, x: i32, by: f64, c: f64) -> f64 {
    c + a.mul_add(f64::from(x), by)
}

fn clip_tree_do_clip(
    nodes: &[ClipNode],
    root: Option<usize>,
    mut x0: i32,
    y: i32,
    mut x1: i32,
) -> Result<Vec<(i32, i8)>, CoreError> {
    let Some(idx) = root else {
        return Ok(vec![(x0, 1), (x1, -1)]);
    };
    match nodes[idx] {
        ClipNode::HalfPlane { a, b, c } => {
            let eps = 1e-9_f64;
            if a.abs() < eps {
                if halfplane_by_plus_c(b, y, c) < -eps {
                    x0 = 1;
                    x1 = 0;
                }
            } else {
                let ix = -halfplane_by_plus_c(b, y, c) / a;
                let by = b * f64::from(y);
                if eval_halfplane_fma(a, x0, by, c) < eps {
                    x0 = lround_f64(f64::from(x0).max(ix))?;
                }
                if eval_halfplane_fma(a, x1, by, c) < eps {
                    x1 = lround_f64(f64::from(x1).min(ix))?;
                }
            }
            if x0 <= x1 {
                Ok(vec![(x0, 1), (x1, -1)])
            } else {
                Ok(Vec::new())
            }
        }
        ClipNode::And(l, r) | ClipNode::Or(l, r) => {
            let is_or = matches!(nodes[idx], ClipNode::Or(_, _));
            let l1 = clip_tree_do_clip(nodes, Some(l), x0, y, x1)?;
            let l2 = clip_tree_do_clip(nodes, Some(r), x0, y, x1)?;
            let mut out: Vec<(i32, i8)> = Vec::with_capacity(l1.len() + l2.len());
            let mut i1 = 0_usize;
            let mut i2 = 0_usize;
            let mut k1 = 0_i32;
            let mut k2 = 0_i32;
            while i1 < l1.len() || i2 < l2.len() {
                let t = if i2 >= l2.len()
                    || (i1 < l1.len()
                        && (l1[i1].0 < l2[i2].0 || (l1[i1].0 == l2[i2].0 && l1[i1].1 > l2[i2].1)))
                {
                    let ev = l1[i1];
                    k1 += i32::from(ev.1);
                    i1 += 1;
                    ev
                } else {
                    let ev = l2[i2];
                    k2 += i32::from(ev.1);
                    i2 += 1;
                    ev
                };
                let tail_type = out.last().map(|&(_, ty)| ty);
                let keep = if is_or {
                    (t.1 == 1 && (tail_type.is_none() || tail_type == Some(-1)))
                        || (t.1 == -1 && k1 == 0 && k2 == 0)
                } else {
                    (t.1 == 1 && (tail_type.is_none() || tail_type == Some(-1)) && k1 > 0 && k2 > 0)
                        || (t.1 == -1 && tail_type == Some(1) && (k1 == 0 || k2 == 0))
                };
                if keep {
                    out.push(t);
                }
            }
            Ok(out)
        }
    }
}

fn normalize_angles(al: &mut f32, ar: &mut f32) {
    if *ar - *al >= 360.0_f32 {
        *al = 0.0;
        *ar = 360.0;
    } else {
        let al_d = f64::from(*al);
        let ar_d = f64::from(*ar);
        let nal = (if al_d < 0.0 {
            360.0 - ((-al_d) % 360.0)
        } else {
            al_d
        }) % 360.0;
        let al_f = nal as f32;
        let al_f64 = f64::from(al_f);
        let nar = al_f64
            + (if ar_d < al_f64 {
                360.0 - ((al_f64 - ar_d) % 360.0)
            } else {
                ar_d - al_f64
            }) % 360.0;
        *al = al_f;
        *ar = nar as f32;
    }
}

fn build_pie_tree(a: i32, b: i32, al: f32, ar: f32) -> (Vec<ClipNode>, Option<usize>) {
    let xl = f64::from(a) * gxr_math::cos(f64::from(al) * PI / 180.0);
    let xr = f64::from(a) * gxr_math::cos(f64::from(ar) * PI / 180.0);
    let yl = f64::from(b) * gxr_math::sin(f64::from(al) * PI / 180.0);
    let yr = f64::from(b) * gxr_math::sin(f64::from(ar) * PI / 180.0);
    let mut nodes = vec![
        ClipNode::HalfPlane {
            a: -yl,
            b: xl,
            c: 0.0,
        },
        ClipNode::HalfPlane {
            a: yr,
            b: -xr,
            c: 0.0,
        },
    ];
    let mut root = if ar - al < 180.0 {
        nodes.push(ClipNode::And(0, 1));
        2
    } else {
        nodes.push(ClipNode::Or(0, 1));
        2
    };
    if ar - al < 90.0 {
        nodes.push(ClipNode::HalfPlane {
            a: (xl + xr) / 2.0,
            b: (yl + yr) / 2.0,
            c: 0.0,
        });
        let sc = nodes.len() - 1;
        nodes.push(ClipNode::And(root, sc));
        root = nodes.len() - 1;
    }
    (nodes, Some(root))
}

fn build_arc_tree(a: i32, b: i32, mut al: f32, mut ar: f32) -> (Vec<ClipNode>, Option<usize>) {
    if a < b {
        let (mut nodes, root) = build_arc_tree(b, a, 90.0 - ar, 90.0 - al);
        if let Some(r) = root {
            clip_tree_transpose(&mut nodes, r);
        }
        return (nodes, root);
    }
    normalize_angles(&mut al, &mut ar);
    if ar == al + 360.0 {
        return (Vec::new(), None);
    }
    let af = f64::from(a);
    let bf = f64::from(b);
    let al_d = f64::from(al);
    let ar_d = f64::from(ar);
    let mut nodes = vec![
        ClipNode::HalfPlane {
            a: -af * gxr_math::sin(al_d * PI / 180.0),
            b: bf * gxr_math::cos(al_d * PI / 180.0),
            c: (af * af - bf * bf) * gxr_math::sin(al_d * PI / 90.0) / 2.0,
        },
        ClipNode::HalfPlane {
            a: af * gxr_math::sin(ar_d * PI / 180.0),
            b: -bf * gxr_math::cos(ar_d * PI / 180.0),
            c: (bf * bf - af * af) * gxr_math::sin(ar_d * PI / 90.0) / 2.0,
        },
    ];
    let root = if al % 180.0 == 0.0 || ar % 180.0 == 0.0 {
        nodes.push(if ar - al < 180.0 {
            ClipNode::And(0, 1)
        } else {
            ClipNode::Or(0, 1)
        });
        nodes.len() - 1
    } else if (((al / 180.0) as i32) + ((ar / 180.0) as i32)) % 2 == 1 {
        let bl = if ((al / 180.0) as i32) % 2 == 0 {
            1.0
        } else {
            -1.0
        };
        let br = if ((ar / 180.0) as i32) % 2 == 0 {
            1.0
        } else {
            -1.0
        };
        nodes.push(ClipNode::HalfPlane {
            a: 0.0,
            b: bl,
            c: 0.0,
        });
        nodes.push(ClipNode::HalfPlane {
            a: 0.0,
            b: br,
            c: 0.0,
        });
        nodes.push(ClipNode::And(2, 0));
        nodes.push(ClipNode::And(3, 1));
        nodes.push(ClipNode::Or(4, 5));
        nodes.len() - 1
    } else {
        let br = if !(180.0..=540.0).contains(&ar) {
            1.0
        } else {
            -1.0
        };
        nodes.push(ClipNode::HalfPlane {
            a: 0.0,
            b: br,
            c: 0.0,
        });
        nodes.push(if ar - al < 180.0 {
            ClipNode::And(0, 1)
        } else {
            ClipNode::Or(0, 1)
        });
        nodes.push(if ar - al < 180.0 {
            ClipNode::And(3, 2)
        } else {
            ClipNode::Or(3, 2)
        });
        nodes.len() - 1
    };
    (nodes, Some(root))
}

fn draw_pieslice_fill(
    im: &mut InkImage,
    xy: [f64; 4],
    start: f64,
    end: f64,
    ink: [u8; 3],
) -> Result<(), CoreError> {
    if xy.iter().any(|v| !v.is_finite()) || !start.is_finite() || !end.is_finite() {
        return Err(bad_coords("coordinates must be finite"));
    }
    if xy[2] < xy[0] {
        return Err(bad_coords("x1 must be greater than or equal to x0"));
    }
    if xy[3] < xy[1] {
        return Err(bad_coords("y1 must be greater than or equal to y0"));
    }
    let mut al = start as f32;
    let mut ar = end as f32;
    normalize_angles(&mut al, &mut ar);
    if al + 360.0 == ar {
        return draw_ellipse(im, xy, ink);
    }
    if al == ar {
        return Ok(());
    }
    let x0 = float_to_c_int(xy[0])?;
    let y0 = float_to_c_int(xy[1])?;
    let x1 = float_to_c_int(xy[2])?;
    let y1 = float_to_c_int(xy[3])?;
    let a = x1
        .checked_sub(x0)
        .ok_or_else(|| bad_coords("pieslice width out of range"))?;
    let b = y1
        .checked_sub(y0)
        .ok_or_else(|| bad_coords("pieslice height out of range"))?;
    if a < 0 || b < 0 {
        return Ok(());
    }
    let (nodes, root) = build_pie_tree(a, b, al, ar);
    let stroke = i32::try_from(i64::from(a) + i64::from(b)).unwrap_or(i32::MAX);
    let mut st = EllipseState::new(a, b, stroke)?;
    while let Some((lx0, ly, lx1)) = st.next_span() {
        let evs = clip_tree_do_clip(&nodes, root, lx0, ly, lx1)?;
        let mut i = 0_usize;
        while i + 1 < evs.len() {
            hline_px_i64(
                im,
                i64::from(x0) + (i64::from(evs[i].0) + i64::from(a)) / 2,
                i64::from(y0) + (i64::from(ly) + i64::from(b)) / 2,
                i64::from(x0) + (i64::from(evs[i + 1].0) + i64::from(a)) / 2,
                ink,
            );
            i += 2;
        }
    }
    Ok(())
}

fn draw_arc_outline(
    im: &mut InkImage,
    xy: [f64; 4],
    start: f64,
    end: f64,
    ink: [u8; 3],
    width: i32,
) -> Result<(), CoreError> {
    if xy.iter().any(|v| !v.is_finite()) || !start.is_finite() || !end.is_finite() {
        return Err(bad_coords("coordinates must be finite"));
    }
    if xy[2] < xy[0] {
        return Err(bad_coords("x1 must be greater than or equal to x0"));
    }
    if xy[3] < xy[1] {
        return Err(bad_coords("y1 must be greater than or equal to y0"));
    }
    let mut al = start as f32;
    let mut ar = end as f32;
    normalize_angles(&mut al, &mut ar);
    if al + 360.0 == ar {
        return draw_ellipse_outline(im, xy, ink, width);
    }
    if al == ar {
        return Ok(());
    }
    let x0 = float_to_c_int(xy[0])?;
    let y0 = float_to_c_int(xy[1])?;
    let x1 = float_to_c_int(xy[2])?;
    let y1 = float_to_c_int(xy[3])?;
    let a = x1
        .checked_sub(x0)
        .ok_or_else(|| bad_coords("arc width out of range"))?;
    let b = y1
        .checked_sub(y0)
        .ok_or_else(|| bad_coords("arc height out of range"))?;
    if a < 0 || b < 0 {
        return Ok(());
    }
    let (nodes, root) = build_arc_tree(a, b, al, ar);
    let mut st = EllipseState::new(a, b, width)?;
    while let Some((lx0, ly, lx1)) = st.next_span() {
        let evs = clip_tree_do_clip(&nodes, root, lx0, ly, lx1)?;
        let mut i = 0_usize;
        while i + 1 < evs.len() {
            hline_px_i64(
                im,
                i64::from(x0) + (i64::from(evs[i].0) + i64::from(a)) / 2,
                i64::from(y0) + (i64::from(ly) + i64::from(b)) / 2,
                i64::from(x0) + (i64::from(evs[i + 1].0) + i64::from(a)) / 2,
                ink,
            );
            i += 2;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ink::image::InkMode;

    #[test]
    fn rounded_rectangle_handles_extreme_and_negative_radii() {
        let mut im = InkImage::new_filled(InkMode::Rgb, 20, 20, [0, 0, 0, 255]).expect("im");
        // All-corners True clamps large radius to ellipse instead of failing.
        draw_rounded_rectangle(
            &mut im,
            [2.0, 2.0, 17.0, 17.0],
            1e300,
            Some([10, 20, 30]),
            None,
            1,
            [true, true, true, true],
        )
        .expect("all corners clamp to ellipse");
        // Partial corners with 1e300 rejects cleanly.
        assert!(
            draw_rounded_rectangle(
                &mut im,
                [2.0, 2.0, 17.0, 17.0],
                1e300,
                Some([10, 20, 30]),
                None,
                1,
                [true, false, false, false],
            )
            .is_err()
        );
        // Negative radius with corners rejects; with all corners false behaves like rectangle.
        assert!(
            draw_rounded_rectangle(
                &mut im,
                [0.0, 0.0, 10.0, 10.0],
                -5.0,
                Some([1, 2, 3]),
                None,
                1,
                [true, true, true, true],
            )
            .is_err()
        );
        draw_rounded_rectangle(
            &mut im,
            [0.0, 0.0, 10.0, 10.0],
            -5.0,
            Some([1, 2, 3]),
            None,
            1,
            [false, false, false, false],
        )
        .expect("no rounded corners delegates to draw_rectangle");
        assert!(lround_f64(f64::INFINITY).is_err());
        assert!(lround_f64(1e20).is_err());
    }

    fn clip_tree_do_clip_unfused(
        nodes: &[ClipNode],
        root: Option<usize>,
        mut x0: i32,
        y: i32,
        mut x1: i32,
    ) -> Result<Vec<(i32, i8)>, CoreError> {
        let Some(idx) = root else {
            return Ok(vec![(x0, 1), (x1, -1)]);
        };
        match nodes[idx] {
            ClipNode::HalfPlane { a, b, c } => {
                let eps = 1e-9_f64;
                if a.abs() < eps {
                    if b * f64::from(y) + c < -eps {
                        x0 = 1;
                        x1 = 0;
                    }
                } else {
                    let ix = -(b * f64::from(y) + c) / a;
                    let by = b * f64::from(y);
                    if c + (a * f64::from(x0) + by) < eps {
                        x0 = lround_f64(f64::from(x0).max(ix))?;
                    }
                    if c + (a * f64::from(x1) + by) < eps {
                        x1 = lround_f64(f64::from(x1).min(ix))?;
                    }
                }
                if x0 <= x1 {
                    Ok(vec![(x0, 1), (x1, -1)])
                } else {
                    Ok(Vec::new())
                }
            }
            ClipNode::And(_, _) | ClipNode::Or(_, _) => unreachable!("halfplane-only test"),
        }
    }

    #[test]
    fn clip_tree_fma_differs_from_unfused() {
        // Site `clip_by_c`: `b.mul_add(y, c)` vs `b * y + c` flips `ix = -0.5` round boundary.
        let a1 = 1.0_f64 / 16_777_216.0_f64;
        let b1 = 1.0_f64 + f64::EPSILON;
        let c1 = -(b1 * 3.0_f64) + 0.5_f64 * a1;
        let nodes_by_c = [ClipNode::HalfPlane {
            a: a1,
            b: b1,
            c: c1,
        }];
        let fused_1 = clip_tree_do_clip(&nodes_by_c, Some(0), -10, 3, 10).expect("fused");
        let unfused_1 =
            clip_tree_do_clip_unfused(&nodes_by_c, Some(0), -10, 3, 10).expect("unfused");
        assert_eq!(fused_1, vec![(0, 1), (10, -1)]);
        assert_eq!(unfused_1, vec![(-1, 1), (10, -1)]);
        assert_ne!(fused_1, unfused_1);

        // Site `clip_ax_by`: `c + a.mul_add(x, by)` vs `c + (a * x + by)` flips `< eps` at `x1 = 3`.
        let a2 = (1.0_f64 + f64::EPSILON) * 1.5e-9_f64;
        let b2 = -(a2 * 3.0_f64);
        let c2 = 1e-9_f64;
        let nodes_ax_by = [ClipNode::HalfPlane {
            a: a2,
            b: b2,
            c: c2,
        }];
        let fused_2 = clip_tree_do_clip(&nodes_ax_by, Some(0), 0, 1, 3).expect("fused");
        let unfused_2 = clip_tree_do_clip_unfused(&nodes_ax_by, Some(0), 0, 1, 3).expect("unfused");
        assert_eq!(fused_2, vec![(2, 1), (2, -1)]);
        assert_eq!(unfused_2, vec![(2, 1), (3, -1)]);
        assert_ne!(fused_2, unfused_2);
    }
}
