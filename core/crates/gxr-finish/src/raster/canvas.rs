//! Port of `gxr_studio/finish/frame_paint.py:background` and the background + photo-paste
//! subset of `gxr_studio/finish/frame.py:draw_frame`.

use gxr_raw::{CoreError, Json};

use crate::geometry::bounds::{
    bad_geometry, checked_mul_u64, checked_rgb_dims, checked_rgb_slice, fit_count, too_big,
    try_vec_u8,
};
use crate::geometry::json::{JsonValue, parse_json};

const INVALID_LAYOUT: &str = "边框版面格式不对。";

#[derive(Clone, Debug, PartialEq)]
pub struct FrameCanvas {
    pub width: usize,
    pub height: usize,
    pub rgb: Vec<u8>,
}

impl FrameCanvas {
    pub fn report_json(&self) -> Json {
        Json::object([
            ("dtype", Json::str("uint8")),
            (
                "shape",
                Json::ints(&[self.height as i64, self.width as i64, 3]),
            ),
        ])
    }
}

fn bad_layout() -> CoreError {
    CoreError::new("bad_frame", INVALID_LAYOUT)
}

fn needs_browser_backdrop() -> CoreError {
    CoreError::new(
        "needs_browser_backdrop",
        "模糊背景或纸张颗粒需要由浏览器渲染",
    )
}

fn parse_u8_rgb(val: &JsonValue) -> Result<[u8; 3], CoreError> {
    let Some(items) = val.as_array() else {
        return Err(bad_layout());
    };
    if items.len() != 3 {
        return Err(bad_layout());
    }
    let mut out = [0_u8; 3];
    for (slot, item) in out.iter_mut().zip(items.iter()) {
        let Some(v) = item.as_strict_int() else {
            return Err(bad_layout());
        };
        if !(0..=255).contains(&v) {
            return Err(bad_layout());
        }
        *slot = v as u8;
    }
    Ok(out)
}

/// Port of `gxr_studio/finish/frame_paint.py:120 background`.
pub fn background(
    width: u64,
    height: u64,
    bg: [u8; 3],
    glow_falloff: Option<f64>,
) -> Result<Vec<u8>, CoreError> {
    let (wu, hu, _, rgb_len) = checked_rgb_dims(width, height)?;
    let mut canvas = try_vec_u8(rgb_len, 0)?;
    let Some(falloff) = glow_falloff else {
        for px in canvas.chunks_exact_mut(3) {
            px.copy_from_slice(&bg);
        }
        return Ok(canvas);
    };
    let hw = (width as f64 / 2.0) as f32;
    let hh = (height as f64 / 2.0) as f32;
    let falloff_f32 = falloff as f32;
    let bg_f32 = [f32::from(bg[0]), f32::from(bg[1]), f32::from(bg[2])];

    let dx_len = fit_count(width, 4)?;
    let mut dx2 = Vec::new();
    dx2.try_reserve_exact(dx_len).map_err(|_| too_big())?;
    for x in 0..wu {
        let dx = (x as f32 - hw) / hw;
        dx2.push(dx * dx);
    }

    let row_stride = wu * 3;
    for y in 0..hu {
        let dy = (y as f32 - hh) / hh;
        let dy2 = dy * dy;
        let row = &mut canvas[y * row_stride..(y + 1) * row_stride];
        for (x, &x2) in dx2.iter().enumerate() {
            let r2 = x2 + dy2;
            let k = 1.0_f32 - (falloff_f32 * r2) / 2.0_f32;
            let base = x * 3;
            row[base] = (bg_f32[0] * k).clamp(0.0_f32, 255.0_f32) as u8;
            row[base + 1] = (bg_f32[1] * k).clamp(0.0_f32, 255.0_f32) as u8;
            row[base + 2] = (bg_f32[2] * k).clamp(0.0_f32, 255.0_f32) as u8;
        }
    }
    Ok(canvas)
}

fn paste_rgb(
    canvas: &mut [u8],
    canvas_size: (usize, usize),
    photo: &[u8],
    photo_size: (usize, usize),
    pos: (i64, i64),
) -> Result<(), CoreError> {
    let (cw, ch) = canvas_size;
    let (pw, ph) = photo_size;
    let (px, py) = pos;
    let cw_i = i64::try_from(cw).map_err(|_| too_big())?;
    let ch_i = i64::try_from(ch).map_err(|_| too_big())?;
    let pw_i = i64::try_from(pw).map_err(|_| too_big())?;
    let ph_i = i64::try_from(ph).map_err(|_| too_big())?;
    let Some(px_end) = px.checked_add(pw_i) else {
        return Err(bad_layout());
    };
    let Some(py_end) = py.checked_add(ph_i) else {
        return Err(bad_layout());
    };

    let dst_x0 = px.max(0);
    let dst_y0 = py.max(0);
    let dst_x1 = px_end.min(cw_i);
    let dst_y1 = py_end.min(ch_i);
    if dst_x1 <= dst_x0 || dst_y1 <= dst_y0 {
        return Ok(());
    }

    let copy_w = usize::try_from(dst_x1 - dst_x0).map_err(|_| too_big())?;
    let row_bytes = fit_count(checked_mul_u64(copy_w as u64, 3)?, 1)?;
    let src_x0 = usize::try_from(dst_x0 - px).map_err(|_| bad_layout())?;
    let dst_ux0 = usize::try_from(dst_x0).map_err(|_| bad_layout())?;

    for dy in dst_y0..dst_y1 {
        let sy = usize::try_from(dy - py).map_err(|_| bad_layout())?;
        let uy = usize::try_from(dy).map_err(|_| bad_layout())?;
        let src_off = (sy * pw + src_x0) * 3;
        let dst_off = (uy * cw + dst_ux0) * 3;
        let src_slice = photo
            .get(src_off..src_off + row_bytes)
            .ok_or_else(bad_geometry)?;
        let dst_slice = canvas
            .get_mut(dst_off..dst_off + row_bytes)
            .ok_or_else(bad_geometry)?;
        dst_slice.copy_from_slice(src_slice);
    }
    Ok(())
}

/// Port of `gxr_studio/finish/frame.py:79 draw_frame` restricted to background + `photo` items.
pub fn draw_frame_canvas(
    photo_rgb: &[u8],
    pw: u64,
    ph: u64,
    layout_json: &str,
) -> Result<FrameCanvas, CoreError> {
    let (pwu, phu, _, _) = checked_rgb_slice(photo_rgb, pw, ph)?;
    let val = parse_json(layout_json, "bad_frame", INVALID_LAYOUT)?;
    if matches!(val, JsonValue::Null) {
        return Err(bad_layout());
    }
    if !matches!(val, JsonValue::Object(_)) {
        return Err(bad_layout());
    }

    let cw_i = val
        .get("width")
        .and_then(JsonValue::as_strict_int)
        .ok_or_else(bad_layout)?;
    let ch_i = val
        .get("height")
        .and_then(JsonValue::as_strict_int)
        .ok_or_else(bad_layout)?;
    let cw = u64::try_from(cw_i).map_err(|_| bad_layout())?;
    let ch = u64::try_from(ch_i).map_err(|_| bad_layout())?;
    let (cwu, chu, _, _) = checked_rgb_dims(cw, ch)?;

    if let Some(grain_val) = val.get("grain")
        && !matches!(grain_val, JsonValue::Null)
    {
        let grain = grain_val.as_f64().ok_or_else(bad_layout)?;
        if grain != 0.0 {
            return Err(needs_browser_backdrop());
        }
    }

    let glow_falloff = match val.get("bgfx") {
        None | Some(JsonValue::Null) => None,
        Some(bgfx) => {
            if !matches!(bgfx, JsonValue::Object(_)) {
                return Err(bad_layout());
            }
            let kind = bgfx.get("kind").and_then(JsonValue::as_str).unwrap_or("");
            if kind == "blur" {
                return Err(needs_browser_backdrop());
            }
            if kind == "glow" {
                let falloff = match bgfx.get("falloff") {
                    None | Some(JsonValue::Null) => 0.05,
                    Some(v) => v.as_f64().ok_or_else(bad_layout)?,
                };
                Some(falloff)
            } else {
                None
            }
        }
    };

    let bg = parse_u8_rgb(val.get("bg").ok_or_else(bad_layout)?)?;
    let items = val
        .get("items")
        .and_then(JsonValue::as_array)
        .ok_or_else(bad_layout)?;

    let mut canvas = background(cw, ch, bg, glow_falloff)?;
    for item in items {
        let kind = item.get("k").and_then(JsonValue::as_str);
        if kind == Some("photo") {
            let x = item
                .get("x")
                .and_then(JsonValue::as_strict_int)
                .ok_or_else(bad_layout)?;
            let y = item
                .get("y")
                .and_then(JsonValue::as_strict_int)
                .ok_or_else(bad_layout)?;
            paste_rgb(&mut canvas, (cwu, chu), photo_rgb, (pwu, phu), (x, y))?;
        }
    }

    Ok(FrameCanvas {
        width: cwu,
        height: chu,
        rgb: canvas,
    })
}

/// Checked-slice entrypoint matching [`draw_frame_canvas`].
pub fn draw_frame_canvas_u64(
    photo_rgb: &[u8],
    pw: u64,
    ph: u64,
    layout_json: &str,
) -> Result<FrameCanvas, CoreError> {
    draw_frame_canvas(photo_rgb, pw, ph, layout_json)
}
