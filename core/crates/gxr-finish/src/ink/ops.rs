//! JSON op-sequence interpreter for `ink-ops` (CLI) and `ink_ops` (WASM).

use gxr_raw::CoreError;

use crate::geometry::bounds::{checked_add_u64, checked_mul_u64, fit_count};
use crate::geometry::json::{JsonValue, parse_json};
use crate::ink::backdrop::{background, blurred_background, paper_grain};
use crate::ink::blur::gaussian_blur;
use crate::ink::carrier::paint_carrier;
use crate::ink::draw::{draw_ellipse, draw_line, draw_polygon, draw_rectangle};
use crate::ink::draw_arc::draw_rounded_rectangle;
use crate::ink::image::{InkImage, InkMode};
use crate::ink::paste::{paste_color, paste_image};
use crate::ink::shapes::{
    RectItem, ShadowItem, paint_ellipse, paint_hole, paint_poly, paint_rect, paint_shadow,
};
use crate::raster::bounds::checked_rgba_slice;

/// An RGBA logo input passed into [`run_ink_ops`].
#[derive(Clone, Debug)]
pub struct LogoInput {
    pub id: String,
    pub image: InkImage,
}

fn bad_ops(msg: &str) -> CoreError {
    CoreError::new("bad_request", msg)
}

pub(crate) fn req_str<'a>(obj: &'a JsonValue, key: &str, msg: &str) -> Result<&'a str, CoreError> {
    obj.get(key)
        .and_then(JsonValue::as_str)
        .ok_or_else(|| bad_ops(msg))
}

pub(crate) fn req_int(obj: &JsonValue, key: &str, msg: &str) -> Result<i64, CoreError> {
    obj.get(key)
        .and_then(JsonValue::as_strict_int)
        .ok_or_else(|| bad_ops(msg))
}

#[allow(dead_code)]
pub(crate) fn opt_int(
    obj: &JsonValue,
    key: &str,
    default: i64,
    msg: &str,
) -> Result<i64, CoreError> {
    match obj.get(key) {
        None | Some(JsonValue::Null) => Ok(default),
        Some(v) => v.as_strict_int().ok_or_else(|| bad_ops(msg)),
    }
}

pub(crate) fn req_f64(obj: &JsonValue, key: &str, msg: &str) -> Result<f64, CoreError> {
    let v = obj
        .get(key)
        .and_then(JsonValue::as_f64)
        .ok_or_else(|| bad_ops(msg))?;
    if !v.is_finite() {
        return Err(bad_ops(msg));
    }
    Ok(v)
}

#[allow(dead_code)]
pub(crate) fn opt_f64(
    obj: &JsonValue,
    key: &str,
    default: f64,
    msg: &str,
) -> Result<f64, CoreError> {
    match obj.get(key) {
        None | Some(JsonValue::Null) => Ok(default),
        Some(v) => {
            let f = v.as_f64().ok_or_else(|| bad_ops(msg))?;
            if !f.is_finite() {
                return Err(bad_ops(msg));
            }
            Ok(f)
        }
    }
}

fn parse_u8(val: i64, msg: &str) -> Result<u8, CoreError> {
    u8::try_from(val).map_err(|_| bad_ops(msg))
}

pub(crate) fn parse_color(val: &JsonValue, msg: &str) -> Result<[u8; 4], CoreError> {
    if let Some(n) = val.as_strict_int() {
        let v = parse_u8(n, msg)?;
        return Ok([v, v, v, 255]);
    }
    let arr = val.as_array().ok_or_else(|| bad_ops(msg))?;
    match arr.len() {
        1 => {
            let v = parse_u8(arr[0].as_strict_int().ok_or_else(|| bad_ops(msg))?, msg)?;
            Ok([v, v, v, 255])
        }
        3 => Ok([
            parse_u8(arr[0].as_strict_int().ok_or_else(|| bad_ops(msg))?, msg)?,
            parse_u8(arr[1].as_strict_int().ok_or_else(|| bad_ops(msg))?, msg)?,
            parse_u8(arr[2].as_strict_int().ok_or_else(|| bad_ops(msg))?, msg)?,
            255,
        ]),
        4 => Ok([
            parse_u8(arr[0].as_strict_int().ok_or_else(|| bad_ops(msg))?, msg)?,
            parse_u8(arr[1].as_strict_int().ok_or_else(|| bad_ops(msg))?, msg)?,
            parse_u8(arr[2].as_strict_int().ok_or_else(|| bad_ops(msg))?, msg)?,
            parse_u8(arr[3].as_strict_int().ok_or_else(|| bad_ops(msg))?, msg)?,
        ]),
        _ => Err(bad_ops(msg)),
    }
}

fn parse_box4(val: &JsonValue, default_w: usize, default_h: usize) -> Result<[i64; 4], CoreError> {
    let arr = val.as_array().ok_or_else(|| bad_ops("box must be array"))?;
    match arr.len() {
        2 => {
            let x0 = arr[0]
                .as_strict_int()
                .ok_or_else(|| bad_ops("bad box int"))?;
            let y0 = arr[1]
                .as_strict_int()
                .ok_or_else(|| bad_ops("bad box int"))?;
            let x1 = x0
                .checked_add(default_w as i64)
                .ok_or_else(|| bad_ops("box overflow"))?;
            let y1 = y0
                .checked_add(default_h as i64)
                .ok_or_else(|| bad_ops("box overflow"))?;
            Ok([x0, y0, x1, y1])
        }
        4 => Ok([
            arr[0]
                .as_strict_int()
                .ok_or_else(|| bad_ops("bad box int"))?,
            arr[1]
                .as_strict_int()
                .ok_or_else(|| bad_ops("bad box int"))?,
            arr[2]
                .as_strict_int()
                .ok_or_else(|| bad_ops("bad box int"))?,
            arr[3]
                .as_strict_int()
                .ok_or_else(|| bad_ops("bad box int"))?,
        ]),
        _ => Err(bad_ops("box must have 2 or 4 integers")),
    }
}

/// Parse `logo_index_json` + `logo_blob` from WASM into [`LogoInput`]s.
pub fn decode_logo_bundle(
    logo_index_json: &str,
    logo_blob: &[u8],
) -> Result<Vec<LogoInput>, CoreError> {
    let trimmed = logo_index_json.trim();
    if trimmed.is_empty() || trimmed == "[]" || trimmed == "{}" {
        return Ok(Vec::new());
    }
    let parsed = parse_json(trimmed, "bad_request", "标志索引格式不对。")?;
    let mut out = Vec::new();
    let entries: Vec<(&str, &JsonValue)> = match &parsed {
        JsonValue::Array(arr) => {
            let mut v = Vec::with_capacity(arr.len());
            for item in arr {
                let id = req_str(item, "id", "logo id must be string")?;
                v.push((id, item));
            }
            v
        }
        JsonValue::Object(pairs) => pairs.iter().map(|(k, v)| (k.as_str(), v)).collect(),
        _ => return Err(bad_ops("logo_index_json must be array or object")),
    };

    for (id, meta) in entries {
        let w = u64::try_from(req_int(meta, "w", "logo w must be integer")?)
            .map_err(|_| bad_ops("logo w invalid"))?;
        let h = u64::try_from(req_int(meta, "h", "logo h must be integer")?)
            .map_err(|_| bad_ops("logo h invalid"))?;
        let offset = u64::try_from(req_int(meta, "offset", "logo offset must be integer")?)
            .map_err(|_| bad_ops("logo offset invalid"))?;
        let len = u64::try_from(req_int(meta, "len", "logo len must be integer")?)
            .map_err(|_| bad_ops("logo len invalid"))?;
        let end = checked_add_u64(offset, len)?;
        if end > logo_blob.len() as u64 {
            return Err(bad_ops("logo slice out of bounds"));
        }
        let off_u = fit_count(offset, 1)?;
        let end_u = fit_count(end, 1)?;
        let slice = &logo_blob[off_u..end_u];
        let _ = checked_rgba_slice(slice, w, h)?;
        let expected_bytes = checked_mul_u64(checked_mul_u64(w, h)?, 4)?;
        if len != expected_bytes {
            return Err(bad_ops("logo len does not match RGBA dimensions"));
        }
        let image = InkImage::from_bytes(InkMode::Rgba, w, h, slice)?;
        out.push(LogoInput {
            id: id.to_string(),
            image,
        });
    }
    Ok(out)
}

fn find_reg<'a>(regs: &'a [(String, InkImage)], name: &str) -> Result<&'a InkImage, CoreError> {
    regs.iter()
        .rev()
        .find(|(k, _)| k == name)
        .map(|(_, img)| img)
        .ok_or_else(|| bad_ops("unknown image register"))
}

fn set_reg(regs: &mut Vec<(String, InkImage)>, name: &str, img: InkImage) {
    if let Some(pos) = regs.iter().position(|(k, _)| k == name) {
        regs[pos].1 = img;
    } else {
        regs.push((name.to_string(), img));
    }
}

/// Execute an array of `ink-ops` commands on `(canvas, w, h, mode)`.
pub fn run_ink_ops(
    canvas: &[u8],
    w: u64,
    h: u64,
    mode: InkMode,
    ops_json: &str,
    logos: &[LogoInput],
) -> Result<InkImage, CoreError> {
    let mut cur = InkImage::from_bytes(mode, w, h, canvas)?;
    let ops_val = parse_json(ops_json, "bad_request", "绘图指令格式不对。")?;
    let ops = ops_val
        .as_array()
        .ok_or_else(|| bad_ops("ops_json must be a JSON array"))?;

    let mut regs: Vec<(String, InkImage)> = Vec::new();
    for logo in logos {
        set_reg(&mut regs, &format!("logo:{}", logo.id), logo.image.clone());
    }

    for step in ops {
        let op = req_str(step, "op", "missing op field")?;
        let target_reg = step.get("target").and_then(JsonValue::as_str);

        if let Some(reg_name) = target_reg {
            let mut img = find_reg(&regs, reg_name)?.clone();
            apply_op(&mut img, op, step, &regs, Some(&cur))?;
            set_reg(&mut regs, reg_name, img);
        } else {
            apply_op(&mut cur, op, step, &regs, None)?;
        }

        if let Some(save_as) = step.get("save_as").and_then(JsonValue::as_str) {
            let img = match target_reg {
                Some(r) => find_reg(&regs, r)?.clone(),
                None => cur.clone(),
            };
            set_reg(&mut regs, save_as, img);
        }
    }
    Ok(cur)
}

fn apply_op(
    img: &mut InkImage,
    op: &str,
    step: &JsonValue,
    regs: &[(String, InkImage)],
    canvas_opt: Option<&InkImage>,
) -> Result<(), CoreError> {
    match op {
        "new" => {
            let m = InkMode::parse(req_str(step, "mode", "new requires mode")?)?;
            let nw = u64::try_from(req_int(step, "w", "new requires integer w")?)
                .map_err(|_| bad_ops("invalid w"))?;
            let nh = u64::try_from(req_int(step, "h", "new requires integer h")?)
                .map_err(|_| bad_ops("invalid h"))?;
            let fill = match step.get("color") {
                Some(c) => parse_color(c, "bad color")?,
                None => [0, 0, 0, 0],
            };
            *img = InkImage::new_filled(m, nw, nh, fill)?;
        }
        "save_reg" => {
            // handled via save_as on the op
        }
        "load_reg" => {
            let reg = req_str(step, "reg", "load_reg requires reg")?;
            *img = find_reg(regs, reg)?.clone();
        }
        "convert" => {
            let m = InkMode::parse(req_str(step, "mode", "convert requires mode")?)?;
            *img = img.convert(m)?;
        }
        "get_alpha" => {
            *img = img.get_alpha()?;
        }
        "put_alpha_const" => {
            let a = parse_u8(
                req_int(step, "alpha", "alpha must be integer")?,
                "alpha 0..255",
            )?;
            img.put_alpha_const(a)?;
        }
        "put_alpha_reg" => {
            let reg = req_str(step, "reg", "put_alpha_reg requires reg")?;
            let mask = find_reg(regs, reg)?;
            img.put_alpha_image(mask)?;
        }
        "point_mul" => {
            let scale = req_f64(step, "scale", "point_mul requires finite scale")?;
            let lut = InkImage::point_table_mul(scale)?;
            *img = img.point(&lut)?;
        }
        "point_lut" => {
            let arr = step
                .get("lut")
                .and_then(JsonValue::as_array)
                .ok_or_else(|| bad_ops("point_lut requires lut array"))?;
            let mut lut = Vec::with_capacity(arr.len());
            for v in arr {
                let n = v.as_strict_int().ok_or_else(|| bad_ops("lut int"))?;
                lut.push(parse_u8(n, "lut entry 0..255")?);
            }
            *img = img.point(&lut)?;
        }
        "alpha_composite_reg" => {
            let reg = req_str(step, "reg", "alpha_composite_reg requires reg")?;
            let over = find_reg(regs, reg)?;
            *img = img.alpha_composite(over)?;
        }
        "rotate90" => {
            *img = img.rotate_90()?;
        }
        "crop" => {
            let arr = step
                .get("box")
                .and_then(JsonValue::as_array)
                .ok_or_else(|| bad_ops("crop requires 4-element box"))?;
            if arr.len() != 4 {
                return Err(bad_ops("crop requires 4-element box"));
            }
            if arr.iter().all(|v| v.as_strict_int().is_some()) {
                let x0 = arr[0].as_strict_int().unwrap_or(0);
                let y0 = arr[1].as_strict_int().unwrap_or(0);
                let x1 = arr[2].as_strict_int().unwrap_or(0);
                let y1 = arr[3].as_strict_int().unwrap_or(0);
                *img = img.crop(x0, y0, x1, y1)?;
            } else {
                let x0 = arr[0].as_f64().ok_or_else(|| bad_ops("bad crop box"))?;
                let y0 = arr[1].as_f64().ok_or_else(|| bad_ops("bad crop box"))?;
                let x1 = arr[2].as_f64().ok_or_else(|| bad_ops("bad crop box"))?;
                let y1 = arr[3].as_f64().ok_or_else(|| bad_ops("bad crop box"))?;
                *img = img.crop_f64([x0, y0, x1, y1])?;
            }
        }
        "paste_image" => {
            let src_reg = req_str(step, "src_reg", "paste_image requires src_reg")?;
            let src = if src_reg == "$canvas" {
                canvas_opt.ok_or_else(|| bad_ops("no main canvas"))?
            } else {
                find_reg(regs, src_reg)?
            };
            let box_val = step
                .get("box")
                .ok_or_else(|| bad_ops("paste requires box"))?;
            let dst_box = parse_box4(box_val, src.width, src.height)?;
            let mask = match step.get("mask_reg").and_then(JsonValue::as_str) {
                Some(mr) => Some(find_reg(regs, mr)?),
                None => None,
            };
            paste_image(img, src, dst_box, mask)?;
        }
        "paste_color" => {
            let color_val = step
                .get("color")
                .ok_or_else(|| bad_ops("paste_color requires color"))?;
            let color = parse_color(color_val, "bad color")?;
            let mask = match step.get("mask_reg").and_then(JsonValue::as_str) {
                Some(mr) => Some(find_reg(regs, mr)?),
                None => None,
            };
            let (mw, mh) = match mask {
                Some(m) => (m.width, m.height),
                None => (0, 0),
            };
            let box_val = step
                .get("box")
                .ok_or_else(|| bad_ops("paste_color requires box"))?;
            let arr = box_val
                .as_array()
                .ok_or_else(|| bad_ops("box must be array"))?;
            if arr.len() == 2 && mask.is_none() {
                return Err(bad_ops("2-tuple box without mask cannot determine size"));
            }
            let dst_box = parse_box4(box_val, mw, mh)?;
            paste_color(img, color, dst_box, mask)?;
        }
        "draw_polygon" => {
            let pts = parse_pts(step.get("pts"), "draw_polygon requires pts")?;
            if let Some(fill) = parse_opt_ink3(step.get("fill"))? {
                draw_polygon(img, &pts, fill)?;
            }
        }
        "draw_rectangle" => {
            let xy = parse_box4_f64(step.get("box"), "draw_rectangle requires 4-number box")?;
            let fill = parse_opt_ink3(step.get("fill"))?;
            let outline = parse_opt_ink3(step.get("outline"))?;
            let width = i32::try_from(opt_int(step, "width", 1, "width must be int")?)
                .map_err(|_| bad_ops("width out of i32 range"))?;
            draw_rectangle(img, xy, fill, outline, width)?;
        }
        "draw_rounded_rectangle" => {
            let xy = parse_box4_f64(
                step.get("box"),
                "draw_rounded_rectangle requires 4-number box",
            )?;
            let radius = opt_f64(step, "radius", 0.0, "radius must be finite")?;
            let fill = parse_opt_ink3(step.get("fill"))?;
            let outline = parse_opt_ink3(step.get("outline"))?;
            let width = i32::try_from(opt_int(step, "width", 1, "width must be int")?)
                .map_err(|_| bad_ops("width out of i32 range"))?;
            let corners = parse_corners(step.get("corners"))?;
            draw_rounded_rectangle(img, xy, radius, fill, outline, width, corners)?;
        }
        "draw_ellipse" => {
            let xy = parse_box4_f64(step.get("box"), "draw_ellipse requires 4-number box")?;
            if let Some(fill) = parse_opt_ink3(step.get("fill"))? {
                draw_ellipse(img, xy, fill)?;
            }
        }
        "draw_line" => {
            let pts = parse_pts(step.get("pts"), "draw_line requires pts")?;
            let width = i32::try_from(opt_int(step, "width", 0, "width must be int")?)
                .map_err(|_| bad_ops("width out of i32 range"))?;
            if let Some(fill) = parse_opt_ink3(step.get("fill"))? {
                draw_line(img, &pts, fill, width)?;
            }
        }
        "gaussian_blur" => {
            let rx = opt_f64(step, "radius", 0.0, "radius must be finite")?;
            let ry = opt_f64(step, "y_radius", rx, "y_radius must be finite")?;
            *img = gaussian_blur(img, rx, ry)?;
        }
        "paint_rect" => {
            let item = RectItem {
                x: req_int(step, "x", "paint_rect requires int x")?,
                y: req_int(step, "y", "paint_rect requires int y")?,
                w: req_int(step, "w", "paint_rect requires int w")?,
                h: req_int(step, "h", "paint_rect requires int h")?,
                radius: opt_f64(step, "radius", 0.0, "radius must be finite")?,
                fill: parse_opt_ink3(step.get("fill"))?,
                outline: parse_opt_ink3(step.get("outline"))?,
                stroke_w: opt_f64(step, "stroke_w", 1.0, "stroke_w must be finite")?,
                alpha: opt_f64(step, "alpha", 1.0, "alpha must be finite")?,
            };
            paint_rect(img, &item)?;
        }
        "paint_poly" => {
            let pts = parse_pts(step.get("pts"), "paint_poly requires pts")?;
            let fill = parse_opt_ink3(step.get("fill"))?
                .ok_or_else(|| bad_ops("paint_poly requires fill"))?;
            let alpha = opt_f64(step, "alpha", 1.0, "alpha must be finite")?;
            paint_poly(img, &pts, fill, alpha)?;
        }
        "paint_ellipse" => {
            let cx = req_f64(step, "cx", "paint_ellipse requires cx")?;
            let cy = req_f64(step, "cy", "paint_ellipse requires cy")?;
            let r = req_f64(step, "r", "paint_ellipse requires r")?;
            let fill = parse_opt_ink3(step.get("fill"))?
                .ok_or_else(|| bad_ops("paint_ellipse requires fill"))?;
            let alpha = opt_f64(step, "alpha", 1.0, "alpha must be finite")?;
            paint_ellipse(img, cx, cy, r, fill, alpha)?;
        }
        "paint_hole" => {
            let x = req_int(step, "x", "paint_hole requires int x")?;
            let y = req_int(step, "y", "paint_hole requires int y")?;
            let w = req_int(step, "w", "paint_hole requires int w")?;
            let h = req_int(step, "h", "paint_hole requires int h")?;
            let radius = opt_f64(step, "radius", 0.0, "radius must be finite")?;
            let base_reg = step
                .get("base_reg")
                .and_then(JsonValue::as_str)
                .unwrap_or("base");
            let base = if base_reg == "$canvas" {
                canvas_opt.ok_or_else(|| bad_ops("no main canvas"))?
            } else {
                find_reg(regs, base_reg)?
            };
            paint_hole(img, x, y, w, h, radius, base)?;
        }
        "paint_shadow" => {
            let color = parse_opt_ink3(step.get("color"))?.unwrap_or([0, 0, 0]);
            let item = ShadowItem {
                x: req_int(step, "x", "paint_shadow requires int x")?,
                y: req_int(step, "y", "paint_shadow requires int y")?,
                w: req_int(step, "w", "paint_shadow requires int w")?,
                h: req_int(step, "h", "paint_shadow requires int h")?,
                blur: req_f64(step, "blur", "paint_shadow requires blur")?,
                dx: opt_int(step, "dx", 0, "dx must be int")?,
                dy: opt_int(step, "dy", 0, "dy must be int")?,
                radius: opt_f64(step, "radius", 0.0, "radius must be finite")?,
                alpha: opt_f64(step, "alpha", 0.3, "alpha must be finite")?,
                color,
            };
            paint_shadow(img, &item)?;
        }
        "background" => {
            let w = u64::try_from(req_int(step, "w", "background requires integer w")?)
                .map_err(|_| bad_ops("invalid w"))?;
            let h = u64::try_from(req_int(step, "h", "background requires integer h")?)
                .map_err(|_| bad_ops("invalid h"))?;
            let bg = parse_opt_ink3(step.get("bg").or_else(|| step.get("color")))?
                .ok_or_else(|| bad_ops("background requires bg or color"))?;
            let kind = step.get("kind").and_then(JsonValue::as_str);
            let glow_falloff = match step.get("falloff") {
                Some(JsonValue::Null) => None,
                Some(v) => {
                    let f = v
                        .as_f64()
                        .ok_or_else(|| bad_ops("falloff must be finite"))?;
                    if !f.is_finite() {
                        return Err(bad_ops("falloff must be finite"));
                    }
                    if kind.is_some_and(|k| k != "glow") {
                        None
                    } else {
                        Some(f)
                    }
                }
                None => {
                    if kind == Some("glow") {
                        Some(0.05)
                    } else {
                        None
                    }
                }
            };
            *img = background(w, h, bg, glow_falloff)?;
        }
        "blurred_background" => {
            let w = u64::try_from(req_int(step, "w", "blurred_background requires integer w")?)
                .map_err(|_| bad_ops("invalid w"))?;
            let h = u64::try_from(req_int(step, "h", "blurred_background requires integer h")?)
                .map_err(|_| bad_ops("invalid h"))?;
            let sigma = opt_f64(step, "sigma", 90.0, "sigma must be finite")?;
            let sat = opt_f64(step, "sat", 1.0, "sat must be finite")?;
            let veil = opt_f64(step, "veil", 1.0, "veil must be finite")?;
            let src = match step.get("photo_reg").and_then(JsonValue::as_str) {
                Some(r) => find_reg(regs, r)?,
                None => img,
            };
            *img = blurred_background(src, w, h, sigma, sat, veil)?;
        }
        "paper_grain" => {
            let amount = req_f64(step, "amount", "paper_grain requires finite amount")?;
            *img = paper_grain(img, amount)?;
        }
        "paint_carrier" => {
            let x = req_int(step, "x", "paint_carrier requires int x")?;
            let y = req_int(step, "y", "paint_carrier requires int y")?;
            let w = req_int(step, "w", "paint_carrier requires int w")?;
            let h = req_int(step, "h", "paint_carrier requires int h")?;
            let c = req_int(step, "c", "paint_carrier requires int c")?;
            let jit = req_int(step, "jit", "paint_carrier requires int jit")?;
            let seed = u64::try_from(req_int(
                step,
                "seed",
                "paint_carrier requires non-negative seed",
            )?)
            .map_err(|_| bad_ops("invalid seed"))?;
            let fill = parse_opt_ink3(step.get("fill"))?
                .ok_or_else(|| bad_ops("paint_carrier requires fill"))?;
            let paper = parse_opt_ink3(step.get("paper"))?
                .ok_or_else(|| bad_ops("paint_carrier requires paper"))?;
            paint_carrier(img, x, y, w, h, c, jit, seed, fill, paper)?;
        }
        _ => return Err(bad_ops("unsupported ink op")),
    }
    Ok(())
}

pub(crate) fn parse_opt_ink3(val: Option<&JsonValue>) -> Result<Option<[u8; 3]>, CoreError> {
    match val {
        None | Some(JsonValue::Null) => Ok(None),
        Some(v) => {
            let c = parse_color(v, "bad ink color")?;
            Ok(Some([c[0], c[1], c[2]]))
        }
    }
}

pub(crate) fn parse_box4_f64(val: Option<&JsonValue>, msg: &str) -> Result<[f64; 4], CoreError> {
    let arr = val
        .and_then(JsonValue::as_array)
        .ok_or_else(|| bad_ops(msg))?;
    if arr.len() != 4 {
        return Err(bad_ops(msg));
    }
    let mut out = [0.0_f64; 4];
    for (i, v) in arr.iter().enumerate() {
        let f = v.as_f64().ok_or_else(|| bad_ops(msg))?;
        if !f.is_finite() {
            return Err(bad_ops(msg));
        }
        out[i] = f;
    }
    Ok(out)
}

pub(crate) fn parse_pts(val: Option<&JsonValue>, msg: &str) -> Result<Vec<[f64; 2]>, CoreError> {
    let arr = val
        .and_then(JsonValue::as_array)
        .ok_or_else(|| bad_ops(msg))?;
    let mut out = Vec::with_capacity(arr.len());
    for item in arr {
        let pair = item.as_array().ok_or_else(|| bad_ops(msg))?;
        if pair.len() != 2 {
            return Err(bad_ops(msg));
        }
        let x = pair[0].as_f64().ok_or_else(|| bad_ops(msg))?;
        let y = pair[1].as_f64().ok_or_else(|| bad_ops(msg))?;
        if !x.is_finite() || !y.is_finite() {
            return Err(bad_ops(msg));
        }
        out.push([x, y]);
    }
    Ok(out)
}

pub(crate) fn parse_corners(val: Option<&JsonValue>) -> Result<[bool; 4], CoreError> {
    match val {
        None | Some(JsonValue::Null) => Ok([true, true, true, true]),
        Some(v) => {
            let arr = v
                .as_array()
                .ok_or_else(|| bad_ops("corners must be 4 booleans"))?;
            if arr.len() != 4 {
                return Err(bad_ops("corners must be 4 booleans"));
            }
            let mut out = [true; 4];
            for (i, item) in arr.iter().enumerate() {
                out[i] = item
                    .as_bool()
                    .ok_or_else(|| bad_ops("corners must be 4 booleans"))?;
            }
            Ok(out)
        }
    }
}
