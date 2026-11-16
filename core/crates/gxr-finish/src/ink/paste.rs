//! Port of Pillow 12.2.0 `src/libImaging/Paste.c`, `Fill.c`, `ImagingUtils.h`, and
//! `Image.py:Image.paste`.

use gxr_raw::CoreError;

use crate::ink::image::{InkImage, InkMode, bad_mismatch};

#[inline]
pub(crate) const fn shift_for_div255(a: u32) -> u32 {
    ((a >> 8) + a) >> 8
}

#[inline]
pub(crate) fn blend_u8(mask: u8, dst: u8, src: u8) -> u8 {
    let t = u32::from(dst) * u32::from(255 - mask) + u32::from(src) * u32::from(mask) + 128;
    shift_for_div255(t) as u8
}

struct ClippedBox {
    dx: usize,
    dy: usize,
    sx: usize,
    sy: usize,
    w: usize,
    h: usize,
}

fn clip_paste_box(
    dst_w: usize,
    dst_h: usize,
    dx0: i64,
    dy0: i64,
    box_w: i64,
    box_h: i64,
) -> Option<ClippedBox> {
    let mut xsize = box_w;
    let mut ysize = box_h;
    let mut sx0: i64 = 0;
    let mut sy0: i64 = 0;
    let mut dx = dx0;
    let mut dy = dy0;

    if dx < 0 {
        xsize = xsize.saturating_add(dx);
        sx0 = dx.saturating_neg();
        dx = 0;
    }
    if dx + xsize > dst_w as i64 {
        xsize = dst_w as i64 - dx;
    }
    if dy < 0 {
        ysize = ysize.saturating_add(dy);
        sy0 = dy.saturating_neg();
        dy = 0;
    }
    if dy + ysize > dst_h as i64 {
        ysize = dst_h as i64 - dy;
    }

    if xsize <= 0 || ysize <= 0 {
        return None;
    }
    Some(ClippedBox {
        dx: dx as usize,
        dy: dy as usize,
        sx: sx0 as usize,
        sy: sy0 as usize,
        w: xsize as usize,
        h: ysize as usize,
    })
}

/// Paste `src` at `(dx, dy)` onto `dst` without a mask (used directly by `crop` and `paste_image`).
pub(crate) fn paste_image_at(
    dst: &mut InkImage,
    src: &InkImage,
    dx: i64,
    dy: i64,
) -> Result<(), CoreError> {
    let dx1 = dx
        .checked_add(src.width as i64)
        .ok_or_else(|| bad_mismatch("box overflow"))?;
    let dy1 = dy
        .checked_add(src.height as i64)
        .ok_or_else(|| bad_mismatch("box overflow"))?;
    paste_image(dst, src, [dx, dy, dx1, dy1], None)
}

/// Port of `Image.paste(im, box, mask)` when `im` is an [`InkImage`].
pub fn paste_image(
    dst: &mut InkImage,
    src: &InkImage,
    dst_box: [i64; 4],
    mask: Option<&InkImage>,
) -> Result<(), CoreError> {
    let [dx0, dy0, dx1, dy1] = dst_box;
    let xsize = dx1
        .checked_sub(dx0)
        .ok_or_else(|| bad_mismatch("box overflow"))?;
    let ysize = dy1
        .checked_sub(dy0)
        .ok_or_else(|| bad_mismatch("box overflow"))?;
    if xsize != src.width as i64 || ysize != src.height as i64 {
        return Err(bad_mismatch("images do not match"));
    }
    if let Some(m) = mask
        && (m.width != src.width || m.height != src.height)
    {
        return Err(bad_mismatch("images do not match"));
    }

    // Match Image.py:1891-1893: convert unless dst is RGB and src is RGBA (or same mode).
    let owned_src;
    let src_ref = if dst.mode == src.mode || (dst.mode == InkMode::Rgb && src.mode == InkMode::Rgba)
    {
        src
    } else {
        owned_src = src.convert(dst.mode)?;
        &owned_src
    };

    let Some(c) = clip_paste_box(dst.width, dst.height, dx0, dy0, xsize, ysize) else {
        return Ok(());
    };

    let dst_ch = dst.mode.channels();
    let src_ch = src_ref.mode.channels();

    match mask {
        None => {
            if dst_ch == src_ch {
                let row_bytes = c.w * dst_ch;
                for row in 0..c.h {
                    let dst_off = ((c.dy + row) * dst.width + c.dx) * dst_ch;
                    let src_off = ((c.sy + row) * src_ref.width + c.sx) * src_ch;
                    dst.data[dst_off..dst_off + row_bytes]
                        .copy_from_slice(&src_ref.data[src_off..src_off + row_bytes]);
                }
            } else {
                // dst is RGB (3ch), src_ref is RGBA (4ch)
                for row in 0..c.h {
                    let dst_row = ((c.dy + row) * dst.width + c.dx) * 3;
                    let src_row = ((c.sy + row) * src_ref.width + c.sx) * 4;
                    for col in 0..c.w {
                        let d = dst_row + col * 3;
                        let s = src_row + col * 4;
                        dst.data[d] = src_ref.data[s];
                        dst.data[d + 1] = src_ref.data[s + 1];
                        dst.data[d + 2] = src_ref.data[s + 2];
                    }
                }
            }
        }
        Some(m) if m.mode == InkMode::L => {
            for row in 0..c.h {
                let dst_row = ((c.dy + row) * dst.width + c.dx) * dst_ch;
                let src_row = ((c.sy + row) * src_ref.width + c.sx) * src_ch;
                let mask_row = (c.sy + row) * m.width + c.sx;
                for col in 0..c.w {
                    let a = m.data[mask_row + col];
                    let d = dst_row + col * dst_ch;
                    let s = src_row + col * src_ch;
                    for ch in 0..dst_ch {
                        dst.data[d + ch] = blend_u8(a, dst.data[d + ch], src_ref.data[s + ch]);
                    }
                }
            }
        }
        Some(m) if m.mode == InkMode::Rgba => {
            for row in 0..c.h {
                let dst_row = ((c.dy + row) * dst.width + c.dx) * dst_ch;
                let src_row = ((c.sy + row) * src_ref.width + c.sx) * src_ch;
                let mask_row = ((c.sy + row) * m.width + c.sx) * 4;
                for col in 0..c.w {
                    let a = m.data[mask_row + col * 4 + 3];
                    let d = dst_row + col * dst_ch;
                    let s = src_row + col * src_ch;
                    for ch in 0..dst_ch {
                        dst.data[d + ch] = blend_u8(a, dst.data[d + ch], src_ref.data[s + ch]);
                    }
                }
            }
        }
        Some(_) => return Err(bad_mismatch("bad transparency mask")),
    }
    Ok(())
}

/// Port of `Image.paste(color, box, mask)` (`Fill.c:ImagingFill2`).
pub fn paste_color(
    dst: &mut InkImage,
    color: [u8; 4],
    dst_box: [i64; 4],
    mask: Option<&InkImage>,
) -> Result<(), CoreError> {
    let [dx0, dy0, dx1, dy1] = dst_box;
    let xsize = dx1
        .checked_sub(dx0)
        .ok_or_else(|| bad_mismatch("box overflow"))?;
    let ysize = dy1
        .checked_sub(dy0)
        .ok_or_else(|| bad_mismatch("box overflow"))?;
    if let Some(m) = mask
        && (xsize != m.width as i64 || ysize != m.height as i64)
    {
        return Err(bad_mismatch("images do not match"));
    }

    let Some(c) = clip_paste_box(dst.width, dst.height, dx0, dy0, xsize, ysize) else {
        return Ok(());
    };
    let ch = dst.mode.channels();

    match mask {
        None => {
            for row in 0..c.h {
                let row_off = ((c.dy + row) * dst.width + c.dx) * ch;
                for col in 0..c.w {
                    let off = row_off + col * ch;
                    dst.data[off..off + ch].copy_from_slice(&color[..ch]);
                }
            }
        }
        Some(m) if m.mode == InkMode::L => {
            let is_rgba = dst.mode == InkMode::Rgba;
            for row in 0..c.h {
                let row_off = ((c.dy + row) * dst.width + c.dx) * ch;
                let mask_row = (c.sy + row) * m.width + c.sx;
                for col in 0..c.w {
                    let a = m.data[mask_row + col];
                    let off = row_off + col * ch;
                    let out_a = if is_rgba { dst.data[off + 3] } else { 255 };
                    for (b, &ink) in color.iter().enumerate().take(ch) {
                        // Fill.c:fill_mask_L computes `255 - (255 - cm) * (1 - (255 - out[3]) / 255)`
                        // for non-alpha channels when cm != 0, which equals 255 iff out[3] == 0.
                        let cm = if is_rgba && b != 3 && a != 0 && out_a == 0 {
                            255
                        } else {
                            a
                        };
                        dst.data[off + b] = blend_u8(cm, dst.data[off + b], ink);
                    }
                }
            }
        }
        Some(m) if m.mode == InkMode::Rgba => {
            for row in 0..c.h {
                let row_off = ((c.dy + row) * dst.width + c.dx) * ch;
                let mask_row = ((c.sy + row) * m.width + c.sx) * 4;
                for col in 0..c.w {
                    let a = m.data[mask_row + col * 4 + 3];
                    let off = row_off + col * ch;
                    for (b, &ink) in color.iter().enumerate().take(ch) {
                        dst.data[off + b] = blend_u8(a, dst.data[off + b], ink);
                    }
                }
            }
        }
        Some(_) => return Err(bad_mismatch("bad transparency mask")),
    }
    Ok(())
}
