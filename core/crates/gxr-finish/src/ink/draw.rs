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
        InkMode::L | InkMode::Rgb => Ok(()),
        InkMode::Rgba => Err(bad_mismatch("draw primitives support L and RGB")),
    }
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
pub(crate) fn hline_px(im: &mut InkImage, mut x0: i32, y0: i32, mut x1: i32, ink: [u8; 3]) {
    if y0 < 0 || (y0 as usize) >= im.height || im.width == 0 {
        return;
    }
    let w_i32 = im.width as i32;
    if x0 < 0 {
        x0 = 0;
    } else if x0 >= w_i32 {
        return;
    }
    if x1 < 0 {
        return;
    } else if x1 >= w_i32 {
        x1 = w_i32 - 1;
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

fn line_px(im: &mut InkImage, mut x0: i32, mut y0: i32, x1: i32, y1: i32, ink: [u8; 3]) {
    let mut dx = x1 - x0;
    let xs = if dx < 0 {
        dx = -dx;
        -1
    } else {
        1
    };
    let mut dy = y1 - y0;
    let ys = if dy < 0 {
        dy = -dy;
        -1
    } else {
        1
    };

    if dx == 0 {
        for _ in 0..dy {
            point_px(im, x0, y0, ink);
            y0 += ys;
        }
    } else if dy == 0 {
        for _ in 0..dx {
            point_px(im, x0, y0, ink);
            x0 += xs;
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

fn make_edge(x0: i32, y0: i32, x1: i32, y1: i32) -> Edge {
    let (xmin, xmax) = if x0 <= x1 { (x0, x1) } else { (x1, x0) };
    let (ymin, ymax) = if y0 <= y1 { (y0, y1) } else { (y1, y0) };
    let dx = if y0 == y1 {
        0.0_f32
    } else {
        ((x1 - x0) as f32) / ((y1 - y0) as f32)
    };
    Edge {
        x0,
        y0,
        xmin,
        ymin,
        xmax,
        ymax,
        dx,
    }
}

fn polygon_generic(im: &mut InkImage, edges: &[Edge], ink: [u8; 3]) {
    if edges.is_empty() || im.width == 0 || im.height == 0 {
        return;
    }
    let mut ymin = (im.height as i32) - 1;
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
    let h_i32 = im.height as i32;
    if ymax > h_i32 {
        ymax = h_i32;
    }

    let mut xx: Vec<f32> = vec![0.0; edge_table.len() * 2];
    for y in ymin..=ymax {
        let mut j = 0_usize;
        for (i, current) in edge_table.iter().enumerate() {
            if y >= current.ymin && y <= current.ymax {
                xx[j] = ((y - current.y0) as f32) * current.dx + (current.x0 as f32);
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
                        let other_x =
                            ((y - other_edge.y0) as f32) * other_edge.dx + (other_edge.x0 as f32);
                        if xx[j - 1].round() == other_x.round() {
                            let offset = if y == current.ymax { -1 } else { 1 };
                            let adj_x = ((y + offset - current.y0) as f32) * current.dx
                                + (current.x0 as f32);
                            if y + offset >= other_edge.ymin && y + offset <= other_edge.ymax {
                                let adj_other = ((y + offset - other_edge.y0) as f32)
                                    * other_edge.dx
                                    + (other_edge.x0 as f32);
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
        edges.push(make_edge(x0, y0, x1, y1));
    }
    let last = count - 1;
    if ixy[last * 2] != ixy[0] || ixy[last * 2 + 1] != ixy[1] {
        edges.push(make_edge(ixy[last * 2], ixy[last * 2 + 1], ixy[0], ixy[1]));
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
    let h_i32 = im.height as i32;
    if fill {
        if y0 < 0 {
            y0 = 0;
        } else if y0 >= h_i32 {
            return;
        }
        if y1 < 0 {
            return;
        } else if y1 > h_i32 {
            y1 = h_i32;
        }
        for y in y0..=y1 {
            hline_px(im, x0, y, x1, ink);
        }
    } else {
        if width == 0 {
            width = 1;
        }
        for i in 0..width {
            hline_px(im, x0, y0 + i, x1, ink);
            hline_px(im, x0, y1 - i, x1, ink);
            line_px(im, x1 - i, y0 + width, x1 - i, y1 - width + 1, ink);
            line_px(im, x0 + i, y0 + width, x0 + i, y1 - width + 1, ink);
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
            line_px(im, w[0][0], w[0][1], w[1][0], w[1][1], ink);
            last = Some(w[1]);
        }
        if let Some(p) = last {
            point_px(im, p[0], p[1], ink);
        }
    } else {
        for w in ixy.windows(2) {
            wide_line(im, w[0][0], w[0][1], w[1][0], w[1][1], ink, width);
        }
    }
    Ok(())
}

fn wide_line(im: &mut InkImage, x0: i32, y0: i32, x1: i32, y1: i32, ink: [u8; 3], width: i32) {
    let dx = x1 - x0;
    let dy = y1 - y0;
    if dx == 0 && dy == 0 {
        point_px(im, x0, y0, ink);
        return;
    }
    let big_hypotenuse = gxr_math::hypot(f64::from(dx), f64::from(dy));
    let small_hypotenuse = f64::from(width - 1) / 2.0_f64;
    let ratio_max = f64::from(round_up_f64(small_hypotenuse)) / big_hypotenuse;
    let ratio_min = f64::from(round_down_f64(small_hypotenuse)) / big_hypotenuse;

    let dxmin = round_down_f64(ratio_min * f64::from(dy));
    let dxmax = round_down_f64(ratio_max * f64::from(dy));
    let dymin = round_down_f64(ratio_min * f64::from(dx));
    let dymax = round_down_f64(ratio_max * f64::from(dx));

    let v = [
        [x0 - dxmin, y0 + dymax],
        [x1 - dxmin, y1 + dymax],
        [x1 + dxmax, y1 - dymin],
        [x0 + dxmax, y0 - dymin],
    ];
    let edges = [
        make_edge(v[0][0], v[0][1], v[1][0], v[1][1]),
        make_edge(v[1][0], v[1][1], v[2][0], v[2][1]),
        make_edge(v[2][0], v[2][1], v[3][0], v[3][1]),
        make_edge(v[3][0], v[3][1], v[0][0], v[0][1]),
    ];
    polygon_generic(im, &edges, ink);
}

/// Port of Pillow `ImageDraw.ellipse(xy, fill=ink)` (filled ellipse).
pub fn draw_ellipse(im: &mut InkImage, xy: [f64; 4], ink: [u8; 3]) -> Result<(), CoreError> {
    check_mode(im)?;
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
    let a = x1 - x0;
    let b = y1 - y0;
    if a < 0 || b < 0 {
        return Ok(());
    }
    let mut st = EllipseState::new(a, b, a + b);
    while let Some((lx0, ly, lx1)) = st.next_span() {
        hline_px(
            im,
            x0 + (lx0 + a) / 2,
            y0 + (ly + b) / 2,
            x0 + (lx1 + a) / 2,
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
    let a = x1 - x0;
    let b = y1 - y0;
    if a < 0 || b < 0 {
        return Ok(());
    }
    let mut st = EllipseState::new(a, b, width);
    while let Some((lx0, ly, lx1)) = st.next_span() {
        hline_px(
            im,
            x0 + (lx0 + a) / 2,
            y0 + (ly + b) / 2,
            x0 + (lx1 + a) / 2,
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
    fn new(a: i32, b: i32) -> Self {
        if a < 0 || b < 0 {
            Self {
                cx: 0,
                cy: 0,
                ex: 0,
                ey: 0,
                a2: 0,
                b2: 0,
                a2b2: 0,
                finished: true,
            }
        } else {
            let a2 = i64::from(a) * i64::from(a);
            let b2 = i64::from(b) * i64::from(b);
            Self {
                cx: a,
                cy: b % 2,
                ex: a % 2,
                ey: b,
                a2,
                b2,
                a2b2: a2 * b2,
                finished: false,
            }
        }
    }

    #[inline]
    fn delta(&self, x: i32, y: i32) -> i64 {
        let xi = i64::from(x);
        let yi = i64::from(y);
        (self.a2 * yi * yi + self.b2 * xi * xi - self.a2b2).abs()
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
    pub(crate) fn new(a: i32, b: i32, w: i32) -> Self {
        let leftmost = a % 2;
        let mut st_o = QuarterState::new(a, b);
        if w < 1 {
            Self {
                st_o,
                st_i: QuarterState::new(-1, -1),
                py: 0,
                pl: 0,
                pr: 0,
                cy: [0; 4],
                cl: [0; 4],
                cr: [0; 4],
                bufcnt: 0,
                finished: true,
                leftmost,
            }
        } else if let Some((pr, py)) = st_o.next_pt() {
            Self {
                st_o,
                st_i: QuarterState::new(a - 2 * (w - 1), b - 2 * (w - 1)),
                py,
                pl: leftmost,
                pr,
                cy: [0; 4],
                cl: [0; 4],
                cr: [0; 4],
                bufcnt: 0,
                finished: false,
                leftmost,
            }
        } else {
            Self {
                st_o,
                st_i: QuarterState::new(-1, -1),
                py: 0,
                pl: 0,
                pr: 0,
                cy: [0; 4],
                cl: [0; 4],
                cr: [0; 4],
                bufcnt: 0,
                finished: true,
                leftmost,
            }
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
