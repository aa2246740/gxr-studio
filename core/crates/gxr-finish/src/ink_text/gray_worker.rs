//! Scanline cell accumulator (`ftgrays.c` `gray_TWorker` + `gray_sweep`).

use gxr_raw::CoreError;

use super::ftcalc::FtVector;
use super::glyf::{FT_OUTLINE_EVEN_ODD_FILL, Outline};

const PIXEL_BITS: i32 = 8;
const ONE_PIXEL: i32 = 1 << PIXEL_BITS;
const ONE_PIXEL_I64: i64 = ONE_PIXEL as i64;

/// 64-bit LP64 FreeType default: `FT_RENDER_POOL_SIZE (16384) / sizeof(TCell) (24) = 682`.
const FT_MAX_GRAY_POOL: usize = 682;
const CELL_NULL: usize = FT_MAX_GRAY_POOL - 1;
pub(super) const OVERLAP_SCALE: i32 = 4;

#[derive(Clone, Copy, Default)]
struct Cell {
    x: i32,
    cover: i32,
    area: i32,
    next: usize,
}

#[derive(Clone, Copy)]
pub(super) struct ClipBox {
    pub(super) x_min: i32,
    pub(super) y_min: i32,
    pub(super) x_max: i32,
    pub(super) y_max: i32,
}

pub(super) struct GrayTarget<'a> {
    pub(super) clip: ClipBox,
    pub(super) overlap: bool,
    pub(super) width: usize,
    pub(super) height: usize,
    pub(super) dst: &'a mut [u8],
}

#[inline]
fn upscale(x: i64) -> i64 {
    x.wrapping_mul(i64::from(ONE_PIXEL >> 6))
}

#[inline]
fn trunc_coord(x: i64) -> i32 {
    (x >> PIXEL_BITS) as i32
}

#[inline]
fn fract_coord(x: i64) -> i32 {
    (x & i64::from(ONE_PIXEL - 1)) as i32
}

#[inline]
fn fill_rule(area: i32, even_odd: bool) -> u8 {
    let fill = if even_odd { 0x100_i32 } else { i32::MIN };
    let mut coverage = area >> (PIXEL_BITS * 2 + 1 - 8);
    if (coverage & fill) != 0 {
        coverage = !coverage;
    }
    if coverage > 255 && (fill & i32::MIN) != 0 {
        coverage = 255;
    }
    (coverage & 255) as u8
}

#[inline]
fn udiv(a: i64, r: i64) -> i32 {
    (((a as u64).wrapping_mul(r as u64)) >> 32) as i32
}

#[inline]
fn left_shift_i64(a: i64, b: i32) -> i64 {
    ((a as u64).wrapping_shl(b as u32)) as i64
}

pub(super) struct GrayWorker {
    min_ex: i32,
    max_ex: i32,
    min_ey: i32,
    max_ey: i32,
    count_ey: i32,
    pub(super) overflow: bool,
    cell: usize,
    cell_free: usize,
    cells: [Cell; FT_MAX_GRAY_POOL],
    ycells: [usize; FT_MAX_GRAY_POOL / 8 + 1],
    x: i64,
    y: i64,
}

impl GrayWorker {
    pub(super) fn new() -> Self {
        let mut cells = [Cell::default(); FT_MAX_GRAY_POOL];
        cells[CELL_NULL] = Cell {
            x: i32::MAX,
            cover: 0,
            area: 0,
            next: CELL_NULL,
        };
        Self {
            min_ex: 0,
            max_ex: 0,
            min_ey: 0,
            max_ey: 0,
            count_ey: 0,
            overflow: false,
            cell: CELL_NULL,
            cell_free: 0,
            cells,
            ycells: [CELL_NULL; FT_MAX_GRAY_POOL / 8 + 1],
            x: 0,
            y: 0,
        }
    }

    fn set_cell(&mut self, ex: i32, ey: i32) {
        let ey_index = ey - self.min_ey;
        if ey_index < 0 || ey_index >= self.count_ey || ex >= self.max_ex {
            self.cell = CELL_NULL;
            return;
        }
        let ex = ex.max(self.min_ex - 1);
        let row = ey_index as usize;
        let mut link = self.ycells[row];
        let mut prev: Option<usize> = None;

        loop {
            let cur = self.cells[link];
            if cur.x > ex {
                break;
            }
            if cur.x == ex {
                self.cell = link;
                return;
            }
            prev = Some(link);
            link = cur.next;
        }

        if self.cell_free >= CELL_NULL {
            self.overflow = true;
            self.cell = CELL_NULL;
            return;
        }

        let idx = self.cell_free;
        self.cell_free += 1;
        self.cells[idx] = Cell {
            x: ex,
            cover: 0,
            area: 0,
            next: link,
        };
        if let Some(p) = prev {
            self.cells[p].next = idx;
        } else {
            self.ycells[row] = idx;
        }
        self.cell = idx;
    }

    #[inline]
    fn integrate(&mut self, a: i32, b: i32) {
        let c = &mut self.cells[self.cell];
        c.cover = c.cover.wrapping_add(a);
        c.area = c.area.wrapping_add(a.wrapping_mul(b));
    }

    fn render_line(&mut self, to_x: i64, to_y: i64) {
        let mut ey1 = trunc_coord(self.y);
        let ey2 = trunc_coord(to_y);

        if (ey1 >= self.max_ey && ey2 >= self.max_ey) || (ey1 < self.min_ey && ey2 < self.min_ey) {
            self.x = to_x;
            self.y = to_y;
            return;
        }

        let mut ex1 = trunc_coord(self.x);
        let ex2 = trunc_coord(to_x);
        let mut fx1 = fract_coord(self.x);
        let mut fy1 = fract_coord(self.y);
        let dx = to_x - self.x;
        let dy = to_y - self.y;

        if ex1 == ex2 && ey1 == ey2 {
            // Single cell.
        } else if dy == 0 {
            self.set_cell(ex2, ey2);
            self.x = to_x;
            self.y = to_y;
            return;
        } else if dx == 0 {
            if dy > 0 {
                loop {
                    let fy2 = ONE_PIXEL;
                    self.integrate(fy2 - fy1, fx1 * 2);
                    fy1 = 0;
                    ey1 += 1;
                    self.set_cell(ex1, ey1);
                    if ey1 == ey2 {
                        break;
                    }
                }
            } else {
                loop {
                    let fy2 = 0;
                    self.integrate(fy2 - fy1, fx1 * 2);
                    fy1 = ONE_PIXEL;
                    ey1 -= 1;
                    self.set_cell(ex1, ey1);
                    if ey1 == ey2 {
                        break;
                    }
                }
            }
        } else {
            let mut prod = dx * i64::from(fy1) - dy * i64::from(fx1);
            let dx_r = if ex1 != ex2 { 0xFFFF_FFFF_i64 / dx } else { 0 };
            let dy_r = if ey1 != ey2 { 0xFFFF_FFFF_i64 / dy } else { 0 };

            loop {
                let fx2;
                let fy2;
                if prod - dx * ONE_PIXEL_I64 > 0 && prod <= 0 {
                    // Exit left.
                    fx2 = 0;
                    fy2 = udiv(-prod, -dx_r);
                    prod -= dy * ONE_PIXEL_I64;
                    self.integrate(fy2 - fy1, fx1 + fx2);
                    fx1 = ONE_PIXEL;
                    fy1 = fy2;
                    ex1 -= 1;
                } else if prod - dx * ONE_PIXEL_I64 + dy * ONE_PIXEL_I64 > 0
                    && prod - dx * ONE_PIXEL_I64 <= 0
                {
                    // Exit up.
                    prod -= dx * ONE_PIXEL_I64;
                    fx2 = udiv(-prod, dy_r);
                    fy2 = ONE_PIXEL;
                    self.integrate(fy2 - fy1, fx1 + fx2);
                    fx1 = fx2;
                    fy1 = 0;
                    ey1 += 1;
                } else if prod + dy * ONE_PIXEL_I64 >= 0
                    && prod - dx * ONE_PIXEL_I64 + dy * ONE_PIXEL_I64 <= 0
                {
                    // Exit right.
                    prod += dy * ONE_PIXEL_I64;
                    fx2 = ONE_PIXEL;
                    fy2 = udiv(prod, dx_r);
                    self.integrate(fy2 - fy1, fx1 + fx2);
                    fx1 = 0;
                    fy1 = fy2;
                    ex1 += 1;
                } else {
                    // Exit down.
                    fx2 = udiv(prod, -dy_r);
                    fy2 = 0;
                    prod += dx * ONE_PIXEL_I64;
                    self.integrate(fy2 - fy1, fx1 + fx2);
                    fx1 = fx2;
                    fy1 = ONE_PIXEL;
                    ey1 -= 1;
                }

                self.set_cell(ex1, ey1);
                if ex1 == ex2 && ey1 == ey2 {
                    break;
                }
            }
        }

        let fx2 = fract_coord(to_x);
        let fy2 = fract_coord(to_y);
        self.integrate(fy2 - fy1, fx1 + fx2);
        self.x = to_x;
        self.y = to_y;
    }

    fn render_conic(&mut self, control: FtVector, to: FtVector) {
        let p0 = FtVector {
            x: self.x,
            y: self.y,
        };
        let p1 = FtVector {
            x: upscale(control.x),
            y: upscale(control.y),
        };
        let p2 = FtVector {
            x: upscale(to.x),
            y: upscale(to.y),
        };

        if (trunc_coord(p0.y) >= self.max_ey
            && trunc_coord(p1.y) >= self.max_ey
            && trunc_coord(p2.y) >= self.max_ey)
            || (trunc_coord(p0.y) < self.min_ey
                && trunc_coord(p1.y) < self.min_ey
                && trunc_coord(p2.y) < self.min_ey)
        {
            self.x = p2.x;
            self.y = p2.y;
            return;
        }

        let bx = p1.x - p0.x;
        let by = p1.y - p0.y;
        let ax = p2.x - p1.x - bx;
        let ay = p2.y - p1.y - by;

        let mut dx = ax.abs().max(ay.abs());
        if dx <= i64::from(ONE_PIXEL / 4) {
            self.render_line(p2.x, p2.y);
            return;
        }

        let mut shift = 16_i32;
        loop {
            dx >>= 2;
            shift -= 1;
            if dx <= i64::from(ONE_PIXEL / 4) || shift == 0 {
                break;
            }
        }
        let mut count = 0x10000_u32 >> (shift as u32);

        let mut rx = left_shift_i64(ax, shift + shift);
        let mut ry = left_shift_i64(ay, shift + shift);
        let mut qx = left_shift_i64(bx, shift + 17).wrapping_add(rx);
        let mut qy = left_shift_i64(by, shift + 17).wrapping_add(ry);
        rx = rx.wrapping_mul(2);
        ry = ry.wrapping_mul(2);

        let mut px = left_shift_i64(p0.x, 32);
        let mut py = left_shift_i64(p0.y, 32);

        while count > 0 {
            px = px.wrapping_add(qx);
            py = py.wrapping_add(qy);
            qx = qx.wrapping_add(rx);
            qy = qy.wrapping_add(ry);
            self.render_line(px >> 32, py >> 32);
            count -= 1;
        }
    }

    fn split_cubic(arc: &mut [FtVector], idx: usize) {
        let a0 = arc[idx];
        let a1 = arc[idx + 1];
        let a2 = arc[idx + 2];
        let a3 = arc[idx + 3];

        arc[idx + 6].x = a3.x;
        let mut ax = a0.x + a1.x;
        let bx = a1.x + a2.x;
        let mut cx = a2.x + a3.x;
        arc[idx + 5].x = cx >> 1;
        cx += bx;
        arc[idx + 4].x = cx >> 2;
        arc[idx + 1].x = ax >> 1;
        ax += bx;
        arc[idx + 2].x = ax >> 2;
        arc[idx + 3].x = (ax + cx) >> 3;

        arc[idx + 6].y = a3.y;
        let mut ay = a0.y + a1.y;
        let by = a1.y + a2.y;
        let mut cy = a2.y + a3.y;
        arc[idx + 5].y = cy >> 1;
        cy += by;
        arc[idx + 4].y = cy >> 2;
        arc[idx + 1].y = ay >> 1;
        ay += by;
        arc[idx + 2].y = ay >> 2;
        arc[idx + 3].y = (ay + cy) >> 3;
    }

    fn render_cubic(&mut self, control1: FtVector, control2: FtVector, to: FtVector) {
        let mut bez_stack = [FtVector::default(); 16 * 3 + 1];
        bez_stack[0] = FtVector {
            x: upscale(to.x),
            y: upscale(to.y),
        };
        bez_stack[1] = FtVector {
            x: upscale(control2.x),
            y: upscale(control2.y),
        };
        bez_stack[2] = FtVector {
            x: upscale(control1.x),
            y: upscale(control1.y),
        };
        bez_stack[3] = FtVector {
            x: self.x,
            y: self.y,
        };

        if (trunc_coord(bez_stack[0].y) >= self.max_ey
            && trunc_coord(bez_stack[1].y) >= self.max_ey
            && trunc_coord(bez_stack[2].y) >= self.max_ey
            && trunc_coord(bez_stack[3].y) >= self.max_ey)
            || (trunc_coord(bez_stack[0].y) < self.min_ey
                && trunc_coord(bez_stack[1].y) < self.min_ey
                && trunc_coord(bez_stack[2].y) < self.min_ey
                && trunc_coord(bez_stack[3].y) < self.min_ey)
        {
            self.x = bez_stack[0].x;
            self.y = bez_stack[0].y;
            return;
        }

        let thresh = i64::from(ONE_PIXEL / 2);
        let mut arc = 0_usize;
        loop {
            let needs_split = (2 * bez_stack[arc].x - 3 * bez_stack[arc + 1].x
                + bez_stack[arc + 3].x)
                .abs()
                > thresh
                || (2 * bez_stack[arc].y - 3 * bez_stack[arc + 1].y + bez_stack[arc + 3].y).abs()
                    > thresh
                || (bez_stack[arc].x - 3 * bez_stack[arc + 2].x + 2 * bez_stack[arc + 3].x).abs()
                    > thresh
                || (bez_stack[arc].y - 3 * bez_stack[arc + 2].y + 2 * bez_stack[arc + 3].y).abs()
                    > thresh;

            if needs_split && arc + 6 < bez_stack.len() {
                Self::split_cubic(&mut bez_stack, arc);
                arc += 3;
                continue;
            }

            self.render_line(bez_stack[arc].x, bez_stack[arc].y);
            if arc == 0 {
                return;
            }
            arc -= 3;
        }
    }

    pub(super) fn move_to(&mut self, to: FtVector) -> bool {
        let x = upscale(to.x);
        let y = upscale(to.y);
        self.set_cell(trunc_coord(x), trunc_coord(y));
        self.x = x;
        self.y = y;
        self.overflow
    }

    pub(super) fn line_to(&mut self, to: FtVector) -> bool {
        self.render_line(upscale(to.x), upscale(to.y));
        self.overflow
    }

    pub(super) fn conic_to(&mut self, control: FtVector, to: FtVector) -> bool {
        self.render_conic(control, to);
        self.overflow
    }

    pub(super) fn cubic_to(
        &mut self,
        control1: FtVector,
        control2: FtVector,
        to: FtVector,
    ) -> bool {
        self.render_cubic(control1, control2, to);
        self.overflow
    }

    fn sweep(&self, even_odd: bool, overlap: bool, width: usize, height: usize, dst: &mut [u8]) {
        for y in self.min_ey..self.max_ey {
            let row_cells = self.ycells[(y - self.min_ey) as usize];
            let mut link = row_cells;
            let mut x = self.min_ex;
            let mut cover = 0_i32;

            let dst_row_idx = if overlap {
                height - 1 - ((y / OVERLAP_SCALE) as usize)
            } else {
                height - 1 - (y as usize)
            };
            let dst_row = &mut dst[dst_row_idx * width..(dst_row_idx + 1) * width];

            while link != CELL_NULL {
                let c = self.cells[link];
                if cover != 0 && c.x > x {
                    let coverage = fill_rule(cover, even_odd);
                    emit_span(dst_row, x, c.x - x, coverage, overlap);
                }
                cover = cover.wrapping_add(c.cover.wrapping_mul(ONE_PIXEL * 2));
                let area = cover.wrapping_sub(c.area);
                if area != 0 && c.x >= self.min_ex {
                    let coverage = fill_rule(area, even_odd);
                    emit_span(dst_row, c.x, 1, coverage, overlap);
                }
                x = c.x + 1;
                link = c.next;
            }

            if cover != 0 {
                let coverage = fill_rule(cover, even_odd);
                emit_span(dst_row, x, self.max_ex - x, coverage, overlap);
            }
        }
    }

    pub(super) fn convert<F>(
        &mut self,
        outline: &Outline,
        target: &mut GrayTarget<'_>,
        mut decompose: F,
    ) -> Result<(), CoreError>
    where
        F: FnMut(&mut GrayWorker, &Outline) -> Result<(), CoreError>,
    {
        let clip = target.clip;
        if clip.x_min >= clip.x_max || clip.y_min >= clip.y_max {
            return Ok(());
        }
        let even_odd = (outline.flags & FT_OUTLINE_EVEN_ODD_FILL) != 0;
        let total_height = (clip.y_max - clip.y_min) as usize;
        let mut max_band = FT_MAX_GRAY_POOL / 8;
        let band_height = if total_height > max_band {
            max_band = total_height.div_ceil(max_band);
            total_height.div_ceil(max_band)
        } else {
            total_height
        } as i32;

        let mut bands = [0_i32; 34];
        let mut y = clip.y_min;
        while y < clip.y_max {
            self.min_ey = y;
            y = y.saturating_add(band_height);
            self.max_ey = y.min(clip.y_max);
            self.count_ey = self.max_ey - self.min_ey;

            let mut b_idx = 0_usize;
            bands[1] = clip.x_min;
            bands[0] = clip.x_max;

            loop {
                self.min_ex = bands[b_idx + 1];
                self.max_ex = bands[b_idx];

                for slot in &mut self.ycells[..self.count_ey as usize] {
                    *slot = CELL_NULL;
                }
                self.cell_free = ((self.count_ey as usize) * 8).div_ceil(24);
                self.cell = CELL_NULL;
                self.overflow = false;

                decompose(self, outline)?;
                if !self.overflow {
                    self.sweep(
                        even_odd,
                        target.overlap,
                        target.width,
                        target.height,
                        target.dst,
                    );
                    if b_idx == 0 {
                        break;
                    }
                    b_idx -= 1;
                    continue;
                }

                let half = (bands[b_idx] - bands[b_idx + 1]) >> 1;
                if half == 0 || b_idx + 2 >= bands.len() {
                    return Err(CoreError::new("bad_geometry", "gray raster pool overflow"));
                }
                b_idx += 1;
                bands[b_idx + 1] = bands[b_idx];
                bands[b_idx] += half;
            }
        }
        Ok(())
    }
}

#[inline]
fn emit_span(dst_row: &mut [u8], x: i32, len: i32, coverage: u8, overlap: bool) {
    if len <= 0 {
        return;
    }
    if overlap {
        let cover = (u32::from(coverage) + 8) / 16;
        let start = x as usize;
        let end = start + (len as usize);
        for sx in start..end {
            let px = sx >> 2;
            let sum = u32::from(dst_row[px]) + cover;
            dst_row[px] = (sum - (sum >> 8)) as u8;
        }
    } else {
        let start = x as usize;
        let end = start + (len as usize);
        dst_row[start..end].fill(coverage);
    }
}
