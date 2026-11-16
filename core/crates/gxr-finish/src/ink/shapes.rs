//! Port of `src/gxr_studio/finish/frame_paint.py`: `_ss_for`, `aa_paint`, `_rr`,
//! `paint_rect`, `paint_poly`, `paint_ellipse`, `paint_hole`, and `paint_shadow`.

use gxr_raw::CoreError;

use crate::geometry::bounds::{checked_mul_u64, fit_count};
use crate::ink::blur::gaussian_blur;
use crate::ink::draw::{bad_coords, draw_ellipse, draw_polygon, draw_rectangle};
use crate::ink::draw_arc::draw_rounded_rectangle;
use crate::ink::image::{InkImage, InkMode, bad_mismatch};
use crate::ink::paste::{paste_color, paste_image};
use crate::resample::{Filter, Mode, resample_u8_u64};

/// Supersampling factor matching `frame_paint.py:_ss_for`.
#[must_use]
pub(crate) fn ss_for(area: u64) -> u64 {
    if area < 300_000 {
        4
    } else if area < 1_500_000 {
        3
    } else {
        2
    }
}

/// Port of `frame_paint.py:aa_paint`.
pub fn aa_paint<F>(
    canvas: &mut InkImage,
    bbox: [i64; 4],
    draw_fn: F,
    color: [u8; 3],
    alpha: f64,
    src: Option<&InkImage>,
) -> Result<(), CoreError>
where
    F: FnOnce(&mut InkImage, i64, i64, i64) -> Result<(), CoreError>,
{
    if canvas.mode != InkMode::Rgb {
        return Err(bad_mismatch("aa_paint requires RGB canvas"));
    }
    if !alpha.is_finite() {
        return Err(bad_coords("alpha must be finite"));
    }
    let [x0, y0, x1, y1] = bbox;
    let bw = (x1.saturating_sub(x0)).max(1) as u64;
    let bh = (y1.saturating_sub(y0)).max(1) as u64;
    let area = checked_mul_u64(bw, bh)?;
    let _ = fit_count(area, 1)?;
    let ss = ss_for(area);
    let big_w = checked_mul_u64(bw, ss)?;
    let big_h = checked_mul_u64(bh, ss)?;
    let _ = fit_count(checked_mul_u64(big_w, big_h)?, 1)?;

    let mut big = InkImage::new_filled(InkMode::L, big_w, big_h, [0, 0, 0, 0])?;
    draw_fn(&mut big, ss as i64, x0, y0)?;

    let mask_bytes = resample_u8_u64(&big.data, big_w, big_h, Mode::L, Filter::Box, bw, bh)?;
    drop(big);
    let mut mask = InkImage::from_bytes(InkMode::L, bw, bh, &mask_bytes)?;
    if alpha < 1.0_f64 {
        let lut = InkImage::point_table_mul(alpha)?;
        mask = mask.point(&lut)?;
    }

    let bw_i = i64::try_from(bw).map_err(|_| bad_coords("bbox width out of range"))?;
    let bh_i = i64::try_from(bh).map_err(|_| bad_coords("bbox height out of range"))?;
    let px1 = x0
        .checked_add(bw_i)
        .ok_or_else(|| bad_coords("bbox x overflow"))?;
    let py1 = y0
        .checked_add(bh_i)
        .ok_or_else(|| bad_coords("bbox y overflow"))?;
    let dst_box = [x0, y0, px1, py1];

    if let Some(base_src) = src {
        let cropped = base_src.crop(x0, y0, px1, py1)?;
        paste_image(canvas, &cropped, dst_box, Some(&mask))?;
    } else {
        paste_color(
            canvas,
            [color[0], color[1], color[2], 255],
            dst_box,
            Some(&mask),
        )?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn draw_rr(
    d: &mut InkImage,
    ss: f64,
    ox: f64,
    oy: f64,
    x: f64,
    y: f64,
    w: f64,
    h: f64,
    r: f64,
    value: u8,
    inset: f64,
) -> Result<(), CoreError> {
    let box4 = [
        (x + inset - ox) * ss,
        (y + inset - oy) * ss,
        (x + w - inset - ox) * ss - 1.0_f64,
        (y + h - inset - oy) * ss - 1.0_f64,
    ];
    let rr = (r - inset).max(0.0_f64) * ss;
    let ink = [value, value, value];
    if rr > 0.0_f64 {
        draw_rounded_rectangle(d, box4, rr, Some(ink), None, 1, [true, true, true, true])
    } else {
        draw_rectangle(d, box4, Some(ink), None, 1)
    }
}

/// Parameters for [`paint_rect`].
#[derive(Clone, Copy, Debug)]
pub struct RectItem {
    pub x: i64,
    pub y: i64,
    pub w: i64,
    pub h: i64,
    pub radius: f64,
    pub fill: Option<[u8; 3]>,
    pub outline: Option<[u8; 3]>,
    pub stroke_w: f64,
    pub alpha: f64,
}

/// Port of `frame_paint.py:paint_rect`.
pub fn paint_rect(canvas: &mut InkImage, it: &RectItem) -> Result<(), CoreError> {
    if canvas.mode != InkMode::Rgb {
        return Err(bad_mismatch("paint_rect requires RGB canvas"));
    }
    let xf = it.x as f64;
    let yf = it.y as f64;
    let wf = it.w as f64;
    let hf = it.h as f64;
    if it.radius <= 0.0_f64 && it.alpha >= 1.0_f64 {
        let sw_i = if !it.stroke_w.is_finite() {
            return Err(bad_coords("stroke_w must be finite"));
        } else {
            it.stroke_w.trunc() as i32
        };
        return draw_rectangle(
            canvas,
            [xf, yf, (it.x + it.w - 1) as f64, (it.y + it.h - 1) as f64],
            it.fill,
            it.outline,
            sw_i,
        );
    }
    let bbox = [it.x - 1, it.y - 1, it.x + it.w + 1, it.y + it.h + 1];
    if let Some(fill) = it.fill {
        aa_paint(
            canvas,
            bbox,
            |d, ss, ox, oy| {
                draw_rr(
                    d, ss as f64, ox as f64, oy as f64, xf, yf, wf, hf, it.radius, 255, 0.0,
                )
            },
            fill,
            it.alpha,
            None,
        )?;
    }
    if let Some(outline) = it.outline {
        aa_paint(
            canvas,
            bbox,
            |d, ss, ox, oy| {
                let ssf = ss as f64;
                let oxf = ox as f64;
                let oyf = oy as f64;
                draw_rr(d, ssf, oxf, oyf, xf, yf, wf, hf, it.radius, 255, 0.0)?;
                draw_rr(d, ssf, oxf, oyf, xf, yf, wf, hf, it.radius, 0, it.stroke_w)
            },
            outline,
            it.alpha,
            None,
        )?;
    }
    Ok(())
}

/// Port of `frame_paint.py:paint_poly`.
pub fn paint_poly(
    canvas: &mut InkImage,
    pts: &[[f64; 2]],
    fill: [u8; 3],
    alpha: f64,
) -> Result<(), CoreError> {
    if pts.is_empty() {
        return Err(bad_coords("poly pts cannot be empty"));
    }
    let mut min_x = f64::INFINITY;
    let mut max_x = f64::NEG_INFINITY;
    let mut min_y = f64::INFINITY;
    let mut max_y = f64::NEG_INFINITY;
    for p in pts {
        if !p[0].is_finite() || !p[1].is_finite() {
            return Err(bad_coords("poly coordinate must be finite"));
        }
        min_x = min_x.min(p[0]);
        max_x = max_x.max(p[0]);
        min_y = min_y.min(p[1]);
        max_y = max_y.max(p[1]);
    }
    let bbox = [
        (min_x.floor() as i64) - 2,
        (min_y.floor() as i64) - 2,
        (max_x.ceil() as i64) + 2,
        (max_y.ceil() as i64) + 2,
    ];
    aa_paint(
        canvas,
        bbox,
        |d, ss, ox, oy| {
            let ssf = ss as f64;
            let oxf = ox as f64;
            let oyf = oy as f64;
            let scaled: Vec<[f64; 2]> = pts
                .iter()
                .map(|p| [(p[0] - oxf) * ssf, (p[1] - oyf) * ssf])
                .collect();
            draw_polygon(d, &scaled, [255, 255, 255])
        },
        fill,
        alpha,
        None,
    )
}

/// Port of `frame_paint.py:paint_ellipse`.
pub fn paint_ellipse(
    canvas: &mut InkImage,
    cx: f64,
    cy: f64,
    r: f64,
    fill: [u8; 3],
    alpha: f64,
) -> Result<(), CoreError> {
    if !cx.is_finite() || !cy.is_finite() || !r.is_finite() {
        return Err(bad_coords("ellipse parameters must be finite"));
    }
    let bbox = [
        ((cx - r).floor() as i64) - 2,
        ((cy - r).floor() as i64) - 2,
        ((cx + r).ceil() as i64) + 2,
        ((cy + r).ceil() as i64) + 2,
    ];
    aa_paint(
        canvas,
        bbox,
        |d, ss, ox, oy| {
            let ssf = ss as f64;
            let oxf = ox as f64;
            let oyf = oy as f64;
            let box4 = [
                (cx - r - oxf) * ssf,
                (cy - r - oyf) * ssf,
                (cx + r - oxf) * ssf,
                (cy + r - oyf) * ssf,
            ];
            draw_ellipse(d, box4, [255, 255, 255])
        },
        fill,
        alpha,
        None,
    )
}

/// Port of `frame_paint.py:paint_hole`.
pub fn paint_hole(
    canvas: &mut InkImage,
    x: i64,
    y: i64,
    w: i64,
    h: i64,
    radius: f64,
    base: &InkImage,
) -> Result<(), CoreError> {
    let bbox = [x - 1, y - 1, x + w + 1, y + h + 1];
    aa_paint(
        canvas,
        bbox,
        |d, ss, ox, oy| {
            draw_rr(
                d, ss as f64, ox as f64, oy as f64, x as f64, y as f64, w as f64, h as f64, radius,
                255, 0.0,
            )
        },
        [0, 0, 0],
        1.0,
        Some(base),
    )
}

/// Parameters for [`paint_shadow`].
#[derive(Clone, Copy, Debug)]
pub struct ShadowItem {
    pub x: i64,
    pub y: i64,
    pub w: i64,
    pub h: i64,
    pub blur: f64,
    pub dx: i64,
    pub dy: i64,
    pub radius: f64,
    pub alpha: f64,
    pub color: [u8; 3],
}

/// Port of `frame_paint.py:paint_shadow`.
pub fn paint_shadow(canvas: &mut InkImage, it: &ShadowItem) -> Result<(), CoreError> {
    if canvas.mode != InkMode::Rgb {
        return Err(bad_mismatch("paint_shadow requires RGB canvas"));
    }
    if !it.blur.is_finite() || !it.radius.is_finite() || !it.alpha.is_finite() {
        return Err(bad_coords("shadow parameters must be finite"));
    }
    if it.blur < 0.0_f64 {
        return Err(CoreError::new("value_error", "radius must be >= 0"));
    }
    let pad = (it.blur * 3.0_f64).trunc() as i64 + 2;
    let x0 = (it.x + it.dx - pad).max(0);
    let y0 = (it.y + it.dy - pad).max(0);
    let x1 = (it.x + it.dx + it.w + pad).min(canvas.width as i64);
    let y1 = (it.y + it.dy + it.h + pad).min(canvas.height as i64);
    if x1 <= x0 || y1 <= y0 {
        return Ok(());
    }
    let mw = (x1 - x0) as u64;
    let mh = (y1 - y0) as u64;
    let mut m = InkImage::new_filled(InkMode::L, mw, mh, [0, 0, 0, 0])?;
    let box4 = [
        (it.x + it.dx - x0) as f64,
        (it.y + it.dy - y0) as f64,
        (it.x + it.dx + it.w - x0 - 1) as f64,
        (it.y + it.dy + it.h - y0 - 1) as f64,
    ];
    let ink = [255, 255, 255];
    if it.radius > 0.0_f64 {
        draw_rounded_rectangle(&mut m, box4, it.radius, Some(ink), None, 1, [true; 4])?;
    } else {
        draw_rectangle(&mut m, box4, Some(ink), None, 1)?;
    }
    let blurred = gaussian_blur(&m, it.blur, it.blur)?;
    let lut = InkImage::point_table_mul(it.alpha)?;
    let mask = blurred.point(&lut)?;
    paste_color(
        canvas,
        [it.color[0], it.color[1], it.color[2], 255],
        [x0, y0, x1, y1],
        Some(&mask),
    )
}
