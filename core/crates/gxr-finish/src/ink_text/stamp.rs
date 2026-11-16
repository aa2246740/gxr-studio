//! Port of `gxr_studio/finish/stamp.py` (`_paint`, `apply_stamp`) and bounded-memory
//! `stamp_hdr_mask` for `gxr-export` HDR tracking.

use gxr_frame::{Val, parse_json};
use gxr_raw::CoreError;

use super::run::{FontPack, draw_run_fields, parse_rgb3, parse_xs, req_field, val_f64};
use crate::geometry::bounds::{checked_mul_u64, fit_count};
use crate::ink::blur::{gaussian_blur, gaussian_blur_radius};
use crate::ink::draw::{bad_coords, draw_polygon};
use crate::ink::image::{InkImage, InkMode, bad_mismatch};
use crate::ink::paste::paste_image;

const GLOW_GAIN: f64 = 1.15;

fn bad_stamp(msg: &str) -> CoreError {
    CoreError::new("bad_request", msg)
}

enum StampOp<'a> {
    Text {
        font: &'a str,
        px: i64,
        cjk: bool,
        text: &'a str,
        xs: Vec<i64>,
        y: i64,
    },
    Poly {
        pts: Vec<[f64; 2]>,
    },
}

struct ParsedStamp<'a> {
    ink: [u8; 3],
    glow: Option<f64>,
    ops: Vec<StampOp<'a>>,
    bbox: [i64; 4],
}

fn parse_stamp_layout<'a>(layout: &'a Val) -> Result<ParsedStamp<'a>, CoreError> {
    if !matches!(layout, Val::Obj(_)) {
        return Err(bad_stamp("stamp layout must be an object"));
    }
    let ink = parse_rgb3(req_field(layout, "ink")?, "ink")?;
    let glow = match layout.get("glow") {
        None | Some(Val::Null) => None,
        Some(v) => {
            let g = val_f64(v, "glow")?;
            if g < 0.0_f64 {
                return Err(CoreError::new("value_error", "radius must be >= 0"));
            }
            Some(g)
        }
    };
    let Val::Arr(raw_ops) = req_field(layout, "ops")? else {
        return Err(bad_stamp("stamp ops must be an array"));
    };

    let mut min_x = i64::MAX;
    let mut min_y = i64::MAX;
    let mut max_x = i64::MIN;
    let mut max_y = i64::MIN;
    let mut has_bounds = false;

    let mut expand = |x0: i64, y0: i64, x1: i64, y1: i64| {
        has_bounds = true;
        min_x = min_x.min(x0);
        min_y = min_y.min(y0);
        max_x = max_x.max(x1);
        max_y = max_y.max(y1);
    };

    if let Some(Val::Arr(rect)) = layout.get("rect")
        && rect.len() == 4
        && let (Ok(rx), Ok(ry), Ok(rw), Ok(rh)) = (
            val_f64(&rect[0], "rect[0]"),
            val_f64(&rect[1], "rect[1]"),
            val_f64(&rect[2], "rect[2]"),
            val_f64(&rect[3], "rect[3]"),
        )
    {
        let rxi = rx.floor() as i64;
        let ryi = ry.floor() as i64;
        let rxi1 = (rx + rw).ceil() as i64;
        let ryi1 = (ry + rh).ceil() as i64;
        expand(rxi, ryi, rxi1, ryi1);
    }

    let mut ops = Vec::with_capacity(raw_ops.len());
    for raw in raw_ops {
        let Some(Val::Str(k)) = raw.get("k") else {
            return Err(bad_stamp("stamp op missing k"));
        };
        match k.as_str() {
            "text" => {
                let Val::Str(font) = req_field(raw, "font")? else {
                    return Err(bad_stamp("stamp text font must be string"));
                };
                let px = super::text::val_i64(req_field(raw, "px")?, "px")?;
                let cjk = match raw.get("cjk") {
                    None | Some(Val::Null) => false,
                    Some(Val::Bool(b)) => *b,
                    _ => return Err(bad_stamp("cjk must be bool")),
                };
                let Val::Str(text) = req_field(raw, "text")? else {
                    return Err(bad_stamp("stamp text must be string"));
                };
                let xs = parse_xs(req_field(raw, "xs")?, "xs")?;
                let y = super::text::val_i64(req_field(raw, "y")?, "y")?;
                if text.chars().count() != xs.len() {
                    return Err(bad_stamp("stamp text length does not match xs length"));
                }
                if let (Some(&x_first), Some(&x_last)) = (xs.first(), xs.last()) {
                    let x_lo = x_first.min(x_last);
                    let x_hi = x_first.max(x_last);
                    let margin = px.saturating_mul(2).saturating_add(32);
                    expand(
                        x_lo.saturating_sub(margin),
                        y.saturating_sub(margin),
                        x_hi.saturating_add(margin),
                        y.saturating_add(px.saturating_mul(3)).saturating_add(32),
                    );
                }
                ops.push(StampOp::Text {
                    font,
                    px,
                    cjk,
                    text,
                    xs,
                    y,
                });
            }
            "poly" => {
                let Val::Arr(raw_pts) = req_field(raw, "pts")? else {
                    return Err(bad_stamp("poly pts must be array"));
                };
                if raw_pts.is_empty() {
                    return Err(bad_coords("poly pts cannot be empty"));
                }
                let mut pts = Vec::with_capacity(raw_pts.len());
                for p in raw_pts {
                    let Val::Arr(pair) = p else {
                        return Err(bad_stamp("poly point must be [x, y]"));
                    };
                    if pair.len() != 2 {
                        return Err(bad_stamp("poly point must have 2 elements"));
                    }
                    let px = val_f64(&pair[0], "pts.x")?;
                    let py = val_f64(&pair[1], "pts.y")?;
                    expand(
                        (px.floor() as i64).saturating_sub(4),
                        (py.floor() as i64).saturating_sub(4),
                        (px.ceil() as i64).saturating_add(4),
                        (py.ceil() as i64).saturating_add(4),
                    );
                    pts.push([px, py]);
                }
                ops.push(StampOp::Poly { pts });
            }
            _ => return Err(bad_stamp("unsupported stamp op kind")),
        }
    }

    let bbox = if has_bounds {
        let pad = if let Some(g) = glow {
            let blur_r = gaussian_blur_radius(g as f32, 3);
            if !blur_r.is_finite() || blur_r >= 2_147_483_648.0_f32 {
                return Err(CoreError::new("value_error", "radius out of range"));
            }
            ((blur_r.floor() as i64) + 1)
                .saturating_mul(3)
                .saturating_add(16)
        } else {
            8
        };
        [
            min_x.saturating_sub(pad),
            min_y.saturating_sub(pad),
            max_x.saturating_add(pad),
            max_y.saturating_add(pad),
        ]
    } else {
        [0, 0, 0, 0]
    };

    Ok(ParsedStamp {
        ink,
        glow,
        ops,
        bbox,
    })
}

fn paint_ops(
    target: &mut InkImage,
    ops: &[StampOp<'_>],
    fonts: &FontPack<'_>,
    fill: [u8; 3],
    ox: i64,
    oy: i64,
) -> Result<bool, CoreError> {
    let mut exact = true;
    let fill4 = [fill[0], fill[1], fill[2], 255];
    for op in ops {
        match op {
            StampOp::Text {
                font,
                px,
                cjk,
                text,
                xs,
                y,
            } => {
                let shifted_xs;
                let xs_slice = if ox == 0 {
                    xs.as_slice()
                } else {
                    let mut buf = Vec::with_capacity(xs.len());
                    for &x in xs {
                        buf.push(
                            x.checked_sub(ox)
                                .ok_or_else(|| bad_coords("stamp x coordinate overflow"))?,
                        );
                    }
                    shifted_xs = buf;
                    shifted_xs.as_slice()
                };
                let sy = y
                    .checked_sub(oy)
                    .ok_or_else(|| bad_coords("stamp y coordinate overflow"))?;
                exact &=
                    draw_run_fields(target, font, *px, *cjk, text, xs_slice, sy, fonts, fill4)?;
            }
            StampOp::Poly { pts } => {
                if ox == 0 && oy == 0 {
                    draw_polygon(target, pts, fill)?;
                } else {
                    let ox_f = ox as f64;
                    let oy_f = oy as f64;
                    let shifted: Vec<[f64; 2]> =
                        pts.iter().map(|p| [p[0] - ox_f, p[1] - oy_f]).collect();
                    draw_polygon(target, &shifted, fill)?;
                }
            }
        }
    }
    Ok(exact)
}

fn apply_parsed_stamp(
    img: &mut InkImage,
    fonts: &FontPack<'_>,
    parsed: &ParsedStamp<'_>,
    ox: i64,
    oy: i64,
) -> Result<bool, CoreError> {
    if img.mode() != InkMode::Rgb {
        return Err(bad_mismatch("apply_stamp requires RGB canvas"));
    }
    let mut exact = true;
    if let Some(glow) = parsed.glow {
        let cw = i64::try_from(img.width()).map_err(|_| bad_coords("canvas width overflow"))?;
        let ch = i64::try_from(img.height()).map_err(|_| bad_coords("canvas height overflow"))?;
        let bx0 = (parsed.bbox[0].saturating_sub(ox)).clamp(0, cw);
        let by0 = (parsed.bbox[1].saturating_sub(oy)).clamp(0, ch);
        let bx1 = (parsed.bbox[2].saturating_sub(ox)).clamp(0, cw);
        let by1 = (parsed.bbox[3].saturating_sub(oy)).clamp(0, ch);
        if bx1 > bx0 && by1 > by0 {
            let sw_i64 = bx1
                .checked_sub(bx0)
                .ok_or_else(|| bad_coords("stamp width overflow"))?;
            let sh_i64 = by1
                .checked_sub(by0)
                .ok_or_else(|| bad_coords("stamp height overflow"))?;
            let sw = u64::try_from(sw_i64).map_err(|_| bad_coords("stamp width overflow"))?;
            let sh = u64::try_from(sh_i64).map_err(|_| bad_coords("stamp height overflow"))?;
            let shift_x = ox
                .checked_add(bx0)
                .ok_or_else(|| bad_coords("stamp x offset overflow"))?;
            let shift_y = oy
                .checked_add(by0)
                .ok_or_else(|| bad_coords("stamp y offset overflow"))?;
            let mut alpha = InkImage::new_filled(InkMode::L, sw, sh, [0, 0, 0, 0])?;
            exact &= paint_ops(
                &mut alpha,
                &parsed.ops,
                fonts,
                [255, 255, 255],
                shift_x,
                shift_y,
            )?;
            let blurred = gaussian_blur(&alpha, glow, glow)?;
            drop(alpha);
            let lut = InkImage::point_table_mul(GLOW_GAIN)?;
            let glow_a = blurred.point(&lut)?;
            drop(blurred);

            let npx = glow_a.as_slice().len();
            let mut glow_rgba = InkImage::new_filled(InkMode::Rgba, sw, sh, [0, 0, 0, 0])?;
            for (dst_px, &a) in glow_rgba
                .data
                .chunks_exact_mut(4)
                .zip(glow_a.as_slice().iter())
            {
                if a != 0 {
                    dst_px[0] = parsed.ink[0];
                    dst_px[1] = parsed.ink[1];
                    dst_px[2] = parsed.ink[2];
                    dst_px[3] = a;
                }
            }
            let _ = npx;
            drop(glow_a);

            let dst_rgba = img.crop(bx0, by0, bx1, by1)?.convert(InkMode::Rgba)?;
            let comp = dst_rgba
                .alpha_composite(&glow_rgba)?
                .convert(InkMode::Rgb)?;
            drop(dst_rgba);
            drop(glow_rgba);
            paste_image(img, &comp, [bx0, by0, bx1, by1], None)?;
        }
    }
    exact &= paint_ops(img, &parsed.ops, fonts, parsed.ink, ox, oy)?;
    Ok(exact)
}

/// Port of `gxr_studio/finish/stamp.py:apply_stamp` (from a precomputed `StampLayout`).
///
/// Returns `Ok(exact)` where `exact == false` if any drawn glyph used hinting or CFF outlines.
pub fn apply_stamp(
    img: &mut InkImage,
    fonts: &FontPack<'_>,
    layout: &Val,
) -> Result<bool, CoreError> {
    if matches!(layout, Val::Null) {
        return Ok(true);
    }
    let parsed = parse_stamp_layout(layout)?;
    apply_parsed_stamp(img, fonts, &parsed, 0, 0)
}

/// Compute the undilated `stamp_hdr_mask` (`w * h` bytes: `1` where the stamp changed black or
/// white RGB, `0` elsewhere) using a bounded sub-region so full-resolution `4288x2848` exports
/// avoid allocating two full-canvas RGB copies.
pub fn stamp_hdr_mask(
    w: u64,
    h: u64,
    fonts: &FontPack<'_>,
    layout: &Val,
) -> Result<Vec<u8>, CoreError> {
    let total = fit_count(checked_mul_u64(w, h)?, 1)?;
    let mut mask = vec![0_u8; total];
    if matches!(layout, Val::Null) || w == 0 || h == 0 {
        return Ok(mask);
    }
    let parsed = parse_stamp_layout(layout)?;
    if parsed.ops.is_empty() {
        return Ok(mask);
    }

    let cw = i64::try_from(w).map_err(|_| bad_coords("mask width overflow"))?;
    let ch = i64::try_from(h).map_err(|_| bad_coords("mask height overflow"))?;
    let bx0 = parsed.bbox[0].clamp(0, cw);
    let by0 = parsed.bbox[1].clamp(0, ch);
    let bx1 = parsed.bbox[2].clamp(0, cw);
    let by1 = parsed.bbox[3].clamp(0, ch);
    if bx1 <= bx0 || by1 <= by0 {
        return Ok(mask);
    }
    let sw = u64::try_from(
        bx1.checked_sub(bx0)
            .ok_or_else(|| bad_coords("sw overflow"))?,
    )
    .map_err(|_| bad_coords("sw overflow"))?;
    let sh = u64::try_from(
        by1.checked_sub(by0)
            .ok_or_else(|| bad_coords("sh overflow"))?,
    )
    .map_err(|_| bad_coords("sh overflow"))?;
    let sw_u = usize::try_from(sw).map_err(|_| bad_coords("sw index overflow"))?;
    let sh_u = usize::try_from(sh).map_err(|_| bad_coords("sh index overflow"))?;
    let w_u = usize::try_from(w).map_err(|_| bad_coords("w index overflow"))?;

    let mut black = InkImage::new_filled(InkMode::Rgb, sw, sh, [0, 0, 0, 255])?;
    apply_parsed_stamp(&mut black, fonts, &parsed, bx0, by0)?;

    let mut white = InkImage::new_filled(InkMode::Rgb, sw, sh, [255, 255, 255, 255])?;
    apply_parsed_stamp(&mut white, fonts, &parsed, bx0, by0)?;

    let b_data = black.as_slice();
    let w_data = white.as_slice();
    let ox_u = usize::try_from(bx0).map_err(|_| bad_coords("ox index overflow"))?;
    let oy_u = usize::try_from(by0).map_err(|_| bad_coords("oy index overflow"))?;
    for sy in 0..sh_u {
        let dst_row = (oy_u + sy) * w_u + ox_u;
        let src_row = sy * sw_u;
        for sx in 0..sw_u {
            let off = (src_row + sx) * 3;
            let b_changed = b_data[off] != 0 || b_data[off + 1] != 0 || b_data[off + 2] != 0;
            let w_changed = w_data[off] != 255 || w_data[off + 1] != 255 || w_data[off + 2] != 255;
            if b_changed || w_changed {
                mask[dst_row + sx] = 1;
            }
        }
    }
    Ok(mask)
}

/// Entry point for `inktext-stamp` CLI.
pub fn inktext_stamp_u64(
    pixels: &[u8],
    w: u64,
    h: u64,
    layout_json: &str,
    font_index_json: &str,
    fonts_blob: &[u8],
) -> Result<(InkImage, bool), CoreError> {
    let mut img = InkImage::from_bytes(InkMode::Rgb, w, h, pixels)?;
    let fonts = FontPack::from_pack(font_index_json, fonts_blob)?;
    let layout = parse_json(layout_json.as_bytes())?;
    let exact = apply_stamp(&mut img, &fonts, &layout)?;
    Ok((img, exact))
}
