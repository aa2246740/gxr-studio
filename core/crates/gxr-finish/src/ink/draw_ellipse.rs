//! Ellipse state machine and rasterizers ported from Pillow 12.2.0 `src/libImaging/Draw.c`.

use gxr_raw::CoreError;

use crate::ink::draw::{bad_coords, check_mode, float_to_c_int, hline_px_i64};
use crate::ink::image::InkImage;

/// Port of Pillow `ImageDraw.ellipse(xy, fill=ink)` (filled ellipse).
pub fn draw_ellipse(im: &mut InkImage, xy: [f64; 4], ink: [u8; 3]) -> Result<(), CoreError> {
    check_mode(im)?;
    if xy.iter().any(|v| !v.is_finite()) {
        return Err(bad_coords("coordinates must be finite"));
    }
    if xy[2] < xy[0] {
        return Err(bad_coords("x1 must be greater than or equal to x0"));
    }
    if xy[3] < xy[1] {
        return Err(bad_coords("y1 must be greater than or equal to y0"));
    }
    let x0 = float_to_c_int(xy[0])?;
    let y0 = float_to_c_int(xy[1])?;
    let x1 = float_to_c_int(xy[2])?;
    let y1 = float_to_c_int(xy[3])?;
    let a = x1
        .checked_sub(x0)
        .ok_or_else(|| bad_coords("ellipse width out of range"))?;
    let b = y1
        .checked_sub(y0)
        .ok_or_else(|| bad_coords("ellipse height out of range"))?;
    if a < 0 || b < 0 {
        return Ok(());
    }
    let stroke = a
        .checked_add(b)
        .ok_or_else(|| bad_coords("ellipse size out of range"))?;
    let mut st = EllipseState::new(a, b, stroke)?;
    while let Some((lx0, ly, lx1)) = st.next_span() {
        hline_px_i64(
            im,
            i64::from(x0) + (i64::from(lx0) + i64::from(a)) / 2,
            i64::from(y0) + (i64::from(ly) + i64::from(b)) / 2,
            i64::from(x0) + (i64::from(lx1) + i64::from(a)) / 2,
            ink,
        );
    }
    Ok(())
}

pub(crate) fn draw_ellipse_outline(
    im: &mut InkImage,
    xy: [f64; 4],
    ink: [u8; 3],
    width: i32,
) -> Result<(), CoreError> {
    if xy.iter().any(|v| !v.is_finite()) {
        return Err(bad_coords("coordinates must be finite"));
    }
    if xy[2] < xy[0] {
        return Err(bad_coords("x1 must be greater than or equal to x0"));
    }
    if xy[3] < xy[1] {
        return Err(bad_coords("y1 must be greater than or equal to y0"));
    }
    let x0 = float_to_c_int(xy[0])?;
    let y0 = float_to_c_int(xy[1])?;
    let x1 = float_to_c_int(xy[2])?;
    let y1 = float_to_c_int(xy[3])?;
    let a = x1
        .checked_sub(x0)
        .ok_or_else(|| bad_coords("ellipse width out of range"))?;
    let b = y1
        .checked_sub(y0)
        .ok_or_else(|| bad_coords("ellipse height out of range"))?;
    if a < 0 || b < 0 {
        return Ok(());
    }
    let mut st = EllipseState::new(a, b, width)?;
    while let Some((lx0, ly, lx1)) = st.next_span() {
        hline_px_i64(
            im,
            i64::from(x0) + (i64::from(lx0) + i64::from(a)) / 2,
            i64::from(y0) + (i64::from(ly) + i64::from(b)) / 2,
            i64::from(x0) + (i64::from(lx1) + i64::from(a)) / 2,
            ink,
        );
    }
    Ok(())
}

struct QuarterState {
    cx: i32,
    cy: i32,
    ex: i32,
    ey: i32,
    a2: i64,
    b2: i64,
    a2b2: i64,
    finished: bool,
}

impl QuarterState {
    fn new(a: i32, b: i32) -> Result<Self, CoreError> {
        if a < 0 || b < 0 {
            Ok(Self {
                cx: 0,
                cy: 0,
                ex: 0,
                ey: 0,
                a2: 0,
                b2: 0,
                a2b2: 0,
                finished: true,
            })
        } else {
            let a2 = i64::from(a) * i64::from(a);
            let b2 = i64::from(b) * i64::from(b);
            let a2b2 = a2
                .checked_mul(b2)
                .ok_or_else(|| bad_coords("ellipse size out of range"))?;
            Ok(Self {
                cx: a,
                cy: b % 2,
                ex: a % 2,
                ey: b,
                a2,
                b2,
                a2b2,
                finished: false,
            })
        }
    }

    #[inline]
    fn delta(&self, x: i32, y: i32) -> i128 {
        let xi = i128::from(x);
        let yi = i128::from(y);
        (i128::from(self.a2) * yi * yi + i128::from(self.b2) * xi * xi - i128::from(self.a2b2))
            .abs()
    }

    fn next_pt(&mut self) -> Option<(i32, i32)> {
        if self.finished {
            return None;
        }
        let rx = self.cx;
        let ry = self.cy;
        if self.cx == self.ex && self.cy == self.ey {
            self.finished = true;
        } else {
            let mut nx = self.cx;
            let mut ny = self.cy + 2;
            let mut ndelta = self.delta(nx, ny);
            if nx > 1 {
                let d1 = self.delta(self.cx - 2, self.cy + 2);
                if ndelta > d1 {
                    nx = self.cx - 2;
                    ny = self.cy + 2;
                    ndelta = d1;
                }
                let d2 = self.delta(self.cx - 2, self.cy);
                if ndelta > d2 {
                    nx = self.cx - 2;
                    ny = self.cy;
                }
            }
            self.cx = nx;
            self.cy = ny;
        }
        Some((rx, ry))
    }
}

pub(crate) struct EllipseState {
    st_o: QuarterState,
    st_i: QuarterState,
    py: i32,
    pl: i32,
    pr: i32,
    cy: [i32; 4],
    cl: [i32; 4],
    cr: [i32; 4],
    bufcnt: usize,
    finished: bool,
    leftmost: i32,
}

impl EllipseState {
    pub(crate) fn new(a: i32, b: i32, w: i32) -> Result<Self, CoreError> {
        let leftmost = a % 2;
        let mut st_o = QuarterState::new(a, b)?;
        if w < 1 {
            Ok(Self {
                st_o,
                st_i: QuarterState::new(-1, -1)?,
                py: 0,
                pl: 0,
                pr: 0,
                cy: [0; 4],
                cl: [0; 4],
                cr: [0; 4],
                bufcnt: 0,
                finished: true,
                leftmost,
            })
        } else if let Some((pr, py)) = st_o.next_pt() {
            let wm1 = i64::from(w) - 1;
            let ia = i32::try_from(i64::from(a) - 2 * wm1).unwrap_or(-1).max(-1);
            let ib = i32::try_from(i64::from(b) - 2 * wm1).unwrap_or(-1).max(-1);
            Ok(Self {
                st_o,
                st_i: QuarterState::new(ia, ib)?,
                py,
                pl: leftmost,
                pr,
                cy: [0; 4],
                cl: [0; 4],
                cr: [0; 4],
                bufcnt: 0,
                finished: false,
                leftmost,
            })
        } else {
            Ok(Self {
                st_o,
                st_i: QuarterState::new(-1, -1)?,
                py: 0,
                pl: 0,
                pr: 0,
                cy: [0; 4],
                cl: [0; 4],
                cr: [0; 4],
                bufcnt: 0,
                finished: true,
                leftmost,
            })
        }
    }

    pub(crate) fn next_span(&mut self) -> Option<(i32, i32, i32)> {
        if self.bufcnt == 0 {
            if self.finished {
                return None;
            }
            let y = self.py;
            let mut l = self.pl;
            let r = self.pr;
            loop {
                match self.st_o.next_pt() {
                    None => {
                        self.finished = true;
                        break;
                    }
                    Some((cx, cy)) if cy > y => {
                        self.pr = cx;
                        self.py = cy;
                        break;
                    }
                    _ => {}
                }
            }
            loop {
                match self.st_i.next_pt() {
                    None => {
                        self.pl = self.leftmost;
                        break;
                    }
                    Some((cx, cy)) if cy <= y => {
                        l = cx;
                    }
                    Some((cx, _)) => {
                        self.pl = cx;
                        break;
                    }
                }
            }

            if (l > 0 || l < r) && y > 0 {
                self.cl[self.bufcnt] = if l == 0 { 2 } else { l };
                self.cy[self.bufcnt] = y;
                self.cr[self.bufcnt] = r;
                self.bufcnt += 1;
            }
            if y > 0 {
                self.cl[self.bufcnt] = -r;
                self.cy[self.bufcnt] = y;
                self.cr[self.bufcnt] = -l;
                self.bufcnt += 1;
            }
            if l > 0 || l < r {
                self.cl[self.bufcnt] = if l == 0 { 2 } else { l };
                self.cy[self.bufcnt] = -y;
                self.cr[self.bufcnt] = r;
                self.bufcnt += 1;
            }
            self.cl[self.bufcnt] = -r;
            self.cy[self.bufcnt] = -y;
            self.cr[self.bufcnt] = -l;
            self.bufcnt += 1;
        }
        self.bufcnt -= 1;
        Some((
            self.cl[self.bufcnt],
            self.cy[self.bufcnt],
            self.cr[self.bufcnt],
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ink::image::InkMode;

    #[test]
    fn rejects_extreme_ellipse_and_handles_boundary_widths() {
        let mut im = InkImage::new_filled(InkMode::Rgb, 8, 8, [0, 0, 0, 255]).expect("im");
        assert!(draw_ellipse(&mut im, [0.0, 0.0, 65536.0, 65536.0], [1, 2, 3]).is_err());
        // `50000 x 50000` fits a2*b2 in i64 and delta needs i128 without overflow.
        let mut st = QuarterState::new(50_000, 50_000).expect("50k fits");
        assert!(st.next_pt().is_some());
        // Large outline width must not overflow `2 * (w - 1)`.
        draw_ellipse_outline(&mut im, [1.0, 1.0, 6.0, 6.0], [9, 8, 7], i32::MAX).expect("ok");
    }
}
