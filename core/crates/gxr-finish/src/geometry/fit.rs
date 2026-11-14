//! Port of `gxr_studio/finish/geometry.py` (valid rect, fitting, clamping, crop_box).

use gxr_raw::CoreError;

use crate::crop::{MIN_SIDE_PX, RATIO_TOLERANCE, is_known_preset, ratio_off, target_ratio};

use super::bounds::{
    bad_geometry, bad_rect, checked_add_u64, checked_integral_slice, checked_mask_dims,
    checked_mask_slice, checked_mul_u64, checked_rgb_slice, fit_count, rect_to_pixels, round_even,
    too_big, try_vec_i32, try_vec_u8, try_vec_usize_with_capacity,
};
use super::spec::Geometry;
use super::warp::{needs_valid_crop, warp_valid};

/// Port of `gxr_studio/finish/geometry.py:199 max_area_rect`
pub fn max_area_rect(valid: &[u8], width: usize, height: usize) -> Result<[usize; 4], CoreError> {
    let _ = checked_mask_slice(valid, width as u64, height as u64)?;
    let hist_len = fit_count(width as u64, std::mem::size_of::<i32>() as u64)?;
    let stack_elems = checked_add_u64(width as u64, 1)?;
    let stack_cap = fit_count(stack_elems, std::mem::size_of::<i32>() as u64)?;
    if !valid.iter().any(|&v| v != 0) {
        return Ok([0, 0, 0, 0]);
    }
    let mut heights = vec![0_i32; hist_len];
    let mut best = [0_usize, 0, 0, 0];
    let mut best_area = 0_u64;
    let mut stack: Vec<i32> = Vec::with_capacity(stack_cap);
    for i in 0..height {
        let row = &valid[i * width..(i + 1) * width];
        for j in 0..width {
            heights[j] = if row[j] != 0 { heights[j] + 1 } else { 0 };
        }
        stack.clear();
        stack.push(-1);
        for j in 0..=width {
            let cur = if j < width { heights[j] } else { 0 };
            while let Some(&top) = stack.last() {
                if top == -1 || heights[top as usize] <= cur {
                    break;
                }
                stack.pop();
                let bar = heights[top as usize] as usize;
                let left = *stack.last().unwrap_or(&-1);
                let rect_w = (j as i64 - i64::from(left) - 1) as usize;
                let area = (bar as u64) * (rect_w as u64);
                if area > best_area {
                    best_area = area;
                    best = [(left + 1) as usize, i + 1 - bar, rect_w, bar];
                }
            }
            stack.push(j as i32);
        }
    }
    Ok(best)
}

fn linspace_indices(len: usize, count: usize) -> Result<Vec<usize>, CoreError> {
    let mut out = try_vec_usize_with_capacity(count as u64)?;
    if count <= 1 || len <= 1 {
        out.resize(count, 0);
        return Ok(out);
    }
    let stop = (len - 1) as f64;
    let step = stop / ((count - 1) as f64);
    for i in 0..count - 1 {
        out.push(((i as f64) * step) as i32 as usize);
    }
    out.push(len - 1);
    Ok(out)
}

/// Port of `gxr_studio/finish/geometry.py:224 normalized_valid_rect`
pub fn normalized_valid_rect(
    valid: &[u8],
    width: usize,
    height: usize,
) -> Result<[f64; 4], CoreError> {
    let _ = checked_mask_slice(valid, width as u64, height as u64)?;
    if valid.iter().all(|&v| v != 0) {
        return Ok([0.0, 0.0, 1.0, 1.0]);
    }
    let long_edge = height.max(width);
    if long_edge > 240 {
        let scale = 240.0 / long_edge as f64;
        let sh = 2_usize.max(
            round_even(height as f64 * scale)
                .ok()
                .and_then(|v| usize::try_from(v).ok())
                .unwrap_or(2),
        );
        let sw = 2_usize.max(
            round_even(width as f64 * scale)
                .ok()
                .and_then(|v| usize::try_from(v).ok())
                .unwrap_or(2),
        );
        let ys = linspace_indices(height, sh)?;
        let xs = linspace_indices(width, sw)?;
        let sub_pixels = checked_mul_u64(sh as u64, sw as u64)?;
        let mut sub = try_vec_u8(sub_pixels, 0)?;
        for (sy_i, &y) in ys.iter().enumerate() {
            let row = &valid[y * width..(y + 1) * width];
            let dst = &mut sub[sy_i * sw..(sy_i + 1) * sw];
            for (sx_i, &x) in xs.iter().enumerate() {
                dst[sx_i] = row[x];
            }
        }
        let [x, y, rw, rh] = max_area_rect(&sub, sw, sh)?;
        if rw == 0 || rh == 0 {
            return Ok([0.0, 0.0, 1.0, 1.0]);
        }
        let wf = width as f64;
        let hf = height as f64;
        let swf = sw as f64;
        let shf = sh as f64;
        let x0 = ((x as f64 / swf) * wf) as i64 + 1;
        let y0 = ((y as f64 / shf) * hf) as i64 + 1;
        let x1 = (x0 + 1).max((((x + rw) as f64 / swf) * wf) as i64 - 1);
        let y1 = (y0 + 1).max((((y + rh) as f64 / shf) * hf) as i64 - 1);
        return Ok([
            x0 as f64 / wf,
            y0 as f64 / hf,
            (x1 - x0) as f64 / wf,
            (y1 - y0) as f64 / hf,
        ]);
    }
    let [x, y, rw, rh] = max_area_rect(valid, width, height)?;
    if rw == 0 || rh == 0 {
        return Ok([0.0, 0.0, 1.0, 1.0]);
    }
    let wf = width as f64;
    let hf = height as f64;
    Ok([x as f64 / wf, y as f64 / hf, rw as f64 / wf, rh as f64 / hf])
}

/// Port of `gxr_studio/finish/geometry.py:268 header_value`
pub fn header_value(rect: [f64; 4]) -> String {
    format!(
        "{:.4},{:.4},{:.4},{:.4}",
        rect[0], rect[1], rect[2], rect[3]
    )
}

/// Port of `gxr_studio/finish/geometry.py:272 _valid_integral`
pub(crate) fn valid_integral(
    valid: &[u8],
    width: usize,
    height: usize,
) -> Result<Vec<i32>, CoreError> {
    let _ = checked_mask_slice(valid, width as u64, height as u64)?;
    let w1 = checked_add_u64(width as u64, 1)?;
    let h1 = checked_add_u64(height as u64, 1)?;
    let stride = fit_count(w1, 4)?;
    let sheet_elems = checked_mul_u64(h1, w1)?;
    let mut sheet = try_vec_i32(sheet_elems, 0)?;
    for y in 0..height {
        let src_row = &valid[y * width..(y + 1) * width];
        let mut row_sum = 0_i32;
        let prev_off = y * stride;
        let cur_off = (y + 1) * stride;
        for x in 0..width {
            row_sum = row_sum.wrapping_add(i32::from(src_row[x] != 0));
            sheet[cur_off + x + 1] = sheet[prev_off + x + 1].wrapping_add(row_sum);
        }
    }
    Ok(sheet)
}

/// Port of `gxr_studio/finish/geometry.py:279 _covered`
#[inline]
fn covered(sheet: &[i32], stride: usize, x: usize, y: usize, rw: usize, rh: usize) -> bool {
    if rw < 1 || rh < 1 {
        return false;
    }
    let got = sheet[(y + rh) * stride + (x + rw)]
        .wrapping_sub(sheet[y * stride + (x + rw)])
        .wrapping_sub(sheet[(y + rh) * stride + x])
        .wrapping_add(sheet[y * stride + x]);
    got == (rw as i32).wrapping_mul(rh as i32)
}

/// Port of `gxr_studio/finish/geometry.py:295 _has_window`
fn has_window(
    sheet: &[i32],
    width: usize,
    height: usize,
    tw: usize,
    th: usize,
) -> Result<bool, CoreError> {
    let stride = checked_integral_slice(sheet, width, height)?;
    if tw < 1 || th < 1 || tw > width || th > height {
        return Ok(false);
    }
    let target = (tw as i32).wrapping_mul(th as i32);
    let max_y = height - th;
    let max_x = width - tw;
    for y in 0..=max_y {
        let r0 = y * stride;
        let r1 = (y + th) * stride;
        for x in 0..=max_x {
            let got = sheet[r1 + x + tw]
                .wrapping_sub(sheet[r0 + x + tw])
                .wrapping_sub(sheet[r1 + x])
                .wrapping_add(sheet[r0 + x]);
            if got == target {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

/// Port of `gxr_studio/finish/geometry.py:300 _nearest_window`
pub(crate) fn nearest_window(
    sheet: &[i32],
    width: usize,
    height: usize,
    tw: usize,
    th: usize,
    cx: f64,
    cy: f64,
) -> Result<Option<[usize; 4]>, CoreError> {
    let stride = checked_integral_slice(sheet, width, height)?;
    if tw < 1 || th < 1 || tw > width || th > height {
        return Ok(None);
    }
    let target = (tw as i32).wrapping_mul(th as i32);
    let max_y = height - th;
    let max_x = width - tw;
    let half_tw = tw as f64 / 2.0;
    let half_th = th as f64 / 2.0;
    let mut best_dist = f64::INFINITY;
    let mut best: Option<[usize; 4]> = None;

    for y in 0..=max_y {
        let r0 = y * stride;
        let r1 = (y + th) * stride;
        let dy = (y as f64 + half_th) - cy;
        let dy2 = dy * dy;
        for x in 0..=max_x {
            let got = sheet[r1 + x + tw]
                .wrapping_sub(sheet[r0 + x + tw])
                .wrapping_sub(sheet[r1 + x])
                .wrapping_add(sheet[r0 + x]);
            if got == target {
                let dx = (x as f64 + half_tw) - cx;
                let dist = dx * dx + dy2;
                if dist < best_dist {
                    best_dist = dist;
                    best = Some([x, y, tw, th]);
                }
            }
        }
    }
    Ok(best)
}

/// Port of `gxr_studio/finish/geometry.py:317 _place_ratio`
fn place_ratio(
    k: usize,
    numer: usize,
    denom: usize,
    cx: f64,
    cy: f64,
    width: usize,
    height: usize,
) -> Result<Option<[usize; 4]>, CoreError> {
    let Some(tw) = k.checked_mul(numer) else {
        return Ok(None);
    };
    let Some(th) = k.checked_mul(denom) else {
        return Ok(None);
    };
    if tw < 1 || th < 1 || tw > width || th > height {
        return Ok(None);
    }
    let max_x = i64::try_from(width - tw).map_err(|_| bad_rect())?;
    let max_y = i64::try_from(height - th).map_err(|_| bad_rect())?;
    let tx_i = round_even(cx - tw as f64 / 2.0)?.max(0).min(max_x);
    let ty_i = round_even(cy - th as f64 / 2.0)?.max(0).min(max_y);
    let tx = usize::try_from(tx_i).map_err(|_| bad_rect())?;
    let ty = usize::try_from(ty_i).map_err(|_| bad_rect())?;
    Ok(Some([tx, ty, tw, th]))
}

/// Port of `gxr_studio/finish/geometry.py:327 _ratio_in_box`
fn ratio_in_box(
    box_rect: [usize; 4],
    numer: usize,
    denom: usize,
    width: usize,
    height: usize,
) -> Result<[usize; 4], CoreError> {
    let [x, y, box_w, box_h] = box_rect;
    let bw = 1.max(box_w.min(width - x));
    let bh = 1.max(box_h.min(height - y));
    if numer == denom {
        let side = bw.min(bh);
        return Ok([x, y, side, side]);
    }
    let bw_i64 = i64::try_from(bw).map_err(|_| bad_rect())?;
    let bh_i64 = i64::try_from(bh).map_err(|_| bad_rect())?;
    let mut tw_i64 = bw_i64;
    let mut th_i64 = 1_i64.max(round_even(tw_i64 as f64 * denom as f64 / numer as f64)?);
    if th_i64 > bh_i64 {
        th_i64 = bh_i64;
        tw_i64 = 1_i64.max(round_even(th_i64 as f64 * numer as f64 / denom as f64)?);
    }
    let tw = usize::try_from(tw_i64.min(bw_i64).max(1)).map_err(|_| bad_rect())?;
    let th = usize::try_from(th_i64.min(bh_i64).max(1)).map_err(|_| bad_rect())?;
    Ok([x, y, tw, th])
}

/// Port of `gxr_studio/finish/geometry.py:343 fit_ratio_in_valid`
pub fn fit_ratio_in_valid(
    valid: &[u8],
    width: usize,
    height: usize,
    rect: [f64; 4],
    numer: usize,
    denom: usize,
) -> Result<[usize; 4], CoreError> {
    let _ = checked_mask_slice(valid, width as u64, height as u64)?;
    let (_px, _py, limit_w_i64, limit_h_i64) = rect_to_pixels(rect, width, height)?;
    let [x, y, rw, rh] = rect;
    let cx = (x + rw / 2.0) * width as f64;
    let cy = (y + rh / 2.0) * height as f64;
    let w_i64 = i64::try_from(width).map_err(|_| bad_rect())?;
    let h_i64 = i64::try_from(height).map_err(|_| bad_rect())?;
    let limit_w = usize::try_from(limit_w_i64.min(w_i64).max(1)).map_err(|_| bad_rect())?;
    let limit_h = usize::try_from(limit_h_i64.min(h_i64).max(1)).map_err(|_| bad_rect())?;
    let max_k = if numer > 0 && denom > 0 {
        (limit_w / numer).min(limit_h / denom)
    } else {
        0
    };
    let sheet = valid_integral(valid, width, height)?;
    let stride = width + 1;
    let mut centred: Option<[usize; 4]> = None;
    let mut centred_k = 0_usize;
    for k in (1..=max_k).rev() {
        if let Some(b) = place_ratio(k, numer, denom, cx, cy, width, height)?
            && covered(&sheet, stride, b[0], b[1], b[2], b[3])
        {
            centred = Some(b);
            centred_k = k;
            break;
        }
    }
    if let Some(c_box) = centred
        && centred_k == max_k
    {
        return Ok(c_box);
    }
    let mut lo = centred_k;
    let mut hi = max_k;
    let mut found_k = 0_usize;
    while lo < hi {
        let mid = (lo + hi).div_ceil(2);
        if has_window(&sheet, width, height, mid * numer, mid * denom)? {
            found_k = mid;
            lo = mid;
        } else {
            hi = mid - 1;
        }
    }
    if found_k > 0
        && let Some(near) = nearest_window(
            &sheet,
            width,
            height,
            found_k * numer,
            found_k * denom,
            cx,
            cy,
        )?
    {
        return Ok(near);
    }
    if let Some(c_box) = centred {
        return Ok(c_box);
    }
    let [fx, fy, fw_raw, fh_raw] = max_area_rect(valid, width, height)?;
    let fw = fw_raw.min(width - fx);
    let fh = fh_raw.min(height - fy);
    let k = if numer > 0 && denom > 0 && fw > 0 && fh > 0 {
        (fw / numer).min(fh / denom)
    } else {
        0
    };
    if k < 1 {
        return ratio_in_box([fx, fy, fw.max(1), fh.max(1)], numer, denom, width, height);
    }
    let tw = k * numer;
    let th = k * denom;
    let fx_i = i64::try_from(fx).map_err(|_| bad_rect())?;
    let fy_i = i64::try_from(fy).map_err(|_| bad_rect())?;
    let max_x = i64::try_from(fx + fw - tw).map_err(|_| bad_rect())?;
    let max_y = i64::try_from(fy + fh - th).map_err(|_| bad_rect())?;
    let tx = usize::try_from(round_even(cx - tw as f64 / 2.0)?.max(fx_i).min(max_x))
        .map_err(|_| bad_rect())?;
    let ty = usize::try_from(round_even(cy - th as f64 / 2.0)?.max(fy_i).min(max_y))
        .map_err(|_| bad_rect())?;
    Ok([tx, ty, tw, th])
}

/// Port of `gxr_studio/finish/geometry.py:397 fit_preset_in_valid`
pub fn fit_preset_in_valid(
    valid: &[u8],
    width: usize,
    height: usize,
    preset: Option<&str>,
    rect: [f64; 4],
) -> Result<Option<[usize; 4]>, CoreError> {
    let _ = checked_mask_slice(valid, width as u64, height as u64)?;
    let Some(p) = preset else {
        return Ok(None);
    };
    if !is_known_preset(p) || p == "free" || p == "original" {
        return Ok(None);
    }
    let Some(mut ratio) = target_ratio(p, width as i64, height as i64, false)
        .ok()
        .flatten()
    else {
        return Ok(None);
    };
    let [_, _, rw, rh] = rect;
    if (rw * width as f64 >= rh * height as f64) != (ratio.numer >= ratio.denom) {
        ratio = ratio.inv();
    }
    Ok(Some(fit_ratio_in_valid(
        valid,
        width,
        height,
        rect,
        ratio.numer as usize,
        ratio.denom as usize,
    )?))
}

/// Port of `gxr_studio/finish/geometry.py:419 window_full`
pub fn window_full(
    valid: &[u8],
    width: usize,
    height: usize,
    x: i64,
    y: i64,
    rw: i64,
    rh: i64,
) -> Result<bool, CoreError> {
    let (wu, hu, _) = checked_mask_slice(valid, width as u64, height as u64)?;
    if rw < 1 || rh < 1 || x < 0 || y < 0 {
        return Ok(false);
    }
    let w_i64 = i64::try_from(wu).map_err(|_| bad_geometry())?;
    let h_i64 = i64::try_from(hu).map_err(|_| bad_geometry())?;
    let Some(x_end) = x.checked_add(rw) else {
        return Ok(false);
    };
    let Some(y_end) = y.checked_add(rh) else {
        return Ok(false);
    };
    if x_end > w_i64 || y_end > h_i64 {
        return Ok(false);
    }
    let (Ok(ux), Ok(uy), Ok(urw), Ok(urh)) = (
        usize::try_from(x),
        usize::try_from(y),
        usize::try_from(rw),
        usize::try_from(rh),
    ) else {
        return Ok(false);
    };
    for row in uy..uy + urh {
        let start = checked_add_u64(checked_mul_u64(row as u64, wu as u64)?, ux as u64)?;
        let end = checked_add_u64(start, urw as u64)?;
        let s_idx = fit_count(start, 1)?;
        let e_idx = fit_count(end, 1)?;
        if valid
            .get(s_idx..e_idx)
            .ok_or_else(bad_geometry)?
            .contains(&0)
        {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Port of `gxr_studio/finish/geometry.py:428 _snap_to_aspect`
fn snap_to_aspect(
    th: i64,
    aspect: f64,
    tolerance: f64,
    floor: i64,
) -> Result<(i64, i64), CoreError> {
    let fits = |t: i64| -> Result<Option<(i64, i64)>, CoreError> {
        if t < 1 {
            return Ok(None);
        }
        let w = round_even(t as f64 * aspect)?;
        if w >= 1 && ((w as f64 / t as f64) - aspect).abs() / aspect <= tolerance {
            Ok(Some((w, t)))
        } else {
            Ok(None)
        }
    };
    let start_down = floor.max(th.saturating_sub(40));
    for t in (start_down..=th).rev() {
        if let Some(hit) = fits(t)? {
            return Ok(hit);
        }
    }
    let end_up = th.saturating_add(40);
    for t in th.saturating_add(1)..end_up {
        if let Some(hit) = fits(t)? {
            return Ok(hit);
        }
    }
    Ok((1_i64.max(round_even(th as f64 * aspect)?), th))
}

/// Port of `gxr_studio/finish/geometry.py:447 clamp_norm_rect`
pub fn clamp_norm_rect(
    valid: &[u8],
    width: usize,
    height: usize,
    mut rect: [f64; 4],
    aspect: Option<f64>,
) -> Result<[f64; 4], CoreError> {
    let _ = checked_mask_slice(valid, width as u64, height as u64)?;
    let wf = width as f64;
    let hf = height as f64;
    let (mut px, mut py, mut pw, mut ph) = rect_to_pixels(rect, width, height)?;
    let w_i64 = i64::try_from(width).map_err(|_| bad_rect())?;
    let h_i64 = i64::try_from(height).map_err(|_| bad_rect())?;
    let min_h = match aspect {
        Some(a) => {
            if !a.is_finite() || a <= 0.0 {
                return Err(bad_rect());
            }
            let min_f = (16.0 * 1.0_f64.max(1.0 / a)).ceil();
            2.max(round_even(min_f)?)
        }
        None => 2,
    };
    if let Some(a) = aspect
        && ((pw as f64 / ph as f64 - a).abs() / a) > RATIO_TOLERANCE
    {
        let cx0 = px as f64 + pw as f64 / 2.0;
        let cy0 = py as f64 + ph as f64 / 2.0;
        let bw = (pw as f64).min(ph as f64 * a);
        ph = min_h.max(round_even(bw / a)?);
        let snapped = snap_to_aspect(ph, a, 0.009, min_h)?;
        pw = snapped.0;
        ph = snapped.1;
        px = round_even(cx0 - pw as f64 / 2.0)?;
        py = round_even(cy0 - ph as f64 / 2.0)?;
        let _ = px.checked_add(pw).ok_or_else(bad_rect)?;
        let _ = py.checked_add(ph).ok_or_else(bad_rect)?;
        rect = [
            px as f64 / wf,
            py as f64 / hf,
            pw as f64 / wf,
            ph as f64 / hf,
        ];
    }
    if window_full(valid, width, height, px, py, pw, ph)? {
        return Ok(rect);
    }
    let cx = px as f64 + pw as f64 / 2.0;
    let cy = py as f64 + ph as f64 / 2.0;
    let eff_aspect = aspect.unwrap_or(pw as f64 / ph as f64);
    let mut lo = 0.05_f64;
    let mut hi = 1.0_f64;
    let mut best: Option<[usize; 4]> = None;
    for _ in 0..18 {
        let mid = (lo + hi) / 2.0;
        let mut th = min_h.max(round_even(ph as f64 * mid)?);
        let mut tw = 2.max(round_even(th as f64 * eff_aspect)?);
        if min_h > 2 {
            (tw, th) = snap_to_aspect(th, eff_aspect, 0.009, min_h)?;
        }
        let max_tx = w_i64.checked_sub(tw).unwrap_or(0).max(0);
        let max_ty = h_i64.checked_sub(th).unwrap_or(0).max(0);
        let tx = round_even(cx - tw as f64 / 2.0)?.max(0).min(max_tx);
        let ty = round_even(cy - th as f64 / 2.0)?.max(0).min(max_ty);
        if window_full(valid, width, height, tx, ty, tw, th)? {
            best = Some([
                usize::try_from(tx).map_err(|_| bad_rect())?,
                usize::try_from(ty).map_err(|_| bad_rect())?,
                usize::try_from(tw).map_err(|_| bad_rect())?,
                usize::try_from(th).map_err(|_| bad_rect())?,
            ]);
            lo = mid;
        } else {
            hi = mid;
        }
    }
    let best_scale = if best.is_some() { lo } else { 0.0 };
    if best.is_none() || best_scale < 0.999 {
        let sheet = valid_integral(valid, width, height)?;
        let mut lo2 = if best.is_some() { best_scale } else { 0.05 };
        let mut hi2 = 1.0_f64;
        let mut found_size: Option<(usize, usize)> = None;
        for _ in 0..14 {
            let mid = (lo2 + hi2) / 2.0;
            let mut th = min_h.max(round_even(ph as f64 * mid)?);
            let mut tw = 2.max(round_even(th as f64 * eff_aspect)?);
            if min_h > 2 {
                (tw, th) = snap_to_aspect(th, eff_aspect, 0.009, min_h)?;
            }
            if tw <= w_i64
                && th <= h_i64
                && let (Ok(utw), Ok(uth)) = (usize::try_from(tw), usize::try_from(th))
                && utw >= 1
                && uth >= 1
                && has_window(&sheet, width, height, utw, uth)?
            {
                found_size = Some((utw, uth));
                lo2 = mid;
            } else {
                hi2 = mid;
            }
        }
        if let Some((fw, fh)) = found_size {
            best = nearest_window(&sheet, width, height, fw, fh, cx, cy)?;
        }
    }
    Ok(match best {
        None => {
            let [fx, fy, fw, fh] = max_area_rect(valid, width, height)?;
            if fw < 1 || fh < 1 {
                [0.0, 0.0, 1.0, 1.0]
            } else {
                [
                    fx as f64 / wf,
                    fy as f64 / hf,
                    fw as f64 / wf,
                    fh as f64 / hf,
                ]
            }
        }
        Some([tx, ty, tw, th]) => [
            tx as f64 / wf,
            ty as f64 / hf,
            tw as f64 / wf,
            th as f64 / hf,
        ],
    })
}

/// Port of `gxr_studio/finish/geometry.py:551 _fit_in_valid`
fn fit_in_valid(
    valid: &[u8],
    vw: usize,
    vh: usize,
    preset: Option<&str>,
    target: [f64; 4],
    rect: Option<[f64; 4]>,
) -> Result<[f64; 4], CoreError> {
    let full = [0.0, 0.0, 1.0, 1.0];
    if let Some(p) = preset
        && let Some([x, y, bw, bh]) = fit_preset_in_valid(valid, vw, vh, Some(p), target)?
    {
        return Ok([
            x as f64 / vw as f64,
            y as f64 / vh as f64,
            bw as f64 / vw as f64,
            bh as f64 / vh as f64,
        ]);
    }
    if preset == Some("original") {
        let mut aspect = vw as f64 / vh as f64;
        if (target[2] * vw as f64 >= target[3] * vh as f64) != (aspect >= 1.0) {
            aspect = 1.0 / aspect;
        }
        return clamp_norm_rect(valid, vw, vh, target, Some(aspect));
    }
    match rect {
        None => normalized_valid_rect(valid, vw, vh),
        Some(r) if r == full => normalized_valid_rect(valid, vw, vh),
        Some(r) => clamp_norm_rect(valid, vw, vh, r, None),
    }
}

/// Port of `gxr_studio/finish/geometry.py:577 _usable`
fn usable(vw: usize, vh: usize, preset: Option<&str>, rect: [f64; 4]) -> bool {
    let Ok(w_raw) = round_even(rect[2] * vw as f64) else {
        return false;
    };
    let Ok(h_raw) = round_even(rect[3] * vh as f64) else {
        return false;
    };
    let w_px = 1_i64.max(w_raw);
    let h_px = 1_i64.max(h_raw);
    if w_px.min(h_px) < MIN_SIDE_PX {
        return false;
    }
    let ratio = match preset {
        Some(p) if is_known_preset(p) => {
            target_ratio(p, vw as i64, vh as i64, false).ok().flatten()
        }
        _ => None,
    };
    match ratio {
        None => true,
        Some(r) => ratio_off(r, w_px, h_px) <= RATIO_TOLERANCE,
    }
}

/// Port of `gxr_studio/finish/geometry.py:519 fitted_crop`
pub fn fitted_crop(
    geom: &Geometry,
    rect: Option<[f64; 4]>,
    preset: Option<&str>,
    width: Option<usize>,
    height: Option<usize>,
    force: bool,
) -> Result<Option<[f64; 4]>, CoreError> {
    if !force && !needs_valid_crop(geom) {
        return Ok(None);
    }
    let (vw, vh, valid) = match (width, height) {
        (Some(w), Some(h)) if w > 0 && h > 0 => warp_valid(w, h, geom)?,
        _ => warp_valid(120, 90, geom)?,
    };
    let full = [0.0, 0.0, 1.0, 1.0];
    let target = rect.unwrap_or(full);
    let result = fit_in_valid(&valid, vw, vh, preset, target, rect)?;
    if let Some(r) = rect
        && r != full
        && !usable(vw, vh, preset, result)
    {
        let again = fit_in_valid(&valid, vw, vh, preset, full, None)?;
        if usable(vw, vh, preset, again) {
            return Ok(Some(again));
        }
    }
    Ok(Some(result))
}

/// Port of `gxr_studio/finish/geometry.py:587 crop_box`
pub fn crop_box(width: usize, height: usize, rect: [f64; 4]) -> Result<[usize; 4], CoreError> {
    if width == 0 || height == 0 {
        return Err(bad_rect());
    }
    let (wu, hu, _) = checked_mask_dims(width as u64, height as u64)?;
    let (px, py, pw, ph) = rect_to_pixels(rect, wu, hu)?;
    let w_i64 = i64::try_from(wu).map_err(|_| bad_rect())?;
    let h_i64 = i64::try_from(hu).map_err(|_| bad_rect())?;
    let x_i64 = px.max(0).min(w_i64 - 1);
    let y_i64 = py.max(0).min(h_i64 - 1);
    let rw_i64 = pw.min(w_i64 - x_i64);
    let rh_i64 = ph.min(h_i64 - y_i64);
    let x = usize::try_from(x_i64).map_err(|_| bad_rect())?;
    let y = usize::try_from(y_i64).map_err(|_| bad_rect())?;
    let rw = usize::try_from(rw_i64).map_err(|_| bad_rect())?;
    let rh = usize::try_from(rh_i64).map_err(|_| bad_rect())?;
    Ok([x, y, rw, rh])
}

/// Port of `gxr_studio/finish/geometry.py:596 crop_valid`
pub fn crop_valid(
    rgb: &[u8],
    width: usize,
    height: usize,
    rect: [f64; 4],
) -> Result<(usize, usize, Vec<u8>), CoreError> {
    let (wu, hu, _, _) = checked_rgb_slice(rgb, width as u64, height as u64)?;
    let [x, y, rw, rh] = crop_box(wu, hu, rect)?;
    let bytes = checked_mul_u64(checked_mul_u64(rw as u64, rh as u64)?, 3)?;
    let cap = fit_count(bytes, 1)?;
    let mut out = Vec::new();
    out.try_reserve_exact(cap).map_err(|_| too_big())?;
    let row_bytes = fit_count(checked_mul_u64(rw as u64, 3)?, 1)?;
    for row in y..y + rh {
        let px_off = checked_add_u64(checked_mul_u64(row as u64, wu as u64)?, x as u64)?;
        let off = fit_count(checked_mul_u64(px_off, 3)?, 1)?;
        let end = off.checked_add(row_bytes).ok_or_else(bad_geometry)?;
        let slice = rgb.get(off..end).ok_or_else(bad_geometry)?;
        out.extend_from_slice(slice);
    }
    Ok((rw, rh, out))
}

#[cfg(test)]
#[path = "fit_tests.rs"]
mod tests;
