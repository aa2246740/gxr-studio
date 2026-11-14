//! Port of `gxr_studio/crop.py`.

use gxr_raw::{CoreError, Json};

use crate::geometry::json::{JsonValue, parse_json};

pub const MAX_ANGLE: f64 = 45.0;
pub const MIN_SIDE_PX: i64 = 16;
pub const RATIO_TOLERANCE: f64 = 0.01;
pub const EDGE_TOLERANCE_PX: f64 = 1.0;

/// Reduced positive rational `numer / denom` mirroring Python's `fractions.Fraction`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Ratio {
    pub numer: i64,
    pub denom: i64,
}

impl Ratio {
    pub fn new(numer: i64, denom: i64) -> Self {
        let g = gcd(numer.abs(), denom.abs()).max(1);
        let sign = if denom < 0 { -1 } else { 1 };
        Self {
            numer: (numer / g) * sign,
            denom: (denom / g) * sign,
        }
    }

    pub fn inv(self) -> Self {
        Self::new(self.denom, self.numer)
    }

    pub fn as_f64(self) -> f64 {
        self.numer as f64 / self.denom as f64
    }
}

fn gcd(mut a: i64, mut b: i64) -> i64 {
    while b != 0 {
        let r = a % b;
        a = b;
        b = r;
    }
    a
}

/// Port of `gxr_studio/crop.py:23 PRESETS`
pub fn preset_pair(preset: &str) -> Result<Option<(i64, i64)>, CoreError> {
    match preset {
        "original" | "free" => Ok(None),
        "3:2" => Ok(Some((3, 2))),
        "4:3" => Ok(Some((4, 3))),
        "1:1" => Ok(Some((1, 1))),
        "4:5" => Ok(Some((5, 4))),
        "16:9" => Ok(Some((16, 9))),
        "16:10" => Ok(Some((16, 10))),
        "1.91:1" => Ok(Some((191, 100))),
        "2.35:1" => Ok(Some((235, 100))),
        "xpan-65-24" => Ok(Some((65, 24))),
        _ => Err(CoreError::new(
            "unknown_preset",
            "无法裁剪。请重新选择比例。",
        )),
    }
}

pub fn is_known_preset(preset: &str) -> bool {
    preset_pair(preset).is_ok()
}

/// Port of `gxr_studio/crop.py:45 Crop`
#[derive(Clone, Debug, PartialEq)]
pub struct Crop {
    pub preset: String,
    pub rect: [f64; 4],
    pub angle_deg: f64,
}

impl Crop {
    /// Port of `gxr_studio/crop.py:51 Crop.to_json`
    pub fn to_json(&self) -> Json {
        Json::object([
            ("preset", Json::str(&self.preset)),
            ("rect", Json::floats(&self.rect)),
            ("angle_deg", Json::Float(self.angle_deg)),
        ])
    }
}

/// Port of `gxr_studio/crop.py:56 round_half_up`
pub fn round_half_up(v: f64) -> i64 {
    (v + 0.5).floor() as i64
}

/// Port of `gxr_studio/crop.py:60 target_ratio`
pub fn target_ratio(
    preset: &str,
    width: i64,
    height: i64,
    flip: bool,
) -> Result<Option<Ratio>, CoreError> {
    let pair = preset_pair(preset)?;
    if preset == "free" {
        return Ok(None);
    }
    if preset == "original" {
        if width <= 0 || height <= 0 {
            return Err(CoreError::new("bad_rect", "裁剪框超出画面范围"));
        }
        return Ok(Some(Ratio::new(width, height)));
    }
    let (long_side, short_side) =
        pair.ok_or_else(|| CoreError::new("unknown_preset", "无法裁剪。请重新选择比例。"))?;
    let landscape = (width >= height) != flip;
    if landscape {
        Ok(Some(Ratio::new(long_side, short_side)))
    } else {
        Ok(Some(Ratio::new(short_side, long_side)))
    }
}

/// Port of `gxr_studio/crop.py:73 crop_box`
pub fn crop_box(rect: [f64; 4], width: i64, height: i64) -> [i64; 4] {
    let [x, y, w, h] = rect;
    let wf = width as f64;
    let hf = height as f64;
    [
        round_half_up(x * wf),
        round_half_up(y * hf),
        round_half_up(w * wf),
        round_half_up(h * hf),
    ]
}

/// Port of `gxr_studio/crop.py:81 box_inside`
pub fn box_inside(box_px: [f64; 4], width: i64, height: i64, angle_deg: f64, tol: f64) -> bool {
    let [x0, y0, w, h] = box_px;
    let th = angle_deg.to_radians();
    let c = gxr_math::cos(th);
    let s = gxr_math::sin(th);
    let cx = width as f64 / 2.0;
    let cy = height as f64 / 2.0;
    let corners = [(x0, y0), (x0 + w, y0), (x0, y0 + h), (x0 + w, y0 + h)];
    for (px, py) in corners {
        let dx = px - cx;
        let dy = py - cy;
        let sx = dx * c + dy * s;
        let sy = -dx * s + dy * c;
        if sx.abs() > cx + tol || sy.abs() > cy + tol {
            return false;
        }
    }
    true
}

/// Port of `gxr_studio/crop.py:96 _check_angle`
fn check_angle(angle_deg: f64) -> Result<f64, CoreError> {
    if !angle_deg.is_finite() {
        return Err(CoreError::new("bad_angle", "无法裁剪。拉直角度无效。"));
    }
    if angle_deg.abs() > MAX_ANGLE + 1e-9 {
        return Err(CoreError::new(
            "bad_angle",
            "无法裁剪。拉直角度要在 ±45° 之内。",
        ));
    }
    Ok(angle_deg)
}

/// Port of `gxr_studio/crop.py:104 default_crop`
pub fn default_crop(
    preset: &str,
    width: i64,
    height: i64,
    angle_deg: f64,
    flip: bool,
) -> Result<Crop, CoreError> {
    let angle = check_angle(angle_deg)?;
    if width <= 0 || height <= 0 {
        return Err(CoreError::new("too_small", "裁剪框超出画面范围"));
    }
    let ratio =
        target_ratio(preset, width, height, flip)?.unwrap_or_else(|| Ratio::new(width, height));
    let th = angle.abs().to_radians();
    let c = gxr_math::cos(th);
    let s = gxr_math::sin(th);
    let q = ratio.inv().as_f64();
    let w_max = (width as f64 / (c + q * s)).min(height as f64 / (s + q * c));
    let mut w_px = width.min((w_max + 1e-9).floor() as i64);
    let mut h_px = 0_i64;
    let mut found = false;
    while w_px >= MIN_SIDE_PX {
        // Exact rational floor(w_px / ratio + 1/2)
        let num = i128::from(w_px) * i128::from(ratio.denom) * 2 + i128::from(ratio.numer);
        let den = i128::from(ratio.numer) * 2;
        h_px = (num / den) as i64;
        h_px = h_px.min(height);
        if box_inside(
            [
                (width - w_px) as f64 / 2.0,
                (height - h_px) as f64 / 2.0,
                w_px as f64,
                h_px as f64,
            ],
            width,
            height,
            angle,
            1e-6,
        ) {
            found = true;
            break;
        }
        w_px -= 1;
    }
    if !found {
        return Err(CoreError::new("too_small", "裁剪框超出画面范围"));
    }
    let wf = width as f64;
    let hf = height as f64;
    let rect = [
        (width - w_px) as f64 / 2.0 / wf,
        (height - h_px) as f64 / 2.0 / hf,
        w_px as f64 / wf,
        h_px as f64 / hf,
    ];
    Ok(Crop {
        preset: preset.to_string(),
        rect,
        angle_deg: angle,
    })
}

/// Port of `gxr_studio/crop.py:127 fit_target_crop`
pub fn fit_target_crop(
    preset: &str,
    rect: [f64; 4],
    angle_deg: f64,
    width: i64,
    height: i64,
) -> Result<Crop, CoreError> {
    let angle = check_angle(angle_deg)?;
    let ratio = target_ratio(preset, width, height, false)?;
    let [mut x, mut y, mut w, mut h] = rect;
    if !x.is_finite() || !y.is_finite() || !w.is_finite() || !h.is_finite() || w <= 0.0 || h <= 0.0
    {
        return Err(CoreError::new("bad_rect", "裁剪框超出画面范围"));
    }
    let tw_px = w * width as f64;
    let th_px = h * height as f64;
    let mut long_side = MIN_SIDE_PX as f64;
    let mut short_side = MIN_SIDE_PX as f64;
    if preset == "original" {
        let aspect = width as f64 / height as f64;
        long_side = (MIN_SIDE_PX as f64 * aspect.max(1.0 / aspect)).ceil() + 1.0;
        short_side = (MIN_SIDE_PX + 1) as f64;
    } else if let Some(r) = ratio {
        let (small, big) = if r.numer <= r.denom {
            (r.numer, r.denom)
        } else {
            (r.denom, r.numer)
        };
        let k = (MIN_SIDE_PX + small - 1) / small;
        long_side = (k * big) as f64;
        short_side = (k * small) as f64;
    }
    let (need_w, need_h) = if tw_px >= th_px {
        (long_side, short_side)
    } else {
        (short_side, long_side)
    };
    let cx = x + w / 2.0;
    let cy = y + h / 2.0;
    w = w.max(need_w / width as f64).min(1.0);
    h = h.max(need_h / height as f64).min(1.0);
    x = (cx - w / 2.0).max(0.0).min(1.0 - w);
    y = (cy - h / 2.0).max(0.0).min(1.0 - h);
    Ok(Crop {
        preset: preset.to_string(),
        rect: [x, y, w, h],
        angle_deg: angle,
    })
}

/// Port of `gxr_studio/crop.py:162 ratio_off`
pub fn ratio_off(ratio: Ratio, w_px: i64, h_px: i64) -> f64 {
    let got = w_px as f64 / h_px as f64;
    let r1 = ratio.as_f64();
    let r2 = ratio.inv().as_f64();
    ((got - r1).abs() / r1).min((got - r2).abs() / r2)
}

/// Port of `gxr_studio/crop.py:168 check_final`
pub fn check_final(preset: &str, rect: [f64; 4], width: i64, height: i64) -> Result<(), CoreError> {
    check_min_size(rect, width, height)?;
    if let Some(ratio) = target_ratio(preset, width, height, false)? {
        let [_, _, w_px, h_px] = crop_box(rect, width, height);
        if ratio_off(ratio, w_px, h_px) > RATIO_TOLERANCE {
            return Err(CoreError::new("ratio_mismatch", "裁剪框超出画面范围"));
        }
    }
    Ok(())
}

/// Port of `gxr_studio/crop.py:179 check_min_size`
pub fn check_min_size(rect: [f64; 4], width: i64, height: i64) -> Result<(), CoreError> {
    let [_, _, w_px, h_px] = crop_box(rect, width, height);
    if w_px < MIN_SIDE_PX || h_px < MIN_SIDE_PX {
        return Err(CoreError::new("too_small", "裁剪框超出画面范围"));
    }
    Ok(())
}

/// Port of `gxr_studio/crop.py:186 validate_crop`
pub fn validate_crop(
    preset: &str,
    rect: [f64; 4],
    angle_deg: f64,
    width: i64,
    height: i64,
    mirrored: bool,
    check_region: bool,
) -> Result<Crop, CoreError> {
    let angle = check_angle(angle_deg)?;
    let ratio = target_ratio(preset, width, height, false)?;
    let [x, y, w, h] = rect;
    if !x.is_finite() || !y.is_finite() || !w.is_finite() || !h.is_finite() {
        return Err(CoreError::new("bad_rect", "裁剪框超出画面范围"));
    }
    let eps = 1e-9;
    if w <= 0.0 || h <= 0.0 || x < -eps || y < -eps || x + w > 1.0 + eps || y + h > 1.0 + eps {
        return Err(CoreError::new("bad_rect", "裁剪框超出画面范围"));
    }
    let [_, _, w_px, h_px] = crop_box([x, y, w, h], width, height);
    if w_px < MIN_SIDE_PX || h_px < MIN_SIDE_PX {
        return Err(CoreError::new("too_small", "裁剪框超出画面范围"));
    }
    if let Some(r) = ratio
        && ratio_off(r, w_px, h_px) > RATIO_TOLERANCE
    {
        return Err(CoreError::new("ratio_mismatch", "裁剪框超出画面范围"));
    }
    let box_px = [
        x * width as f64,
        y * height as f64,
        w * width as f64,
        h * height as f64,
    ];
    let region_angle = if mirrored { -angle } else { angle };
    if check_region && !box_inside(box_px, width, height, region_angle, EDGE_TOLERANCE_PX) {
        return Err(CoreError::new("crop_outside_image", "裁剪框超出画面范围"));
    }
    Ok(Crop {
        preset: preset.to_string(),
        rect: [x, y, w, h],
        angle_deg: angle,
    })
}

/// Port of `gxr_studio/crop.py:219 affine_coeffs`
pub fn affine_coeffs(box_px: [i64; 4], width: i64, height: i64, angle_deg: f64) -> [f64; 6] {
    let x0 = box_px[0] as f64;
    let y0 = box_px[1] as f64;
    let th = angle_deg.to_radians();
    let c = gxr_math::cos(th);
    let s = gxr_math::sin(th);
    let cx = width as f64 / 2.0;
    let cy = height as f64 / 2.0;
    [
        c,
        s,
        cx + c * (x0 - cx) + s * (y0 - cy),
        -s,
        c,
        cy - s * (x0 - cx) + c * (y0 - cy),
    ]
}

fn parse_rect_array(val: &JsonValue) -> Result<[f64; 4], CoreError> {
    let Some(arr) = val.as_array() else {
        return Err(CoreError::new("bad_rect", "裁剪框超出画面范围"));
    };
    if arr.len() != 4 {
        return Err(CoreError::new("bad_rect", "裁剪框超出画面范围"));
    }
    let mut out = [0.0; 4];
    for (i, item) in arr.iter().enumerate() {
        out[i] = item
            .as_f64()
            .ok_or_else(|| CoreError::new("bad_rect", "裁剪框超出画面范围"))?;
    }
    Ok(out)
}

/// CLI helper: parse `--crop JSON`, validate or compute default crop, return `{"box": ..., "crop": ...}` JSON.
pub fn crop_box_json(width: i64, height: i64, crop_json: &str) -> Result<Json, CoreError> {
    if width <= 0 || height <= 0 {
        return Err(CoreError::new("bad_rect", "裁剪框超出画面范围"));
    }
    let val = parse_json(crop_json, "bad_rect", "裁剪框超出画面范围")?;
    if !matches!(val, JsonValue::Object(_)) {
        return Err(CoreError::new("bad_rect", "裁剪框超出画面范围"));
    }
    let preset = match val.get("preset") {
        None | Some(JsonValue::Null) => "free",
        Some(v) => v
            .as_str()
            .ok_or_else(|| CoreError::new("unknown_preset", "无法裁剪。请重新选择比例。"))?,
    };
    let angle_deg = match val.get("angle_deg") {
        None | Some(JsonValue::Null) => 0.0,
        Some(v) => v
            .as_f64()
            .ok_or_else(|| CoreError::new("bad_angle", "无法裁剪。拉直角度无效。"))?,
    };
    let mirrored = match val.get("mirrored") {
        None | Some(JsonValue::Null) => false,
        Some(v) => v
            .as_bool()
            .ok_or_else(|| CoreError::new("bad_rect", "裁剪框超出画面范围"))?,
    };
    let check_region = match val.get("check_region") {
        None | Some(JsonValue::Null) => true,
        Some(v) => v
            .as_bool()
            .ok_or_else(|| CoreError::new("bad_rect", "裁剪框超出画面范围"))?,
    };
    let flip = match val.get("flip") {
        None | Some(JsonValue::Null) => false,
        Some(v) => v
            .as_bool()
            .ok_or_else(|| CoreError::new("bad_rect", "裁剪框超出画面范围"))?,
    };
    let crop = match val.get("rect") {
        None | Some(JsonValue::Null) => default_crop(preset, width, height, angle_deg, flip)?,
        Some(rect_val) => {
            let rect = parse_rect_array(rect_val)?;
            validate_crop(
                preset,
                rect,
                angle_deg,
                width,
                height,
                mirrored,
                check_region,
            )?
        }
    };
    let px = crop_box(crop.rect, width, height);
    Ok(Json::object([
        ("box", Json::ints(&px)),
        ("crop", crop.to_json()),
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_half_up_matches_python() {
        assert_eq!(round_half_up(0.5), 1);
        assert_eq!(round_half_up(1.5), 2);
        assert_eq!(round_half_up(2.5), 3);
        assert_eq!(round_half_up(-0.5), 0);
        assert_eq!(round_half_up(-1.5), -1);
        assert_eq!(round_half_up(10.5), 11);
    }

    #[test]
    fn xpan_4288_2848() {
        let c = default_crop("xpan-65-24", 4288, 2848, 0.0, false).unwrap();
        let b = crop_box(c.rect, 4288, 2848);
        assert_eq!(b[2], 4288);
        assert_eq!(b[3], 1583);
        assert_eq!(b[0], 0);
    }

    #[test]
    fn validate_errors() {
        assert_eq!(
            validate_crop(
                "xpan-65-24",
                [0.0, 0.0, 1.0, 1.0],
                0.0,
                4288,
                2848,
                false,
                true
            )
            .unwrap_err()
            .kind,
            "ratio_mismatch"
        );
        assert_eq!(
            validate_crop("free", [0.5, 0.5, 0.6, 0.6], 0.0, 4288, 2848, false, true)
                .unwrap_err()
                .kind,
            "bad_rect"
        );
        assert_eq!(
            validate_crop(
                "free",
                [0.0, 0.0, 0.001, 0.001],
                0.0,
                4288,
                2848,
                false,
                true
            )
            .unwrap_err()
            .kind,
            "too_small"
        );
        assert_eq!(
            validate_crop("free", [0.0, 0.0, 1.0, 1.0], 50.0, 4288, 2848, false, true)
                .unwrap_err()
                .kind,
            "bad_angle"
        );
        assert_eq!(
            validate_crop("free", [0.0, 0.0, 1.0, 1.0], 5.0, 4288, 2848, false, true)
                .unwrap_err()
                .kind,
            "crop_outside_image"
        );
        assert_eq!(
            validate_crop("nope", [0.0, 0.0, 1.0, 1.0], 0.0, 4288, 2848, false, true)
                .unwrap_err()
                .kind,
            "unknown_preset"
        );
    }

    #[test]
    fn fit_target_check_final_and_affine() {
        let grown = fit_target_crop("4:3", [0.4, 0.4, 0.004, 0.004], 0.0, 640, 480).unwrap();
        let b = crop_box(grown.rect, 640, 480);
        assert!(b[2].min(b[3]) >= MIN_SIDE_PX);
        assert!(check_final("4:3", grown.rect, 640, 480).is_ok());

        let coeffs = affine_coeffs([10, 20, 100, 100], 640, 480, 0.0);
        assert_eq!(coeffs, [1.0, 0.0, 10.0, -0.0, 1.0, 20.0]);
    }
}
