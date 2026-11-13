//! Port of `gxr_studio/finish/geometry.py` (valid rect, fitting, clamping, crop_box).

use gxr_raw::CoreError;

use crate::crop::{MIN_SIDE_PX, RATIO_TOLERANCE, is_known_preset, ratio_off, target_ratio};

use super::spec::Geometry;
use super::warp::{needs_valid_crop, warp_valid};

#[inline]
fn round_even(v: f64) -> i64 {
    v.round_ties_even() as i64
}

/// Port of `gxr_studio/finish/geometry.py:199 max_area_rect`
pub fn max_area_rect(valid: &[u8], width: usize, height: usize) -> [usize; 4] {
    if height == 0 || width == 0 || !valid.iter().any(|&v| v != 0) {
        return [0, 0, 0, 0];
    }
    let mut heights = vec![0_i32; width];
    let mut best = [0_usize, 0, 0, 0];
    let mut best_area = 0_u64;
    let mut stack: Vec<isize> = Vec::with_capacity(width + 1);
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
                let rect_w = (j as isize - left - 1) as usize;
                let area = (bar as u64) * (rect_w as u64);
                if area > best_area {
                    best_area = area;
                    best = [(left + 1) as usize, i + 1 - bar, rect_w, bar];
                }
            }
            stack.push(j as isize);
        }
    }
    best
}

fn linspace_indices(len: usize, count: usize) -> Vec<usize> {
    if count <= 1 || len <= 1 {
        return vec![0; count];
    }
    let stop = (len - 1) as f64;
    let step = stop / ((count - 1) as f64);
    let mut out = Vec::with_capacity(count);
    for i in 0..count - 1 {
        out.push(((i as f64) * step) as i32 as usize);
    }
    out.push(len - 1);
    out
}

/// Port of `gxr_studio/finish/geometry.py:224 normalized_valid_rect`
pub fn normalized_valid_rect(valid: &[u8], width: usize, height: usize) -> [f64; 4] {
    if height == 0 || width == 0 {
        return [0.0, 0.0, 1.0, 1.0];
    }
    if valid.iter().all(|&v| v != 0) {
        return [0.0, 0.0, 1.0, 1.0];
    }
    let long_edge = height.max(width);
    if long_edge > 240 {
        let scale = 240.0 / long_edge as f64;
        let sh = 2.max(round_even(height as f64 * scale) as usize);
        let sw = 2.max(round_even(width as f64 * scale) as usize);
        let ys = linspace_indices(height, sh);
        let xs = linspace_indices(width, sw);
        let mut sub = vec![0_u8; sh * sw];
        for (sy_i, &y) in ys.iter().enumerate() {
            let row = &valid[y * width..(y + 1) * width];
            let dst = &mut sub[sy_i * sw..(sy_i + 1) * sw];
            for (sx_i, &x) in xs.iter().enumerate() {
                dst[sx_i] = row[x];
            }
        }
        let [x, y, rw, rh] = max_area_rect(&sub, sw, sh);
        if rw == 0 || rh == 0 {
            return [0.0, 0.0, 1.0, 1.0];
        }
        let wf = width as f64;
        let hf = height as f64;
        let swf = sw as f64;
        let shf = sh as f64;
        let x0 = ((x as f64 / swf) * wf) as i64 + 1;
        let y0 = ((y as f64 / shf) * hf) as i64 + 1;
        let x1 = (x0 + 1).max((((x + rw) as f64 / swf) * wf) as i64 - 1);
        let y1 = (y0 + 1).max((((y + rh) as f64 / shf) * hf) as i64 - 1);
        return [
            x0 as f64 / wf,
            y0 as f64 / hf,
            (x1 - x0) as f64 / wf,
            (y1 - y0) as f64 / hf,
        ];
    }
    let [x, y, rw, rh] = max_area_rect(valid, width, height);
    if rw == 0 || rh == 0 {
        return [0.0, 0.0, 1.0, 1.0];
    }
    let wf = width as f64;
    let hf = height as f64;
    [x as f64 / wf, y as f64 / hf, rw as f64 / wf, rh as f64 / hf]
}

/// Port of `gxr_studio/finish/geometry.py:268 header_value`
pub fn header_value(rect: [f64; 4]) -> String {
    format!(
        "{:.4},{:.4},{:.4},{:.4}",
        rect[0], rect[1], rect[2], rect[3]
    )
}

/// Port of `gxr_studio/finish/geometry.py:272 _valid_integral`
pub(crate) fn valid_integral(valid: &[u8], width: usize, height: usize) -> Vec<i32> {
    let stride = width + 1;
    let mut sheet = vec![0_i32; (height + 1) * stride];
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
    sheet
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
fn has_window(sheet: &[i32], width: usize, height: usize, tw: usize, th: usize) -> bool {
    if tw < 1 || th < 1 || tw > width || th > height {
        return false;
    }
    let stride = width + 1;
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
                return true;
            }
        }
    }
    false
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
) -> Option<[usize; 4]> {
    if tw < 1 || th < 1 || tw > width || th > height {
        return None;
    }
    let stride = width + 1;
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
    best
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
) -> Option<[usize; 4]> {
    let tw = k.checked_mul(numer)?;
    let th = k.checked_mul(denom)?;
    if tw < 1 || th < 1 || tw > width || th > height {
        return None;
    }
    let tx = round_even(cx - tw as f64 / 2.0)
        .max(0)
        .min((width - tw) as i64) as usize;
    let ty = round_even(cy - th as f64 / 2.0)
        .max(0)
        .min((height - th) as i64) as usize;
    Some([tx, ty, tw, th])
}

/// Port of `gxr_studio/finish/geometry.py:327 _ratio_in_box`
fn ratio_in_box(
    box_rect: [usize; 4],
    numer: usize,
    denom: usize,
    width: usize,
    height: usize,
) -> [usize; 4] {
    let [x, y, box_w, box_h] = box_rect;
    let bw = 1.max(box_w.min(width - x));
    let bh = 1.max(box_h.min(height - y));
    if numer == denom {
        let side = bw.min(bh);
        return [x, y, side, side];
    }
    let mut tw = bw;
    let mut th = 1_i64.max(round_even(tw as f64 * denom as f64 / numer as f64)) as usize;
    if th > bh {
        th = bh;
        tw = 1_i64.max(round_even(th as f64 * numer as f64 / denom as f64)) as usize;
    }
    [x, y, tw.min(bw), th.min(bh)]
}

/// Port of `gxr_studio/finish/geometry.py:343 fit_ratio_in_valid`
pub fn fit_ratio_in_valid(
    valid: &[u8],
    width: usize,
    height: usize,
    rect: [f64; 4],
    numer: usize,
    denom: usize,
) -> [usize; 4] {
    let [x, y, rw, rh] = rect;
    let cx = (x + rw / 2.0) * width as f64;
    let cy = (y + rh / 2.0) * height as f64;
    let limit_w = width.min(1_i64.max(round_even(rw * width as f64)) as usize);
    let limit_h = height.min(1_i64.max(round_even(rh * height as f64)) as usize);
    let max_k = if numer > 0 && denom > 0 {
        (limit_w / numer).min(limit_h / denom)
    } else {
        0
    };
    let sheet = valid_integral(valid, width, height);
    let stride = width + 1;
    let mut centred: Option<[usize; 4]> = None;
    let mut centred_k = 0_usize;
    for k in (1..=max_k).rev() {
        if let Some(b) = place_ratio(k, numer, denom, cx, cy, width, height)
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
        return c_box;
    }
    let mut lo = centred_k;
    let mut hi = max_k;
    let mut found_k = 0_usize;
    while lo < hi {
        let mid = (lo + hi).div_ceil(2);
        if has_window(&sheet, width, height, mid * numer, mid * denom) {
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
        )
    {
        return near;
    }
    if let Some(c_box) = centred {
        return c_box;
    }
    let [fx, fy, fw_raw, fh_raw] = max_area_rect(valid, width, height);
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
    let tx = round_even(cx - tw as f64 / 2.0)
        .max(fx as i64)
        .min((fx + fw - tw) as i64) as usize;
    let ty = round_even(cy - th as f64 / 2.0)
        .max(fy as i64)
        .min((fy + fh - th) as i64) as usize;
    [tx, ty, tw, th]
}

/// Port of `gxr_studio/finish/geometry.py:397 fit_preset_in_valid`
pub fn fit_preset_in_valid(
    valid: &[u8],
    width: usize,
    height: usize,
    preset: Option<&str>,
    rect: [f64; 4],
) -> Option<[usize; 4]> {
    let p = preset?;
    if !is_known_preset(p) || p == "free" || p == "original" {
        return None;
    }
    let mut ratio = target_ratio(p, width as i64, height as i64, false)
        .ok()
        .flatten()?;
    let [_, _, rw, rh] = rect;
    if (rw * width as f64 >= rh * height as f64) != (ratio.numer >= ratio.denom) {
        ratio = ratio.inv();
    }
    Some(fit_ratio_in_valid(
        valid,
        width,
        height,
        rect,
        ratio.numer as usize,
        ratio.denom as usize,
    ))
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
) -> bool {
    if rw < 1 || rh < 1 || x < 0 || y < 0 || x + rw > width as i64 || y + rh > height as i64 {
        return false;
    }
    let ux = x as usize;
    let uy = y as usize;
    let urw = rw as usize;
    let urh = rh as usize;
    for row in uy..uy + urh {
        if valid[row * width + ux..row * width + ux + urw].contains(&0) {
            return false;
        }
    }
    true
}

/// Port of `gxr_studio/finish/geometry.py:428 _snap_to_aspect`
fn snap_to_aspect(th: i64, aspect: f64, tolerance: f64, floor: i64) -> (i64, i64) {
    let fits = |t: i64| -> Option<(i64, i64)> {
        if t < 1 {
            return None;
        }
        let w = round_even(t as f64 * aspect);
        if w >= 1 && ((w as f64 / t as f64) - aspect).abs() / aspect <= tolerance {
            Some((w, t))
        } else {
            None
        }
    };
    let start_down = floor.max(th - 40);
    for t in (start_down..=th).rev() {
        if let Some(hit) = fits(t) {
            return hit;
        }
    }
    for t in (th + 1)..(th + 40) {
        if let Some(hit) = fits(t) {
            return hit;
        }
    }
    (1.max(round_even(th as f64 * aspect)), th)
}

/// Port of `gxr_studio/finish/geometry.py:447 clamp_norm_rect`
pub fn clamp_norm_rect(
    valid: &[u8],
    width: usize,
    height: usize,
    mut rect: [f64; 4],
    aspect: Option<f64>,
) -> [f64; 4] {
    let wf = width as f64;
    let hf = height as f64;
    let [x, y, rw, rh] = rect;
    let mut px = round_even(x * wf);
    let mut py = round_even(y * hf);
    let mut pw = 1.max(round_even(rw * wf));
    let mut ph = 1.max(round_even(rh * hf));
    let min_h = match aspect {
        Some(a) => 2.max((16.0 * 1.0_f64.max(1.0 / a)).ceil() as i64),
        None => 2,
    };
    if let Some(a) = aspect
        && ((pw as f64 / ph as f64 - a).abs() / a) > RATIO_TOLERANCE
    {
        let cx0 = px as f64 + pw as f64 / 2.0;
        let cy0 = py as f64 + ph as f64 / 2.0;
        let bw = (pw as f64).min(ph as f64 * a);
        ph = min_h.max(round_even(bw / a));
        let snapped = snap_to_aspect(ph, a, 0.009, min_h);
        pw = snapped.0;
        ph = snapped.1;
        px = round_even(cx0 - pw as f64 / 2.0);
        py = round_even(cy0 - ph as f64 / 2.0);
        rect = [
            px as f64 / wf,
            py as f64 / hf,
            pw as f64 / wf,
            ph as f64 / hf,
        ];
    }
    if window_full(valid, width, height, px, py, pw, ph) {
        return rect;
    }
    let cx = px as f64 + pw as f64 / 2.0;
    let cy = py as f64 + ph as f64 / 2.0;
    let eff_aspect = aspect.unwrap_or(pw as f64 / ph as f64);
    let mut lo = 0.05_f64;
    let mut hi = 1.0_f64;
    let mut best: Option<[usize; 4]> = None;
    for _ in 0..18 {
        let mid = (lo + hi) / 2.0;
        let mut th = min_h.max(round_even(ph as f64 * mid));
        let mut tw = 2.max(round_even(th as f64 * eff_aspect));
        if min_h > 2 {
            (tw, th) = snap_to_aspect(th, eff_aspect, 0.009, min_h);
        }
        let tx = round_even(cx - tw as f64 / 2.0)
            .max(0)
            .min(0.max(width as i64 - tw));
        let ty = round_even(cy - th as f64 / 2.0)
            .max(0)
            .min(0.max(height as i64 - th));
        if window_full(valid, width, height, tx, ty, tw, th) {
            best = Some([tx as usize, ty as usize, tw as usize, th as usize]);
            lo = mid;
        } else {
            hi = mid;
        }
    }
    let best_scale = if best.is_some() { lo } else { 0.0 };
    if best.is_none() || best_scale < 0.999 {
        let sheet = valid_integral(valid, width, height);
        let mut lo2 = if best.is_some() { best_scale } else { 0.05 };
        let mut hi2 = 1.0_f64;
        let mut found_size: Option<(usize, usize)> = None;
        for _ in 0..14 {
            let mid = (lo2 + hi2) / 2.0;
            let mut th = min_h.max(round_even(ph as f64 * mid));
            let mut tw = 2.max(round_even(th as f64 * eff_aspect));
            if min_h > 2 {
                (tw, th) = snap_to_aspect(th, eff_aspect, 0.009, min_h);
            }
            if tw >= 1 && th >= 1 && has_window(&sheet, width, height, tw as usize, th as usize) {
                found_size = Some((tw as usize, th as usize));
                lo2 = mid;
            } else {
                hi2 = mid;
            }
        }
        if let Some((fw, fh)) = found_size {
            best = nearest_window(&sheet, width, height, fw, fh, cx, cy);
        }
    }
    match best {
        None => {
            let [fx, fy, fw, fh] = max_area_rect(valid, width, height);
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
    }
}

/// Port of `gxr_studio/finish/geometry.py:551 _fit_in_valid`
fn fit_in_valid(
    valid: &[u8],
    vw: usize,
    vh: usize,
    preset: Option<&str>,
    target: [f64; 4],
    rect: Option<[f64; 4]>,
) -> [f64; 4] {
    let full = [0.0, 0.0, 1.0, 1.0];
    if let Some(p) = preset
        && let Some([x, y, bw, bh]) = fit_preset_in_valid(valid, vw, vh, Some(p), target)
    {
        return [
            x as f64 / vw as f64,
            y as f64 / vh as f64,
            bw as f64 / vw as f64,
            bh as f64 / vh as f64,
        ];
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
    let w_px = 1.max(round_even(rect[2] * vw as f64));
    let h_px = 1.max(round_even(rect[3] * vh as f64));
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
    let result = fit_in_valid(&valid, vw, vh, preset, target, rect);
    if let Some(r) = rect
        && r != full
        && !usable(vw, vh, preset, result)
    {
        let again = fit_in_valid(&valid, vw, vh, preset, full, None);
        if usable(vw, vh, preset, again) {
            return Ok(Some(again));
        }
    }
    Ok(Some(result))
}

/// Port of `gxr_studio/finish/geometry.py:587 crop_box`
pub fn crop_box(width: usize, height: usize, rect: [f64; 4]) -> [usize; 4] {
    let wf = width as f64;
    let hf = height as f64;
    let x = round_even(rect[0] * wf).max(0).min((width as i64) - 1) as usize;
    let y = round_even(rect[1] * hf).max(0).min((height as i64) - 1) as usize;
    let rw = 1.max(round_even(rect[2] * wf)) as usize;
    let rh = 1.max(round_even(rect[3] * hf)) as usize;
    [x, y, rw.min(width - x), rh.min(height - y)]
}

/// Port of `gxr_studio/finish/geometry.py:596 crop_valid`
pub fn crop_valid(
    rgb: &[u8],
    width: usize,
    height: usize,
    rect: [f64; 4],
) -> (usize, usize, Vec<u8>) {
    let [x, y, rw, rh] = crop_box(width, height, rect);
    let mut out = Vec::with_capacity(rw * rh * 3);
    for row in y..y + rh {
        let off = (row * width + x) * 3;
        out.extend_from_slice(&rgb[off..off + rw * 3]);
    }
    (rw, rh, out)
}
