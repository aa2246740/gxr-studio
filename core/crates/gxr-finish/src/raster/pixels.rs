//! Port of `gxr_studio/finish/pixels.py` and the `Adjust` / `Curves` parsing in
//! `gxr_studio/render/params.py`.

use gxr_color::{Adjust as ColorAdjust, Curves, apply_table, channel_gains, display_table};
use gxr_raw::CoreError;

use crate::geometry::bounds::{checked_mul_u64, checked_rgb_slice, fit_count, try_vec_u8};
use crate::geometry::json::JsonValue;
use crate::geometry::spec::round4;

pub const EXPOSURE_RANGE: f64 = 5.0;
pub const SLIDER_RANGE: i64 = 100;
pub const CURVE_MAX_POINTS: usize = 16;

const INVALID_REQUEST: &str = "请求无效。请刷新页面后重试。";
const INV_2_4_F32: f32 = (1.0_f64 / 2.4_f64) as f32;

/// Full finish adjustment sliders (`render/params.py:60 Adjust`).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct FinishAdjust {
    pub exposure_ev: f64,
    pub warm: i64,
    pub tint: i64,
    pub contrast: i64,
    pub highlight: i64,
    pub shadow: i64,
    pub black_point: i64,
    pub saturation: i64,
    pub vibrance: i64,
    pub vignette: i64,
}

impl FinishAdjust {
    pub fn to_color_adjust(&self) -> ColorAdjust {
        ColorAdjust {
            exposure_ev: self.exposure_ev,
            warm: self.warm,
            tint: self.tint,
            contrast: self.contrast,
            highlight: self.highlight,
            shadow: self.shadow,
        }
    }

    pub fn tone_is_identity(&self) -> bool {
        self.contrast == 0 && self.highlight == 0 && self.shadow == 0
    }

    pub fn base_is_identity(&self, curves: Option<&Curves>) -> bool {
        self.exposure_ev == 0.0
            && self.warm == 0
            && self.tint == 0
            && self.tone_is_identity()
            && curves.is_none()
    }

    pub fn extra_is_identity(&self) -> bool {
        self.black_point == 0 && self.saturation == 0 && self.vibrance == 0 && self.vignette == 0
    }
}

fn bad_adjust() -> CoreError {
    CoreError::new("bad_adjust", INVALID_REQUEST)
}

fn bad_curve() -> CoreError {
    CoreError::new("bad_curve", INVALID_REQUEST)
}

fn int_in(value: &JsonValue, lo: i64, hi: i64) -> Result<i64, CoreError> {
    if let Some(i) = value.as_strict_int() {
        if i < lo || i > hi {
            return Err(bad_adjust());
        }
        return Ok(i);
    }
    let Some(f) = value.as_f64() else {
        return Err(bad_adjust());
    };
    if !f.is_finite() || f.fract() != 0.0 || f < (lo as f64) || f > (hi as f64) {
        return Err(bad_adjust());
    }
    Ok(f as i64)
}

/// Port of `gxr_studio/render/params.py:142 parse_adjust` on a parsed `JsonValue`.
pub fn parse_finish_adjust_value(val: &JsonValue) -> Result<FinishAdjust, CoreError> {
    if matches!(val, JsonValue::Null) {
        return Ok(FinishAdjust::default());
    }
    let JsonValue::Object(pairs) = val else {
        return Err(CoreError::new("bad_adjust", INVALID_REQUEST));
    };
    for (k, _) in pairs {
        if !matches!(
            k.as_str(),
            "exposure_ev"
                | "wb"
                | "contrast"
                | "highlight"
                | "shadow"
                | "black_point"
                | "saturation"
                | "vibrance"
                | "vignette"
        ) {
            return Err(bad_adjust());
        }
    }
    let mut out = FinishAdjust::default();
    if let Some(v) = val.get("exposure_ev") {
        let Some(ev) = v.as_f64() else {
            return Err(bad_adjust());
        };
        if !ev.is_finite() || ev.abs() > EXPOSURE_RANGE {
            return Err(bad_adjust());
        }
        out.exposure_ev = round4(ev);
    }
    if let Some(wb_val) = val.get("wb")
        && !matches!(wb_val, JsonValue::Null)
    {
        let JsonValue::Object(wb_pairs) = wb_val else {
            return Err(bad_adjust());
        };
        for (k, _) in wb_pairs {
            if !matches!(k.as_str(), "warm" | "tint") {
                return Err(bad_adjust());
            }
        }
        if let Some(v) = wb_val.get("warm") {
            out.warm = int_in(v, -SLIDER_RANGE, SLIDER_RANGE)?;
        }
        if let Some(v) = wb_val.get("tint") {
            out.tint = int_in(v, -SLIDER_RANGE, SLIDER_RANGE)?;
        }
    }
    for (name, slot) in [
        ("contrast", &mut out.contrast),
        ("highlight", &mut out.highlight),
        ("shadow", &mut out.shadow),
        ("black_point", &mut out.black_point),
        ("saturation", &mut out.saturation),
        ("vibrance", &mut out.vibrance),
        ("vignette", &mut out.vignette),
    ] {
        if let Some(v) = val.get(name) {
            *slot = int_in(v, -SLIDER_RANGE, SLIDER_RANGE)?;
        }
    }
    Ok(out)
}

fn parse_curve_points(val: &JsonValue) -> Result<Option<Vec<(f64, f64)>>, CoreError> {
    if matches!(val, JsonValue::Null) {
        return Ok(None);
    }
    let Some(items) = val.as_array() else {
        return Err(bad_curve());
    };
    if !(2..=CURVE_MAX_POINTS).contains(&items.len()) {
        return Err(bad_curve());
    }
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        let Some(pair) = item.as_array() else {
            return Err(bad_curve());
        };
        if pair.len() != 2 {
            return Err(bad_curve());
        }
        let Some(x) = pair[0].as_f64() else {
            return Err(bad_curve());
        };
        let Some(y) = pair[1].as_f64() else {
            return Err(bad_curve());
        };
        if !x.is_finite()
            || !y.is_finite()
            || !(0.0..=255.0).contains(&x)
            || !(0.0..=255.0).contains(&y)
        {
            return Err(bad_curve());
        }
        out.push((round4(x), round4(y)));
    }
    for win in out.windows(2) {
        if win[1].0 <= win[0].0 {
            return Err(bad_curve());
        }
    }
    if out.iter().all(|&(x, y)| x == y) {
        return Ok(None);
    }
    Ok(Some(out))
}

/// Port of `gxr_studio/render/params.py:170 parse_curve` on a parsed `JsonValue`.
pub fn parse_curves_value(val: &JsonValue) -> Result<Option<Curves>, CoreError> {
    if matches!(val, JsonValue::Null) {
        return Ok(None);
    }
    let JsonValue::Object(pairs) = val else {
        return Err(bad_curve());
    };
    for (k, _) in pairs {
        if !matches!(k.as_str(), "master" | "r" | "g" | "b") {
            return Err(bad_curve());
        }
    }
    let master = val
        .get("master")
        .map(parse_curve_points)
        .transpose()?
        .flatten();
    let r = val.get("r").map(parse_curve_points).transpose()?.flatten();
    let g = val.get("g").map(parse_curve_points).transpose()?.flatten();
    let b = val.get("b").map(parse_curve_points).transpose()?.flatten();
    if master.is_none() && r.is_none() && g.is_none() && b.is_none() {
        return Ok(None);
    }
    Ok(Some(Curves { master, r, g, b }))
}

#[inline]
pub(crate) fn srgb_to_linear_u8(v: u8) -> f32 {
    let x = f32::from(v) / 255.0_f32;
    if x <= 0.04045_f32 {
        x / 12.92_f32
    } else {
        gxr_math::powf(f64::from((x + 0.055_f32) / 1.055_f32), f64::from(2.4_f32)) as f32
    }
}

#[inline]
pub(crate) fn linear_to_srgb_f32(lin: f32) -> u8 {
    // np.clip(lin, 0.0, None): NaN propagates, negative values clamp to 0.0.
    let x = if lin < 0.0_f32 { 0.0_f32 } else { lin };
    let encoded = if x <= 0.0031308_f32 {
        x * 12.92_f32
    } else {
        1.055_f32 * (gxr_math::powf(f64::from(x), f64::from(INV_2_4_F32)) as f32) - 0.055_f32
    };
    (encoded * 255.0_f32 + 0.5_f32).clamp(0.0_f32, 255.0_f32) as u8
}

/// Port of `gxr_studio/finish/pixels.py:14 _srgb_to_linear`.
pub fn srgb_to_linear(rgb: &[u8], w: u64, h: u64) -> Result<Vec<f32>, CoreError> {
    let (_, _, _, rgb_len) = checked_rgb_slice(rgb, w, h)?;
    let count = fit_count(rgb_len, 4)?;
    let mut out = Vec::new();
    out.try_reserve_exact(count)
        .map_err(|_| crate::geometry::bounds::too_big())?;
    for &b in rgb {
        out.push(srgb_to_linear_u8(b));
    }
    Ok(out)
}

/// Port of `gxr_studio/finish/pixels.py:19 _linear_to_srgb`.
pub fn linear_to_srgb(lin: &[f32], w: u64, h: u64) -> Result<Vec<u8>, CoreError> {
    let (_, _, pixels, rgb_len) = crate::geometry::bounds::checked_rgb_dims(w, h)?;
    let _ = checked_mul_u64(pixels, 3)?;
    let expected = fit_count(rgb_len, 4)?;
    if lin.len() != expected {
        return Err(crate::geometry::bounds::bad_geometry());
    }
    let mut out = try_vec_u8(rgb_len, 0)?;
    for (dst, &src) in out.iter_mut().zip(lin.iter()) {
        *dst = linear_to_srgb_f32(src);
    }
    Ok(out)
}

/// Port of `gxr_studio/finish/pixels.py:25 apply_base`.
pub fn apply_base(
    rgb: &[u8],
    w: u64,
    h: u64,
    adjust: &FinishAdjust,
    curves: Option<&Curves>,
) -> Result<Vec<u8>, CoreError> {
    let (_, _, _, rgb_len) = checked_rgb_slice(rgb, w, h)?;
    let color_adjust = adjust.to_color_adjust();
    if adjust.base_is_identity(curves) {
        let mut out = try_vec_u8(rgb_len, 0)?;
        out.copy_from_slice(rgb);
        return Ok(out);
    }
    let gains = channel_gains(&color_adjust).map(|g| [g[0] as f32, g[1] as f32, g[2] as f32]);
    let mut out = try_vec_u8(rgb_len, 0)?;
    if let Some([gr, gg, gb]) = gains {
        for (dst, src) in out.chunks_exact_mut(3).zip(rgb.chunks_exact(3)) {
            dst[0] = linear_to_srgb_f32(srgb_to_linear_u8(src[0]) * gr);
            dst[1] = linear_to_srgb_f32(srgb_to_linear_u8(src[1]) * gg);
            dst[2] = linear_to_srgb_f32(srgb_to_linear_u8(src[2]) * gb);
        }
    } else {
        for (dst, src) in out.chunks_exact_mut(3).zip(rgb.chunks_exact(3)) {
            dst[0] = linear_to_srgb_f32(srgb_to_linear_u8(src[0]));
            dst[1] = linear_to_srgb_f32(srgb_to_linear_u8(src[1]));
            dst[2] = linear_to_srgb_f32(srgb_to_linear_u8(src[2]));
        }
    }
    if let Some(table) = display_table(&color_adjust, curves)? {
        for dst in out.chunks_exact_mut(3) {
            for (c, slot) in dst.iter_mut().enumerate() {
                let flat = apply_table(f32::from(*slot), &table, c);
                *slot = (flat + 0.5_f32).clamp(0.0_f32, 255.0_f32) as u8;
            }
        }
    }
    Ok(out)
}

/// Port of `gxr_studio/finish/pixels.py:41 _black`.
pub fn black(rgb: &[u8], w: u64, h: u64, amount: i64) -> Result<Vec<u8>, CoreError> {
    let (_, _, _, rgb_len) = checked_rgb_slice(rgb, w, h)?;
    let mut out = try_vec_u8(rgb_len, 0)?;
    if amount == 0 {
        out.copy_from_slice(rgb);
        return Ok(out);
    }
    let k_f64 = (amount.abs() as f64) / 100.0 * 0.35;
    let k = k_f64 as f32;
    let denom = (1.0_f64 - k_f64).max(1e-4) as f32;
    let one_minus_k = (1.0_f64 - k_f64) as f32;
    for (dst, &src) in out.iter_mut().zip(rgb.iter()) {
        let y = f32::from(src) / 255.0_f32;
        let adj = if amount > 0 {
            ((y - k) / denom).clamp(0.0_f32, 1.0_f32)
        } else {
            y * one_minus_k + k
        };
        *dst = (adj * 255.0_f32 + 0.5_f32).clamp(0.0_f32, 255.0_f32) as u8;
    }
    Ok(out)
}

/// Port of `gxr_studio/finish/pixels.py:53 _chroma`.
pub fn chroma(
    rgb: &[u8],
    w: u64,
    h: u64,
    amount: i64,
    vibrance: bool,
) -> Result<Vec<u8>, CoreError> {
    let (_, _, _, rgb_len) = checked_rgb_slice(rgb, w, h)?;
    let mut out = try_vec_u8(rgb_len, 0)?;
    if amount == 0 {
        out.copy_from_slice(rgb);
        return Ok(out);
    }
    let amt_f32 = ((amount as f64) / 100.0) as f32;
    let fixed_factor = (1.0_f64 + (amount as f64) / 100.0) as f32;
    for (dst, src) in out.chunks_exact_mut(3).zip(rgb.chunks_exact(3)) {
        let r = f32::from(src[0]);
        let g = f32::from(src[1]);
        let b = f32::from(src[2]);
        let gray = r * 0.2126_f32 + g * 0.7152_f32 + b * 0.0722_f32;
        let scale = if vibrance {
            let peak = r.max(g).max(b);
            let floor = r.min(g).min(b);
            let dull = 1.0_f32 - ((peak - floor) / 255.0_f32).clamp(0.0_f32, 1.0_f32);
            1.0_f32 + amt_f32 * dull
        } else {
            fixed_factor
        };
        let mr = gray + (r - gray) * scale;
        let mg = gray + (g - gray) * scale;
        let mb = gray + (b - gray) * scale;
        dst[0] = (mr + 0.5_f32).clamp(0.0_f32, 255.0_f32) as u8;
        dst[1] = (mg + 0.5_f32).clamp(0.0_f32, 255.0_f32) as u8;
        dst[2] = (mb + 0.5_f32).clamp(0.0_f32, 255.0_f32) as u8;
    }
    Ok(out)
}

/// Port of `gxr_studio/finish/pixels.py:70 _vignette`.
pub fn vignette(rgb: &[u8], w: u64, h: u64, amount: i64) -> Result<Vec<u8>, CoreError> {
    let (wu, hu, _, rgb_len) = checked_rgb_slice(rgb, w, h)?;
    let mut out = try_vec_u8(rgb_len, 0)?;
    if amount == 0 {
        out.copy_from_slice(rgb);
        return Ok(out);
    }
    let cx = (wu as f64 - 1.0) / 2.0;
    let cy = (hu as f64 - 1.0) / 2.0;
    let sx = cx.max(1.0);
    let sy = cy.max(1.0);
    let amt_scale = ((amount as f64) / 100.0 * 0.65) as f32;
    let row_stride = wu * 3;
    for y in 0..hu {
        let ny = (y as f64 - cy) / sy;
        let row_in = &rgb[y * row_stride..(y + 1) * row_stride];
        let row_out = &mut out[y * row_stride..(y + 1) * row_stride];
        for x in 0..wu {
            let nx = (x as f64 - cx) / sx;
            let radius = gxr_math::hypot(nx, ny).clamp(0.0, 1.0) as f32;
            let mask = radius * radius * (3.0_f32 - 2.0_f32 * radius);
            let gain = 1.0_f32 + amt_scale * mask;
            let base = x * 3;
            row_out[base] =
                (f32::from(row_in[base]) * gain + 0.5_f32).clamp(0.0_f32, 255.0_f32) as u8;
            row_out[base + 1] =
                (f32::from(row_in[base + 1]) * gain + 0.5_f32).clamp(0.0_f32, 255.0_f32) as u8;
            row_out[base + 2] =
                (f32::from(row_in[base + 2]) * gain + 0.5_f32).clamp(0.0_f32, 255.0_f32) as u8;
        }
    }
    Ok(out)
}

/// Port of `gxr_studio/finish/pixels.py:83 apply_extra`.
pub fn apply_extra(
    rgb: &[u8],
    w: u64,
    h: u64,
    adjust: &FinishAdjust,
) -> Result<Vec<u8>, CoreError> {
    let (_, _, _, rgb_len) = checked_rgb_slice(rgb, w, h)?;
    if adjust.extra_is_identity() {
        let mut out = try_vec_u8(rgb_len, 0)?;
        out.copy_from_slice(rgb);
        return Ok(out);
    }
    let step1 = black(rgb, w, h, adjust.black_point)?;
    let step2 = chroma(&step1, w, h, adjust.saturation, false)?;
    let step3 = chroma(&step2, w, h, adjust.vibrance, true)?;
    vignette(&step3, w, h, adjust.vignette)
}
