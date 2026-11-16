//! Owned 8-bit image buffer (`L`, `RGB`, `RGBA`) matching Pillow 12.2.0 pixel plumbing
//! (`Crop.c`, `Convert.c`, `Bands.c`, `Point.c`, `AlphaComposite.c`, `Geometry.c`) and NumPy 2.4.3
//! `float32` `pairwise_sum` (`loops_utils.h.src`).

use gxr_raw::CoreError;

use crate::geometry::bounds::{checked_mul_u64, fit_count, too_big, try_vec_u8};
use crate::ink::paste::{paste_image_at, shift_for_div255};

/// Supported 8-bit Pillow modes for `ink`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InkMode {
    L,
    Rgb,
    Rgba,
}

impl InkMode {
    pub fn parse(name: &str) -> Result<Self, CoreError> {
        match name {
            "L" => Ok(Self::L),
            "RGB" => Ok(Self::Rgb),
            "RGBA" => Ok(Self::Rgba),
            _ => Err(CoreError::new("value_error", "unsupported image mode")),
        }
    }

    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::L => "L",
            Self::Rgb => "RGB",
            Self::Rgba => "RGBA",
        }
    }

    #[must_use]
    pub const fn channels(self) -> usize {
        match self {
            Self::L => 1,
            Self::Rgb => 3,
            Self::Rgba => 4,
        }
    }
}

/// Owned 8-bit image buffer in row-major C order (`H x W x C`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InkImage {
    pub width: usize,
    pub height: usize,
    pub mode: InkMode,
    pub data: Vec<u8>,
}

pub(crate) fn bad_dims_zero() -> CoreError {
    CoreError::new("value_error", "height and width must be > 0")
}

pub(crate) fn bad_mismatch(msg: &str) -> CoreError {
    CoreError::new("value_error", msg)
}

pub(crate) fn checked_image_dims(
    w: u64,
    h: u64,
    mode: InkMode,
) -> Result<(usize, usize, usize), CoreError> {
    let _ = fit_count(w, 1)?;
    let _ = fit_count(h, 1)?;
    let pixels = checked_mul_u64(w, h)?;
    let _ = fit_count(pixels, 1)?;
    let bytes = checked_mul_u64(pixels, mode.channels() as u64)?;
    let byte_len = fit_count(bytes, 1)?;
    let wu = usize::try_from(w).map_err(|_| too_big())?;
    let hu = usize::try_from(h).map_err(|_| too_big())?;
    Ok((wu, hu, byte_len))
}

impl InkImage {
    /// Construct an [`InkImage`] from caller-supplied pixels, rejecting zero dimensions and
    /// enforcing the shared `MAX_COUNT` / `MAX_BYTES` bounds before copying.
    pub fn from_bytes(mode: InkMode, w: u64, h: u64, data: &[u8]) -> Result<Self, CoreError> {
        if w == 0 || h == 0 {
            return Err(bad_dims_zero());
        }
        let (wu, hu, expected) = checked_image_dims(w, h, mode)?;
        if data.len() != expected {
            return Err(bad_mismatch("input length does not match image dimensions"));
        }
        let mut buf = try_vec_u8(expected as u64, 0)?;
        buf.copy_from_slice(data);
        Ok(Self {
            width: wu,
            height: hu,
            mode,
            data: buf,
        })
    }

    /// Construct a new [`InkImage`] filled with a constant pixel value (`[l, 0, 0, 0]` for `L`,
    /// `[r, g, b, 0]` for `RGB`, `[r, g, b, a]` for `RGBA`). Allows 0-width / 0-height for crop
    /// results and `Image.new`.
    pub fn new_filled(mode: InkMode, w: u64, h: u64, fill: [u8; 4]) -> Result<Self, CoreError> {
        let (wu, hu, byte_len) = checked_image_dims(w, h, mode)?;
        let mut data = match mode {
            InkMode::L => try_vec_u8(byte_len as u64, fill[0])?,
            InkMode::Rgb => {
                if fill[0] == 0 && fill[1] == 0 && fill[2] == 0 {
                    try_vec_u8(byte_len as u64, 0)?
                } else {
                    let mut buf = try_vec_u8(byte_len as u64, 0)?;
                    for px in buf.chunks_exact_mut(3) {
                        px[0] = fill[0];
                        px[1] = fill[1];
                        px[2] = fill[2];
                    }
                    buf
                }
            }
            InkMode::Rgba => {
                if fill == [0, 0, 0, 0] {
                    try_vec_u8(byte_len as u64, 0)?
                } else {
                    let mut buf = try_vec_u8(byte_len as u64, 0)?;
                    for px in buf.chunks_exact_mut(4) {
                        px[0] = fill[0];
                        px[1] = fill[1];
                        px[2] = fill[2];
                        px[3] = fill[3];
                    }
                    buf
                }
            }
        };
        if wu == 0 || hu == 0 {
            data.clear();
        }
        Ok(Self {
            width: wu,
            height: hu,
            mode,
            data,
        })
    }

    #[must_use]
    pub fn size_u64(&self) -> (u64, u64) {
        (self.width as u64, self.height as u64)
    }

    /// Port of Pillow `Image.point` for 8-bit images. Accepts either a 256-entry table (applied to
    /// every channel) or a `256 * channels`-entry per-band table.
    pub fn point(&self, lut: &[u8]) -> Result<Self, CoreError> {
        let ch = self.mode.channels();
        let per_band = if lut.len() == 256 {
            false
        } else if lut.len() == 256 * ch {
            true
        } else {
            return Err(bad_mismatch("wrong number of lut entries"));
        };
        let mut out = try_vec_u8(self.data.len() as u64, 0)?;
        if !per_band || ch == 1 {
            for (dst, &src) in out.iter_mut().zip(self.data.iter()) {
                *dst = lut[usize::from(src)];
            }
        } else {
            for (dst_px, src_px) in out.chunks_exact_mut(ch).zip(self.data.chunks_exact(ch)) {
                for b in 0..ch {
                    dst_px[b] = lut[b * 256 + usize::from(src_px[b])];
                }
            }
        }
        Ok(Self {
            width: self.width,
            height: self.height,
            mode: self.mode,
            data: out,
        })
    }

    /// Build a 256-entry `point` LUT matching Python `[CLIP8(int(v * scale)) for v in range(256)]`.
    pub fn point_table_mul(scale: f64) -> Result<[u8; 256], CoreError> {
        if !scale.is_finite() {
            return Err(bad_mismatch("point scale must be finite"));
        }
        let mut table = [0_u8; 256];
        for (i, slot) in table.iter_mut().enumerate() {
            let prod = (i as f64) * scale;
            let t = prod.trunc();
            *slot = if t <= 0.0 {
                0
            } else if t >= 255.0 {
                255
            } else {
                t as u8
            };
        }
        Ok(table)
    }

    /// Port of Pillow `Convert.c` conversions among `L`, `RGB`, and `RGBA`.
    pub fn convert(&self, target: InkMode) -> Result<Self, CoreError> {
        if self.mode == target {
            let mut out = try_vec_u8(self.data.len() as u64, 0)?;
            out.copy_from_slice(&self.data);
            return Ok(Self {
                width: self.width,
                height: self.height,
                mode: target,
                data: out,
            });
        }
        let (_, _, out_bytes) = checked_image_dims(self.width as u64, self.height as u64, target)?;
        let mut out = try_vec_u8(out_bytes as u64, 0)?;
        match (self.mode, target) {
            (InkMode::Rgb, InkMode::L) => {
                for (dst, px) in out.iter_mut().zip(self.data.chunks_exact(3)) {
                    *dst = rgb_to_l(px[0], px[1], px[2]);
                }
            }
            (InkMode::Rgba, InkMode::L) => {
                // Pillow's Convert.c maps RGBA -> L via rgb2l (ignoring alpha).
                for (dst, px) in out.iter_mut().zip(self.data.chunks_exact(4)) {
                    *dst = rgb_to_l(px[0], px[1], px[2]);
                }
            }
            (InkMode::Rgba, InkMode::Rgb) => {
                for (dst_px, src_px) in out.chunks_exact_mut(3).zip(self.data.chunks_exact(4)) {
                    dst_px[0] = src_px[0];
                    dst_px[1] = src_px[1];
                    dst_px[2] = src_px[2];
                }
            }
            (InkMode::Rgb, InkMode::Rgba) => {
                for (dst_px, src_px) in out.chunks_exact_mut(4).zip(self.data.chunks_exact(3)) {
                    dst_px[0] = src_px[0];
                    dst_px[1] = src_px[1];
                    dst_px[2] = src_px[2];
                    dst_px[3] = 255;
                }
            }
            (InkMode::L, InkMode::Rgb) => {
                for (dst_px, &v) in out.chunks_exact_mut(3).zip(self.data.iter()) {
                    dst_px[0] = v;
                    dst_px[1] = v;
                    dst_px[2] = v;
                }
            }
            (InkMode::L, InkMode::Rgba) => {
                for (dst_px, &v) in out.chunks_exact_mut(4).zip(self.data.iter()) {
                    dst_px[0] = v;
                    dst_px[1] = v;
                    dst_px[2] = v;
                    dst_px[3] = 255;
                }
            }
            _ => unreachable!("handled by self.mode == target"),
        }
        Ok(Self {
            width: self.width,
            height: self.height,
            mode: target,
            data: out,
        })
    }

    /// Port of Pillow `Image.getchannel("A")` for `RGBA`.
    pub fn get_alpha(&self) -> Result<Self, CoreError> {
        if self.mode != InkMode::Rgba {
            return Err(bad_mismatch("The image has no channel \"A\""));
        }
        let (_, _, out_bytes) =
            checked_image_dims(self.width as u64, self.height as u64, InkMode::L)?;
        let mut out = try_vec_u8(out_bytes as u64, 0)?;
        for (dst, px) in out.iter_mut().zip(self.data.chunks_exact(4)) {
            *dst = px[3];
        }
        Ok(Self {
            width: self.width,
            height: self.height,
            mode: InkMode::L,
            data: out,
        })
    }

    /// Port of Pillow `Image.putalpha(alpha)` when `alpha` is an `"L"` image.
    /// Promotes `RGB` to `RGBA` automatically, matching Pillow `Image.py:2065`.
    pub fn put_alpha_image(&mut self, alpha: &Self) -> Result<(), CoreError> {
        if alpha.mode != InkMode::L {
            return Err(bad_mismatch("illegal image mode"));
        }
        if alpha.width != self.width || alpha.height != self.height {
            return Err(bad_mismatch("images do not match"));
        }
        if self.mode == InkMode::Rgb {
            *self = self.convert(InkMode::Rgba)?;
        } else if self.mode != InkMode::Rgba {
            return Err(bad_mismatch("illegal image mode"));
        }
        for (px, &a) in self.data.chunks_exact_mut(4).zip(alpha.data.iter()) {
            px[3] = a;
        }
        Ok(())
    }

    /// Port of Pillow `Image.putalpha(alpha)` when `alpha` is an integer constant.
    pub fn put_alpha_const(&mut self, alpha: u8) -> Result<(), CoreError> {
        if self.mode == InkMode::Rgb {
            *self = self.convert(InkMode::Rgba)?;
        } else if self.mode != InkMode::Rgba {
            return Err(bad_mismatch("illegal image mode"));
        }
        for px in self.data.chunks_exact_mut(4) {
            px[3] = alpha;
        }
        Ok(())
    }

    /// Port of Pillow `src/libImaging/AlphaComposite.c:ImagingAlphaComposite`.
    /// Composites `over` (`imSrc`) over `self` (`imDst`) for `RGBA`.
    pub fn alpha_composite(&self, over: &Self) -> Result<Self, CoreError> {
        if self.mode != InkMode::Rgba || over.mode != InkMode::Rgba {
            return Err(bad_mismatch("images must be RGBA"));
        }
        if self.width != over.width || self.height != over.height {
            return Err(bad_mismatch("images do not match"));
        }
        let mut out = try_vec_u8(self.data.len() as u64, 0)?;
        for ((dst_px, src_px), out_px) in self
            .data
            .chunks_exact(4)
            .zip(over.data.chunks_exact(4))
            .zip(out.chunks_exact_mut(4))
        {
            let sa = u32::from(src_px[3]);
            if sa == 0 {
                out_px.copy_from_slice(dst_px);
            } else {
                let da = u32::from(dst_px[3]);
                let blend = da * (255 - sa);
                let outa255 = sa * 255 + blend;
                let coef1 = (sa * 255 * 255 * 128) / outa255;
                let coef2 = 255 * 128 - coef1;

                let tmpr = u32::from(src_px[0]) * coef1 + u32::from(dst_px[0]) * coef2;
                let tmpg = u32::from(src_px[1]) * coef1 + u32::from(dst_px[1]) * coef2;
                let tmpb = u32::from(src_px[2]) * coef1 + u32::from(dst_px[2]) * coef2;

                out_px[0] = (shift_for_div255(tmpr + (0x80 << 7)) >> 7) as u8;
                out_px[1] = (shift_for_div255(tmpg + (0x80 << 7)) >> 7) as u8;
                out_px[2] = (shift_for_div255(tmpb + (0x80 << 7)) >> 7) as u8;
                out_px[3] = shift_for_div255(outa255 + 0x80) as u8;
            }
        }
        Ok(Self {
            width: self.width,
            height: self.height,
            mode: InkMode::Rgba,
            data: out,
        })
    }

    /// Port of Pillow `rotate(90, expand=True)` (`Geometry.c:ImagingRotate90`).
    pub fn rotate_90(&self) -> Result<Self, CoreError> {
        let out_w = self.height;
        let out_h = self.width;
        let ch = self.mode.channels();
        let mut out = try_vec_u8(self.data.len() as u64, 0)?;
        for y in 0..self.height {
            for x in 0..self.width {
                let dst_y = self.width - 1 - x;
                let dst_x = y;
                let src_idx = (y * self.width + x) * ch;
                let dst_idx = (dst_y * out_w + dst_x) * ch;
                out[dst_idx..dst_idx + ch].copy_from_slice(&self.data[src_idx..src_idx + ch]);
            }
        }
        Ok(Self {
            width: out_w,
            height: out_h,
            mode: self.mode,
            data: out,
        })
    }

    /// Port of Pillow `Crop.c:ImagingCrop` for integer box `(sx0, sy0, sx1, sy1)`.
    /// Out-of-range regions come back as zeros.
    pub fn crop(&self, sx0: i64, sy0: i64, sx1: i64, sy1: i64) -> Result<Self, CoreError> {
        if sx1 < sx0 {
            return Err(bad_mismatch("Coordinate 'right' is less than 'left'"));
        }
        if sy1 < sy0 {
            return Err(bad_mismatch("Coordinate 'lower' is less than 'upper'"));
        }
        let xsize = (sx1 - sx0) as u64;
        let ysize = (sy1 - sy0) as u64;
        let mut out = Self::new_filled(self.mode, xsize, ysize, [0, 0, 0, 0])?;
        if out.width == 0 || out.height == 0 || self.width == 0 || self.height == 0 {
            return Ok(out);
        }
        let dx0 = sx0.checked_neg().ok_or_else(too_big)?;
        let dy0 = sy0.checked_neg().ok_or_else(too_big)?;
        paste_image_at(&mut out, self, dx0, dy0)?;
        Ok(out)
    }

    /// Port of Pillow `Image.crop((x0, y0, x1, y1))` accepting float bounds.
    pub fn crop_f64(&self, box_f64: [f64; 4]) -> Result<Self, CoreError> {
        if box_f64.iter().any(|v| !v.is_finite()) {
            return Err(bad_mismatch("crop box must be finite"));
        }
        if box_f64[2] < box_f64[0] {
            return Err(bad_mismatch("Coordinate 'right' is less than 'left'"));
        }
        if box_f64[3] < box_f64[1] {
            return Err(bad_mismatch("Coordinate 'lower' is less than 'upper'"));
        }
        let x0 = round_even_i64(box_f64[0])?;
        let y0 = round_even_i64(box_f64[1])?;
        let x1 = round_even_i64(box_f64[2])?;
        let y1 = round_even_i64(box_f64[3])?;
        if x1 < x0 || y1 < y0 {
            return Err(bad_mismatch("crop box order inverted after rounding"));
        }
        self.crop(x0, y0, x1, y1)
    }
}

#[inline]
pub(crate) fn rgb_to_l(r: u8, g: u8, b: u8) -> u8 {
    ((u32::from(r) * 19_595 + u32::from(g) * 38_470 + u32::from(b) * 7_471 + 0x8000) >> 16) as u8
}

#[inline]
fn round_even_i64(v: f64) -> Result<i64, CoreError> {
    const I64_MIN_F64: f64 = -9_223_372_036_854_775_808.0;
    const I64_UPPER_F64: f64 = 9_223_372_036_854_775_808.0;
    let r = v.round_ties_even();
    if !r.is_finite() || !(I64_MIN_F64..I64_UPPER_F64).contains(&r) {
        return Err(too_big());
    }
    Ok(r as i64)
}

fn pairwise_sum_f32_inner(a: &[f32]) -> f32 {
    let n = a.len();
    if n < 8 {
        let mut res = -0.0_f32;
        for &v in a {
            res += v;
        }
        res
    } else if n <= 128 {
        let mut r = [a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]];
        let mut i = 8;
        let limit = n - (n % 8);
        while i < limit {
            for (j, slot) in r.iter_mut().enumerate() {
                *slot += a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise_sum_f32_inner(&a[..n2]) + pairwise_sum_f32_inner(&a[n2..])
    }
}

/// Port of NumPy 2.4.3 `FLOAT_pairwise_sum` (`loops_utils.h.src`) inside `add.reduce`.
#[must_use]
pub fn pairwise_sum_f32(a: &[f32]) -> f32 {
    0.0_f32 + pairwise_sum_f32_inner(a)
}

/// Port of NumPy 2.4.3 `np.asarray(..., dtype=np.float32).mean()` (scalar `float32`).
#[must_use]
pub fn mean_f32(a: &[f32]) -> f32 {
    if a.is_empty() {
        f32::NAN
    } else {
        let s = 0.0_f32 + pairwise_sum_f32_inner(a);
        (f64::from(s) / (a.len() as f64)) as f32
    }
}

fn pairwise_sum_u8_inner(a: &[u8]) -> f32 {
    let n = a.len();
    if n < 8 {
        let mut res = -0.0_f32;
        for &v in a {
            res += f32::from(v);
        }
        res
    } else if n <= 128 {
        let mut r = [
            f32::from(a[0]),
            f32::from(a[1]),
            f32::from(a[2]),
            f32::from(a[3]),
            f32::from(a[4]),
            f32::from(a[5]),
            f32::from(a[6]),
            f32::from(a[7]),
        ];
        let mut i = 8;
        let limit = n - (n % 8);
        while i < limit {
            for (j, slot) in r.iter_mut().enumerate() {
                *slot += f32::from(a[i + j]);
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += f32::from(a[i]);
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise_sum_u8_inner(&a[..n2]) + pairwise_sum_u8_inner(&a[n2..])
    }
}

/// Port of NumPy `np.asarray(u8_slice, dtype=np.float32).mean()` without allocating a `Vec<f32>`.
#[must_use]
pub fn mean_u8_as_f32(a: &[u8]) -> f32 {
    if a.is_empty() {
        f32::NAN
    } else {
        let s = pairwise_sum_u8_inner(a);
        (f64::from(s) / (a.len() as f64)) as f32
    }
}
