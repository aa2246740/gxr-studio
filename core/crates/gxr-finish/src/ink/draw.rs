//! Port of Pillow 12.2.0 `src/libImaging/Draw.c`, `src/_imaging.c` (`_draw_*`), and
//! `src/PIL/ImageDraw.py` for `L` and `RGB`: `polygon` fill, `rectangle` fill + outline,
//! and `line` (axis-aligned + wide diagonal). See `draw_arc.rs` for `ellipse` and
//! `rounded_rectangle`.

use gxr_raw::CoreError;

use crate::ink::image::{InkImage, InkMode, bad_mismatch};

pub(crate) fn bad_coords(msg: &str) -> CoreError {
    CoreError::new("value_error", msg)
}

#[inline]
pub(crate) fn float_to_c_int(v: f64) -> Result<i32, CoreError> {
    if !v.is_finite() {
        return Err(bad_coords("coordinates must be finite"));
    }
    if v < f64::from(i32::MIN) || v > f64::from(i32::MAX) {
        return Err(bad_coords("coordinate out of range"));
    }
    Ok(v as i32)
}

#[inline]
pub(crate) fn check_mode(im: &InkImage) -> Result<(), CoreError> {
    match im.mode {
        InkMode::L | InkMode::Rgb => {}
        InkMode::Rgba => return Err(bad_mismatch("draw primitives support L and RGB")),
    }
    if i32::try_from(im.width).is_err() || i32::try_from(im.height).is_err() {
        return Err(bad_coords("image dimensions exceed i32"));
    }
    Ok(())
}

#[inline]
pub(crate) fn same_ink(mode: InkMode, a: [u8; 3], b: [u8; 3]) -> bool {
    match mode {
        InkMode::L => a[0] == b[0],
        InkMode::Rgb | InkMode::Rgba => a == b,
    }
}

#[inline]
fn round_up_f32(f: f32) -> i32 {
    if f >= 0.0_f32 {
        f64::from(f + 0.5_f32).floor() as i32
    } else {
        -((f64::from(f).abs() + 0.5_f64).floor() as i32)
    }
}

#[inline]
fn round_down_f32(f: f32) -> i32 {
    if f >= 0.0_f32 {
        f64::from(f - 0.5_f32).ceil() as i32
    } else {
        -((f64::from(f).abs() - 0.5_f64).ceil() as i32)
    }
}

#[inline]
fn round_up_f64(f: f64) -> i32 {
    if f >= 0.0_f64 {
        (f + 0.5_f64).floor() as i32
    } else {
        -((f.abs() + 0.5_f64).floor() as i32)
    }
}

#[inline]
fn round_down_f64(f: f64) -> i32 {
    if f >= 0.0_f64 {
        (f - 0.5_f64).ceil() as i32
    } else {
        -((f.abs() - 0.5_f64).ceil() as i32)
    }
}

/// Contracted `ROUND_DOWN(a * b)` matching `_ImagingDrawWideLine`
/// (`0x1cb30`, `0x1cb5c`, `0x1cb84`, `0x1cbac` in `_imaging.cpython-314-darwin.so`):
/// non-negative branch contracts `fma(a, b, -0.5)` (`fmsub`), negative branch is unfused
/// `fabs(a * b) - 0.5`.
#[inline]
#[allow(clippy::disallowed_methods)]
fn round_down_mul_f64(a: f64, b: f64) -> Result<i32, CoreError> {
    let prod = a * b;
    let rounded = if prod >= 0.0_f64 {
        a.mul_add(b, -0.5_f64).ceil()
    } else {
        -((prod.abs() - 0.5_f64).ceil())
    };
    if !rounded.is_finite() || rounded < f64::from(i32::MIN) || rounded > f64::from(i32::MAX) {
        return Err(bad_coords("wide line coordinate out of range"));
    }
    Ok(rounded as i32)
}

#[inline]
fn point_px(im: &mut InkImage, x: i32, y: i32, ink: [u8; 3]) {
    if x < 0 || y < 0 {
        return;
    }
    let xu = x as usize;
    let yu = y as usize;
    if xu >= im.width || yu >= im.height {
        return;
    }
    match im.mode {
        InkMode::L => im.data[yu * im.width + xu] = ink[0],
        InkMode::Rgb => {
            let off = (yu * im.width + xu) * 3;
            im.data[off] = ink[0];
            im.data[off + 1] = ink[1];
            im.data[off + 2] = ink[2];
        }
        InkMode::Rgba => {
            let off = (yu * im.width + xu) * 4;
            im.data[off] = ink[0];
            im.data[off + 1] = ink[1];
            im.data[off + 2] = ink[2];
            im.data[off + 3] = 255;
        }
    }
}

#[inline]
pub(crate) fn hline_px(im: &mut InkImage, x0: i32, y0: i32, x1: i32, ink: [u8; 3]) {
    hline_px_i64(im, i64::from(x0), i64::from(y0), i64::from(x1), ink);
}

#[inline]
pub(crate) fn hline_px_i64(im: &mut InkImage, mut x0: i64, y0: i64, mut x1: i64, ink: [u8; 3]) {
    if im.width == 0 || im.height == 0 || y0 < 0 || y0 >= im.height as i64 {
        return;
    }
    let w_i64 = im.width as i64;
    if x0 < 0 {
        x0 = 0;
    } else if x0 >= w_i64 {
        return;
    }
    if x1 < 0 {
        return;
    } else if x1 >= w_i64 {
        x1 = w_i64 - 1;
    }
    if x0 > x1 {
        return;
    }
    let xu0 = x0 as usize;
    let xu1 = x1 as usize;
    let yu = y0 as usize;
    match im.mode {
        InkMode::L => {
            let row = yu * im.width;
            im.data[row + xu0..=row + xu1].fill(ink[0]);
        }
        InkMode::Rgb => {
            let row = yu * im.width * 3;
            for x in xu0..=xu1 {
                let off = row + x * 3;
                im.data[off] = ink[0];
                im.data[off + 1] = ink[1];
                im.data[off + 2] = ink[2];
            }
        }
        InkMode::Rgba => {
            let row = yu * im.width * 4;
            for x in xu0..=xu1 {
                let off = row + x * 4;
                im.data[off] = ink[0];
                im.data[off + 1] = ink[1];
                im.data[off + 2] = ink[2];
                im.data[off + 3] = 255;
            }
        }
    }
}

#[inline]
fn vline_half_open_i64(im: &mut InkImage, x: i64, y_start: i64, y_end: i64, ink: [u8; 3]) {
    if im.width == 0 || im.height == 0 || x < 0 || x >= im.width as i64 || y_start == y_end {
        return;
    }
    let h_i64 = im.height as i64;
    let (lo, hi_incl) = if y_start < y_end {
        (y_start.max(0), (y_end - 1).min(h_i64 - 1))
    } else {
        ((y_end + 1).max(0), y_start.min(h_i64 - 1))
    };
    if lo > hi_incl {
        return;
    }
    let xu = x as usize;
    for yu in (lo as usize)..=(hi_incl as usize) {
        match im.mode {
            InkMode::L => im.data[yu * im.width + xu] = ink[0],
            InkMode::Rgb => {
                let off = (yu * im.width + xu) * 3;
                im.data[off] = ink[0];
                im.data[off + 1] = ink[1];
                im.data[off + 2] = ink[2];
            }
            InkMode::Rgba => {
                let off = (yu * im.width + xu) * 4;
                im.data[off] = ink[0];
                im.data[off + 1] = ink[1];
                im.data[off + 2] = ink[2];
                im.data[off + 3] = 255;
            }
        }
    }
}

fn line_px(
    im: &mut InkImage,
    mut x0: i32,
    mut y0: i32,
    x1: i32,
    y1: i32,
    ink: [u8; 3],
) -> Result<(), CoreError> {
    let raw_dx = x1
        .checked_sub(x0)
        .ok_or_else(|| bad_coords("line coordinate overflow"))?;
    let raw_dy = y1
        .checked_sub(y0)
        .ok_or_else(|| bad_coords("line coordinate overflow"))?;
    let mut dx = i64::from(raw_dx);
    let xs = if dx < 0 {
        dx = -dx;
        -1
    } else {
        1
    };
    let mut dy = i64::from(raw_dy);
    let ys = if dy < 0 {
        dy = -dy;
        -1
    } else {
        1
    };

    if dx == 0 {
        vline_half_open_i64(im, i64::from(x0), i64::from(y0), i64::from(y1), ink);
    } else if dy == 0 {
        if xs > 0 {
            hline_px_i64(im, i64::from(x0), i64::from(y0), i64::from(x1) - 1, ink);
        } else {
            hline_px_i64(im, i64::from(x1) + 1, i64::from(y0), i64::from(x0), ink);
        }
    } else if dx > dy {
        let n = dx;
        dy += dy;
        let mut e = dy - dx;
        dx += dx;
        for _ in 0..n {
            point_px(im, x0, y0, ink);
            if e >= 0 {
                y0 += ys;
                e -= dx;
            }
            e += dy;
            x0 += xs;
        }
    } else {
        let n = dy;
        dx += dx;
        let mut e = dx - dy;
        dy += dy;
        for _ in 0..n {
            point_px(im, x0, y0, ink);
            if e >= 0 {
                x0 += xs;
                e -= dy;
            }
            e += dx;
            y0 += ys;
        }
    }
    Ok(())
}

#[derive(Clone, Copy, Debug)]
struct Edge {
    x0: i32,
    y0: i32,
    xmin: i32,
    ymin: i32,
    xmax: i32,
    ymax: i32,
    dx: f32,
}

fn make_edge(x0: i32, y0: i32, x1: i32, y1: i32) -> Result<Edge, CoreError> {
    let dx_i = x1
        .checked_sub(x0)
        .ok_or_else(|| bad_coords("edge coordinate overflow"))?;
    let dy_i = y1
        .checked_sub(y0)
        .ok_or_else(|| bad_coords("edge coordinate overflow"))?;
    let (xmin, xmax) = if x0 <= x1 { (x0, x1) } else { (x1, x0) };
    let (ymin, ymax) = if y0 <= y1 { (y0, y1) } else { (y1, y0) };
    let dx = if dy_i == 0 {
        0.0_f32
    } else {
        (dx_i as f32) / (dy_i as f32)
    };
    Ok(Edge {
        x0,
        y0,
        xmin,
        ymin,
        xmax,
        ymax,
        dx,
    })
}

/// Edge scanline X coordinate matching `_polygon_generic`
/// (`0x1d0bc`, `0x1d158`, `0x1d188`, `0x1d18c` in `_imaging.cpython-314-darwin.so`).
#[inline]
#[allow(clippy::disallowed_methods)]
fn edge_x_f32(y: i32, offset: i32, e: &Edge) -> f32 {
    let dy = (i64::from(y) + i64::from(offset) - i64::from(e.y0)) as f32;
    dy.mul_add(e.dx, e.x0 as f32)
}

fn polygon_generic(im: &mut InkImage, edges: &[Edge], ink: [u8; 3]) {
    if edges.is_empty() || im.width == 0 || im.height == 0 {
        return;
    }
    let h_i32 = im.height as i32;
    let mut ymin = h_i32 - 1;
    let mut ymax = 0_i32;
    let mut edge_table: Vec<&Edge> = Vec::with_capacity(edges.len());

    for e in edges {
        if ymin > e.ymin {
            ymin = e.ymin;
        }
        if ymax < e.ymax {
            ymax = e.ymax;
        }
        if e.ymin == e.ymax {
            hline_px(im, e.xmin, e.ymin, e.xmax, ink);
            continue;
        }
        edge_table.push(e);
    }

    if ymin < 0 {
        ymin = 0;
    }
    if ymax > h_i32 {
        ymax = h_i32;
    }

    let mut xx: Vec<f32> = vec![0.0; edge_table.len() * 2];
    for y in ymin..=ymax {
        let mut j = 0_usize;
        for (i, current) in edge_table.iter().enumerate() {
            if y >= current.ymin && y <= current.ymax {
                xx[j] = edge_x_f32(y, 0, current);
                j += 1;

                if y == current.ymax && y < ymax {
                    xx[j] = xx[j - 1];
                    j += 1;
                } else if (y == current.ymin || y == current.ymax) && current.dx != 0.0_f32 {
                    for other_edge in &edge_table[..i] {
                        if (y != other_edge.ymin && y != other_edge.ymax)
                            || other_edge.dx == 0.0_f32
                        {
                            continue;
                        }
                        let other_x = edge_x_f32(y, 0, other_edge);
                        if xx[j - 1].round() == other_x.round() {
                            let offset = if y == current.ymax { -1 } else { 1 };
                            let adj_x = edge_x_f32(y, offset, current);
                            let y_off = i64::from(y) + i64::from(offset);
                            if y_off >= i64::from(other_edge.ymin)
                                && y_off <= i64::from(other_edge.ymax)
                            {
                                let adj_other = edge_x_f32(y, offset, other_edge);
                                if xx[j - 1] > adj_x + 1.0_f32 && xx[j - 1] > adj_other + 1.0_f32 {
                                    xx[j - 1] = adj_x.max(adj_other).round() + 1.0_f32;
                                } else if xx[j - 1] < adj_x - 1.0_f32
                                    && xx[j - 1] < adj_other - 1.0_f32
                                {
                                    xx[j - 1] = adj_x.min(adj_other).round() - 1.0_f32;
                                }
                                break;
                            }
                        }
                    }
                }
            }
        }
        xx[..j].sort_by(f32::total_cmp);
        let mut i = 1_usize;
        while i < j {
            hline_px(im, round_up_f32(xx[i - 1]), y, round_down_f32(xx[i]), ink);
            i += 2;
        }
    }
}

/// Port of Pillow `ImageDraw.polygon(..., fill=ink)` (no outline).
pub fn draw_polygon(im: &mut InkImage, pts: &[[f64; 2]], ink: [u8; 3]) -> Result<(), CoreError> {
    check_mode(im)?;
    if pts.len() < 2 {
        return Err(bad_coords(
            "coordinate list must contain at least 2 coordinates",
        ));
    }
    let mut ixy = Vec::with_capacity(pts.len() * 2);
    for p in pts {
        ixy.push(float_to_c_int(p[0])?);
        ixy.push(float_to_c_int(p[1])?);
    }
    let count = pts.len();
    let mut edges: Vec<Edge> = Vec::with_capacity(count);
    for i in 0..count - 1 {
        let x0 = ixy[i * 2];
        let y0 = ixy[i * 2 + 1];
        let x1 = ixy[i * 2 + 2];
        let y1 = ixy[i * 2 + 3];
        if y0 == y1 && i != 0 && y0 == ixy[i * 2 - 1] {
            let last_e = edges.last_mut().expect("i > 0 has prior edge");
            if x1 > x0 && x0 > ixy[i * 2 - 2] {
                last_e.xmax = x1;
                continue;
            } else if x1 < x0 && x0 < ixy[i * 2 - 2] {
                last_e.xmin = x1;
                continue;
            }
        }
        edges.push(make_edge(x0, y0, x1, y1)?);
    }
    let last = count - 1;
    if ixy[last * 2] != ixy[0] || ixy[last * 2 + 1] != ixy[1] {
        edges.push(make_edge(ixy[last * 2], ixy[last * 2 + 1], ixy[0], ixy[1])?);
    }
    polygon_generic(im, &edges, ink);
    Ok(())
}

/// Port of Pillow `ImageDraw.rectangle(xy, fill=..., outline=..., width=...)`.
pub fn draw_rectangle(
    im: &mut InkImage,
    xy: [f64; 4],
    fill: Option<[u8; 3]>,
    outline: Option<[u8; 3]>,
    width: i32,
) -> Result<(), CoreError> {
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

    if let Some(fill_ink) = fill {
        draw_rect_c(im, x0, y0, x1, y1, fill_ink, true, 0);
    }
    if let Some(out_ink) = outline
        && !fill.is_some_and(|f| same_ink(im.mode, out_ink, f))
        && width != 0
    {
        draw_rect_c(im, x0, y0, x1, y1, out_ink, false, width);
    }
    Ok(())
}

pub(crate) fn draw_rect_fill_i64(
    im: &mut InkImage,
    x0: i64,
    mut y0: i64,
    x1: i64,
    mut y1: i64,
    ink: [u8; 3],
) {
    if y0 > y1 {
        std::mem::swap(&mut y0, &mut y1);
    }
    if im.width == 0 || im.height == 0 {
        return;
    }
    let img_h = im.height as i64;
    if y0 < 0 {
        y0 = 0;
    } else if y0 >= img_h {
        return;
    }
    if y1 < 0 {
        return;
    } else if y1 >= img_h {
        y1 = img_h - 1;
    }
    for y in y0..=y1 {
        hline_px_i64(im, x0, y, x1, ink);
    }
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn draw_rect_c(
    im: &mut InkImage,
    x0: i32,
    mut y0: i32,
    x1: i32,
    mut y1: i32,
    ink: [u8; 3],
    fill: bool,
    mut width: i32,
) {
    if y0 > y1 {
        std::mem::swap(&mut y0, &mut y1);
    }
    if im.width == 0 || im.height == 0 {
        return;
    }
    if fill {
        draw_rect_fill_i64(
            im,
            i64::from(x0),
            i64::from(y0),
            i64::from(x1),
            i64::from(y1),
            ink,
        );
    } else {
        if width < 0 {
            return;
        }
        if width == 0 {
            width = 1;
        }
        let w_i64 = i64::from(width);
        let x0_64 = i64::from(x0);
        let y0_64 = i64::from(y0);
        let x1_64 = i64::from(x1);
        let y1_64 = i64::from(y1);
        let img_w = im.width as i64;
        let img_h = im.height as i64;

        // Top horizontal stroke: i in 0..width where y0 + i in 0..img_h
        let top_start = (-y0_64).clamp(0, w_i64);
        let top_end = (img_h - y0_64).clamp(0, w_i64);
        for i in top_start..top_end {
            hline_px_i64(im, x0_64, y0_64 + i, x1_64, ink);
        }

        // Bottom horizontal stroke: i in 0..width where y1 - i in 0..img_h
        let bot_start = (y1_64 - img_h + 1).clamp(0, w_i64);
        let bot_end = (y1_64 + 1).clamp(0, w_i64);
        for i in bot_start..bot_end {
            hline_px_i64(im, x0_64, y1_64 - i, x1_64, ink);
        }

        // Right & left vertical strokes clamped to visible X columns
        let v_a = y0_64 + w_i64;
        let v_b = y1_64 - w_i64 + 1;
        let r_start = (x1_64 - img_w + 1).clamp(0, w_i64);
        let r_end = (x1_64 + 1).clamp(0, w_i64);
        for i in r_start..r_end {
            vline_half_open_i64(im, x1_64 - i, v_a, v_b, ink);
        }
        let l_start = (-x0_64).clamp(0, w_i64);
        let l_end = (img_w - x0_64).clamp(0, w_i64);
        for i in l_start..l_end {
            vline_half_open_i64(im, x0_64 + i, v_a, v_b, ink);
        }
    }
}

/// Port of Pillow `ImageDraw.line(xy, fill=ink, width=width)` (axis-aligned & diagonal wide).
pub fn draw_line(
    im: &mut InkImage,
    pts: &[[f64; 2]],
    ink: [u8; 3],
    width: i32,
) -> Result<(), CoreError> {
    check_mode(im)?;
    if pts.is_empty() {
        return Ok(());
    }
    let mut ixy = Vec::with_capacity(pts.len());
    for p in pts {
        ixy.push([float_to_c_int(p[0])?, float_to_c_int(p[1])?]);
    }
    if width <= 1 {
        let mut last = None;
        for w in ixy.windows(2) {
            line_px(im, w[0][0], w[0][1], w[1][0], w[1][1], ink)?;
            last = Some(w[1]);
        }
        if let Some(p) = last {
            point_px(im, p[0], p[1], ink);
        }
    } else {
        for w in ixy.windows(2) {
            wide_line(im, w[0][0], w[0][1], w[1][0], w[1][1], ink, width)?;
        }
    }
    Ok(())
}

fn wide_line(
    im: &mut InkImage,
    x0: i32,
    y0: i32,
    x1: i32,
    y1: i32,
    ink: [u8; 3],
    width: i32,
) -> Result<(), CoreError> {
    let dx = x1
        .checked_sub(x0)
        .ok_or_else(|| bad_coords("wide line dx overflow"))?;
    let dy = y1
        .checked_sub(y0)
        .ok_or_else(|| bad_coords("wide line dy overflow"))?;
    if dx == 0 && dy == 0 {
        point_px(im, x0, y0, ink);
        return Ok(());
    }
    let wm1 = width
        .checked_sub(1)
        .ok_or_else(|| bad_coords("wide line width underflow"))?;
    let big_hypotenuse = gxr_math::hypot(f64::from(dx), f64::from(dy));
    let small_hypotenuse = f64::from(wm1) / 2.0_f64;
    let ratio_max = f64::from(round_up_f64(small_hypotenuse)) / big_hypotenuse;
    let ratio_min = f64::from(round_down_f64(small_hypotenuse)) / big_hypotenuse;

    let dxmin = round_down_mul_f64(ratio_min, f64::from(dy))?;
    let dxmax = round_down_mul_f64(ratio_max, f64::from(dy))?;
    let dymin = round_down_mul_f64(ratio_min, f64::from(dx))?;
    let dymax = round_down_mul_f64(ratio_max, f64::from(dx))?;

    let v0x = x0
        .checked_sub(dxmin)
        .ok_or_else(|| bad_coords("wide line vertex overflow"))?;
    let v0y = y0
        .checked_add(dymax)
        .ok_or_else(|| bad_coords("wide line vertex overflow"))?;
    let v1x = x1
        .checked_sub(dxmin)
        .ok_or_else(|| bad_coords("wide line vertex overflow"))?;
    let v1y = y1
        .checked_add(dymax)
        .ok_or_else(|| bad_coords("wide line vertex overflow"))?;
    let v2x = x1
        .checked_add(dxmax)
        .ok_or_else(|| bad_coords("wide line vertex overflow"))?;
    let v2y = y1
        .checked_sub(dymin)
        .ok_or_else(|| bad_coords("wide line vertex overflow"))?;
    let v3x = x0
        .checked_add(dxmax)
        .ok_or_else(|| bad_coords("wide line vertex overflow"))?;
    let v3y = y0
        .checked_sub(dymin)
        .ok_or_else(|| bad_coords("wide line vertex overflow"))?;

    let edges = [
        make_edge(v0x, v0y, v1x, v1y)?,
        make_edge(v1x, v1y, v2x, v2y)?,
        make_edge(v2x, v2y, v3x, v3y)?,
        make_edge(v3x, v3y, v0x, v0y)?,
    ];
    polygon_generic(im, &edges, ink);
    Ok(())
}

pub use crate::ink::draw_ellipse::draw_ellipse;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wide_line_and_polygon_fma_and_bounds() {
        // Negative/non-negative FMA sensitivity in `round_down_mul_f64`:
        let a = 1.5_f64 / gxr_math::hypot(1.0, 2.0);
        let b = 2.0_f64;
        assert_eq!(round_down_mul_f64(a, b).expect("ok"), 1);

        // Edge X FMA helper works with negative dy without overflow:
        let e = make_edge(-10, -10, 10, 10).expect("edge");
        assert_eq!(edge_x_f32(0, 0, &e), 0.0_f32);

        // Extreme coordinate subtraction fails cleanly instead of overflowing/hanging:
        let mut im = InkImage::new_filled(InkMode::Rgb, 16, 16, [0, 0, 0, 255]).expect("im");
        assert!(
            draw_line(
                &mut im,
                &[[f64::from(i32::MIN), 0.0], [f64::from(i32::MAX), 1.0]],
                [255, 0, 0],
                3,
            )
            .is_err()
        );
        // Huge rectangle width completes without hanging:
        draw_rectangle(
            &mut im,
            [0.0, 0.0, 15.0, 15.0],
            None,
            Some([1, 2, 3]),
            i32::MAX,
        )
        .expect("ok");
        assert_eq!(im.as_slice()[0..3], [1, 2, 3]);
    }
}
