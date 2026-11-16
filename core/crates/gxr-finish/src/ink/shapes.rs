//! Port of `src/gxr_studio/finish/frame_paint.py`: `_ss_for`, `aa_paint`, `_rr`,
//! `paint_rect`, `paint_poly`, `paint_ellipse`, `paint_hole`, and `paint_shadow`.

use gxr_raw::CoreError;

use crate::geometry::bounds::{checked_mul_u64, fit_count, too_big};
use crate::ink::blur::{gaussian_blur, gaussian_blur_radius};
use crate::ink::draw::{bad_coords, draw_polygon, draw_rectangle};
use crate::ink::draw_arc::draw_rounded_rectangle;
use crate::ink::draw_ellipse::draw_ellipse;
use crate::ink::image::{InkImage, InkMode, bad_mismatch};
use crate::ink::paste::{paste_color, paste_image};
use crate::resample::{Filter, Mode, resample_u8_u64};

#[inline]
fn finite_f64_to_i64(v: f64, msg: &str) -> Result<i64, CoreError> {
    if !v.is_finite() || v < (i64::MIN as f64) || v >= 9_223_372_036_854_775_808.0_f64 {
        return Err(bad_coords(msg));
    }
    Ok(v as i64)
}

#[inline]
fn finite_f64_to_i32(v: f64, msg: &str) -> Result<i32, CoreError> {
    if !v.is_finite() || v < f64::from(i32::MIN) || v > f64::from(i32::MAX) {
        return Err(bad_coords(msg));
    }
    Ok(v as i32)
}

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
    if canvas.mode() != InkMode::Rgb {
        return Err(bad_mismatch("aa_paint requires RGB canvas"));
    }
    if !alpha.is_finite() {
        return Err(bad_coords("alpha must be finite"));
    }
    let [x0, y0, x1, y1] = bbox;
    let bw = u64::try_from((i128::from(x1) - i128::from(x0)).max(1)).map_err(|_| too_big())?;
    let bh = u64::try_from((i128::from(y1) - i128::from(y0)).max(1)).map_err(|_| too_big())?;
    let area = checked_mul_u64(bw, bh)?;
    let _ = fit_count(area, 1)?;
    let ss = ss_for(area);
    let big_w = checked_mul_u64(bw, ss)?;
    let big_h = checked_mul_u64(bh, ss)?;
    let _ = fit_count(checked_mul_u64(big_w, big_h)?, 1)?;

    let mut big = InkImage::new_filled(InkMode::L, big_w, big_h, [0, 0, 0, 0])?;
    draw_fn(&mut big, ss as i64, x0, y0)?;

    let mask_bytes = resample_u8_u64(big.as_slice(), big_w, big_h, Mode::L, Filter::Box, bw, bh)?;
    drop(big);
    let mut mask = InkImage::from_vec(InkMode::L, bw, bh, mask_bytes)?;
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
    if canvas.mode() != InkMode::Rgb {
        return Err(bad_mismatch("paint_rect requires RGB canvas"));
    }
    if !it.radius.is_finite() || !it.stroke_w.is_finite() || !it.alpha.is_finite() {
        return Err(bad_coords("rect parameters must be finite"));
    }
    let xf = it.x as f64;
    let yf = it.y as f64;
    let wf = it.w as f64;
    let hf = it.h as f64;
    if it.radius <= 0.0_f64 && it.alpha >= 1.0_f64 {
        let sw_i = finite_f64_to_i32(it.stroke_w.trunc(), "stroke_w out of range")?;
        let x1_f = (i128::from(it.x) + i128::from(it.w) - 1) as f64;
        let y1_f = (i128::from(it.y) + i128::from(it.h) - 1) as f64;
        return draw_rectangle(canvas, [xf, yf, x1_f, y1_f], it.fill, it.outline, sw_i);
    }
    let bx0 =
        it.x.checked_sub(1)
            .ok_or_else(|| bad_coords("rect x overflow"))?;
    let by0 =
        it.y.checked_sub(1)
            .ok_or_else(|| bad_coords("rect y overflow"))?;
    let bx1 =
        it.x.checked_add(it.w)
            .and_then(|v| v.checked_add(1))
            .ok_or_else(|| bad_coords("rect x overflow"))?;
    let by1 =
        it.y.checked_add(it.h)
            .and_then(|v| v.checked_add(1))
            .ok_or_else(|| bad_coords("rect y overflow"))?;
    let bbox = [bx0, by0, bx1, by1];
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
    if canvas.mode() != InkMode::Rgb {
        return Err(bad_mismatch("paint_poly requires RGB canvas"));
    }
    if !alpha.is_finite() {
        return Err(bad_coords("alpha must be finite"));
    }
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
    let bx0 = finite_f64_to_i64(min_x.floor(), "poly coordinate out of range")?
        .checked_sub(2)
        .ok_or_else(|| bad_coords("poly bbox overflow"))?;
    let by0 = finite_f64_to_i64(min_y.floor(), "poly coordinate out of range")?
        .checked_sub(2)
        .ok_or_else(|| bad_coords("poly bbox overflow"))?;
    let bx1 = finite_f64_to_i64(max_x.ceil(), "poly coordinate out of range")?
        .checked_add(2)
        .ok_or_else(|| bad_coords("poly bbox overflow"))?;
    let by1 = finite_f64_to_i64(max_y.ceil(), "poly coordinate out of range")?
        .checked_add(2)
        .ok_or_else(|| bad_coords("poly bbox overflow"))?;
    let bbox = [bx0, by0, bx1, by1];
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
    if canvas.mode() != InkMode::Rgb {
        return Err(bad_mismatch("paint_ellipse requires RGB canvas"));
    }
    if !cx.is_finite() || !cy.is_finite() || !r.is_finite() || !alpha.is_finite() {
        return Err(bad_coords("ellipse parameters must be finite"));
    }
    let bx0 = finite_f64_to_i64((cx - r).floor(), "ellipse coordinate out of range")?
        .checked_sub(2)
        .ok_or_else(|| bad_coords("ellipse bbox overflow"))?;
    let by0 = finite_f64_to_i64((cy - r).floor(), "ellipse coordinate out of range")?
        .checked_sub(2)
        .ok_or_else(|| bad_coords("ellipse bbox overflow"))?;
    let bx1 = finite_f64_to_i64((cx + r).ceil(), "ellipse coordinate out of range")?
        .checked_add(2)
        .ok_or_else(|| bad_coords("ellipse bbox overflow"))?;
    let by1 = finite_f64_to_i64((cy + r).ceil(), "ellipse coordinate out of range")?
        .checked_add(2)
        .ok_or_else(|| bad_coords("ellipse bbox overflow"))?;
    let bbox = [bx0, by0, bx1, by1];
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
    if canvas.mode() != InkMode::Rgb {
        return Err(bad_mismatch("paint_hole requires RGB canvas"));
    }
    if !radius.is_finite() {
        return Err(bad_coords("hole radius must be finite"));
    }
    let bx0 = x
        .checked_sub(1)
        .ok_or_else(|| bad_coords("hole bbox overflow"))?;
    let by0 = y
        .checked_sub(1)
        .ok_or_else(|| bad_coords("hole bbox overflow"))?;
    let bx1 = x
        .checked_add(w)
        .and_then(|v| v.checked_add(1))
        .ok_or_else(|| bad_coords("hole bbox overflow"))?;
    let by1 = y
        .checked_add(h)
        .and_then(|v| v.checked_add(1))
        .ok_or_else(|| bad_coords("hole bbox overflow"))?;
    let bbox = [bx0, by0, bx1, by1];
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
    if canvas.mode() != InkMode::Rgb {
        return Err(bad_mismatch("paint_shadow requires RGB canvas"));
    }
    if !it.blur.is_finite() || !it.radius.is_finite() || !it.alpha.is_finite() {
        return Err(bad_coords("shadow parameters must be finite"));
    }
    if it.blur < 0.0_f64 {
        return Err(CoreError::new("value_error", "radius must be >= 0"));
    }
    let blur_r = gaussian_blur_radius(it.blur as f32, 3);
    if !blur_r.is_finite() || blur_r >= 2_147_483_648.0_f32 {
        return Err(CoreError::new("value_error", "radius out of range"));
    }
    let pad = finite_f64_to_i64((it.blur * 3.0_f64).trunc(), "shadow blur out of range")?
        .checked_add(2)
        .ok_or_else(|| CoreError::new("value_error", "shadow blur out of range"))?;
    let lut = InkImage::point_table_mul(it.alpha)?;

    let cw = canvas.width() as i128;
    let ch = canvas.height() as i128;
    let sx = i128::from(it.x) + i128::from(it.dx);
    let sy = i128::from(it.y) + i128::from(it.dy);
    let pad_128 = i128::from(pad);
    let x0_128 = (sx - pad_128).max(0);
    let y0_128 = (sy - pad_128).max(0);
    let x1_128 = (sx + i128::from(it.w) + pad_128).min(cw);
    let y1_128 = (sy + i128::from(it.h) + pad_128).min(ch);
    if x1_128 <= x0_128 || y1_128 <= y0_128 {
        return Ok(());
    }
    let x0 = i64::try_from(x0_128).map_err(|_| bad_coords("shadow bbox overflow"))?;
    let y0 = i64::try_from(y0_128).map_err(|_| bad_coords("shadow bbox overflow"))?;
    let x1 = i64::try_from(x1_128).map_err(|_| bad_coords("shadow bbox overflow"))?;
    let y1 = i64::try_from(y1_128).map_err(|_| bad_coords("shadow bbox overflow"))?;
    let mw = (x1 - x0) as u64;
    let mh = (y1 - y0) as u64;
    let mut m = InkImage::new_filled(InkMode::L, mw, mh, [0, 0, 0, 0])?;
    let box4 = [
        (sx - x0_128) as f64,
        (sy - y0_128) as f64,
        (sx + i128::from(it.w) - x0_128 - 1) as f64,
        (sy + i128::from(it.h) - y0_128 - 1) as f64,
    ];
    let ink = [255, 255, 255];
    if it.radius > 0.0_f64 {
        draw_rounded_rectangle(&mut m, box4, it.radius, Some(ink), None, 1, [true; 4])?;
    } else {
        draw_rectangle(&mut m, box4, Some(ink), None, 1)?;
    }
    let blurred = gaussian_blur(&m, it.blur, it.blur)?;
    let mask = blurred.point(&lut)?;
    paste_color(
        canvas,
        [it.color[0], it.color[1], it.color[2], 255],
        [x0, y0, x1, y1],
        Some(&mask),
    )
}
