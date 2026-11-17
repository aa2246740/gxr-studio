//! HDR exports (wave-1 unit hdr): alignment, the uint8 gain plane with its metadata, and
//! the Ultra HDR container. Strings are the bytes `gxr-cli` prints (JSON plus the newline
//! `println!` adds). A core error is thrown as `{"error": {"kind", "message"}}`.

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_hdr as hdr;
use gxr_raw::{CoreError, error_json};

const U64_UPPER_BOUND_F64: f64 = 18_446_744_073_709_551_616.0;

fn rejected(err: CoreError) -> JsValue {
    JsValue::from_str(&error_json(&err).render())
}

fn checked_dim_u64(v: f64) -> Result<u64, CoreError> {
    if v.is_finite() && v.fract() == 0.0 && v >= 0.0 {
        if v < U64_UPPER_BOUND_F64 {
            #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
            Ok(v as u64)
        } else {
            Ok(u64::MAX)
        }
    } else {
        Err(hdr::unsupported("bad_input", "数组长度和形状对不上"))
    }
}

/// Validate caller dimensions against the shared count limit and borrowed slice length using
/// full `u64` axes so an empty plane `(2^32+1, 0)` does not narrow to `usize` on wasm32.
fn validate_plane_u64(values: &[f32], height: f64, width: f64) -> Result<(u64, u64), CoreError> {
    let h = checked_dim_u64(height)?;
    let w = checked_dim_u64(width)?;
    let n = hdr::count(&[h, w])?;
    if n != values.len() {
        return Err(hdr::unsupported("bad_input", "数组长度和形状对不上"));
    }
    Ok((h, w))
}

fn align_core(
    sdr: &[f32],
    sdr_height: f64,
    sdr_width: f64,
    raw: &[f32],
    raw_height: f64,
    raw_width: f64,
) -> Result<String, CoreError> {
    let (sh, sw) = validate_plane_u64(sdr, sdr_height, sdr_width)?;
    let (rh, rw) = validate_plane_u64(raw, raw_height, raw_width)?;
    if sh == 0 || sw == 0 || rh == 0 || rw == 0 {
        return Err(hdr::unsupported("bad_input", "无法对齐空图"));
    }
    let sdr_plane = hdr::Plane32::from_slice(sdr, sh, sw)?;
    let raw_plane = hdr::Plane32::from_slice(raw, rh, rw)?;
    let report = hdr::align_report(&sdr_plane, &raw_plane)?;
    Ok(format!("{}\n", report.render()))
}

/// `align(sdr_y, raw_y)` and `check_alignment`, as `gxr-cli hdr-align` prints them.
#[wasm_bindgen]
pub fn hdr_align(
    sdr: &[f32],
    sdr_height: f64,
    sdr_width: f64,
    raw: &[f32],
    raw_height: f64,
    raw_width: f64,
) -> Result<String, JsValue> {
    align_core(sdr, sdr_height, sdr_width, raw, raw_height, raw_width).map_err(rejected)
}

/// The gain map on the grid: uint8 codes for the gain JPEG, and the JSON report.
#[wasm_bindgen]
pub struct HdrGain {
    height: u32,
    width: u32,
    codes: Vec<u8>,
    report: String,
}

#[wasm_bindgen]
impl HdrGain {
    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    /// `GainMeta`, its fractions and the stats, as `gxr-cli hdr-gain` prints them.
    #[wasm_bindgen(getter)]
    pub fn report(&self) -> String {
        self.report.clone()
    }

    /// Row-major uint8 codes (`encode_gainmap` before the JPEG encode). Consumes the object.
    pub fn codes(self) -> Vec<u8> {
        self.codes
    }
}

fn gain_core(
    sdr: &[f32],
    raw: &[f32],
    clip: &[u8],
    height: f64,
    width: f64,
    params_json: &str,
) -> Result<HdrGain, CoreError> {
    let (h, w) = validate_plane_u64(sdr, height, width)?;
    let _ = validate_plane_u64(raw, height, width)?;
    // Same order as `gxr-cli hdr-gain`: Params are read before the clip length is checked.
    let params = if params_json.is_empty() {
        hdr::Params::default()
    } else {
        hdr::params_from_json(params_json)?
    };
    let n = hdr::count(&[h, w])?;
    if n != clip.len() {
        return Err(hdr::unsupported("bad_input", "数组长度和形状对不上"));
    }
    if h == 0 || w == 0 {
        return Err(hdr::compute_gain(&[], &[], &[], 0, 0, &params)
            .err()
            .unwrap_or_else(|| hdr::unsupported("no_curve", "可用于拟合的像素太少")));
    }
    let s = hdr::Plane32::from_slice(sdr, h, w)?;
    let r = hdr::Plane32::from_slice(raw, h, w)?;
    let mask = hdr::clip_mask(clip, s.h, s.w)?;
    let result = hdr::compute_gain(&s.v, &r.v, &mask, s.h, s.w, &params)?;
    let report = hdr::gain_report(&result)?;
    let out_h =
        u32::try_from(s.h).map_err(|_| CoreError::new("value_error", hdr::ARRAY_TOO_BIG))?;
    let out_w =
        u32::try_from(s.w).map_err(|_| CoreError::new("value_error", hdr::ARRAY_TOO_BIG))?;
    Ok(HdrGain {
        height: out_h,
        width: out_w,
        codes: hdr::quantise_gain(&result.delta, &result.meta),
        report: format!("{}\n", report.render()),
    })
}

/// Float32 SDR luma grid from `sdr_luma_grid` (`gxr-cli hdr-luma`).
#[wasm_bindgen]
pub struct HdrLuma {
    height: u32,
    width: u32,
    values: Vec<f32>,
}

#[wasm_bindgen]
impl HdrLuma {
    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.height
    }

    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.width
    }

    /// Row-major float32 luma. Consumes the object.
    pub fn bytes(self) -> Result<Vec<u8>, JsValue> {
        hdr::f32_le_bytes(&self.values).map_err(rejected)
    }
}

fn pack_luma(h: u64, w: u64, values: Vec<f32>) -> Result<HdrLuma, CoreError> {
    let height = u32::try_from(h).map_err(|_| CoreError::new("value_error", hdr::ARRAY_TOO_BIG))?;
    let width = u32::try_from(w).map_err(|_| CoreError::new("value_error", hdr::ARRAY_TOO_BIG))?;
    Ok(HdrLuma {
        height,
        width,
        values,
    })
}

/// Axes that do not fit `usize` on wasm32. Same checks as `sdr_luma_grid`, in the same order.
/// A non-empty image whose factor does not fit `usize` hits the block-mean size check
/// (`factor²` does not fit `u64`) and is `value_error` / too big, as on native.
fn luma_wide(rgb: &[u8], h: u64, w: u64, factor: u64) -> Result<HdrLuma, CoreError> {
    let n = hdr::count(&[h, w, 3])?;
    if n != rgb.len() {
        return Err(hdr::unsupported("bad_input", "RGB 长度和形状对不上"));
    }
    if factor == 0 {
        return Err(hdr::unsupported("bad_input", "division by zero"));
    }
    if h == 0 || w == 0 {
        return pack_luma(h.div_ceil(factor), w.div_ceil(factor), Vec::new());
    }
    Err(CoreError::new("value_error", hdr::ARRAY_TOO_BIG))
}

fn luma_core(rgb: &[u8], height: f64, width: f64, factor: f64) -> Result<HdrLuma, CoreError> {
    // f64, not u32: wasm-bindgen would narrow `2**32+1` before the check (UNIT-COMMON).
    let h = checked_dim_u64(height)?;
    let w = checked_dim_u64(width)?;
    let f = checked_dim_u64(factor)?;
    let (hu, wu, fu) = match (usize::try_from(h), usize::try_from(w), usize::try_from(f)) {
        (Ok(hu), Ok(wu), Ok(fu)) => (hu, wu, fu),
        _ => return luma_wide(rgb, h, w, f),
    };
    let grid = hdr::sdr_luma_grid(rgb, hu, wu, fu)?;
    pack_luma(
        u64::try_from(grid.h).map_err(|_| CoreError::new("value_error", hdr::ARRAY_TOO_BIG))?,
        u64::try_from(grid.w).map_err(|_| CoreError::new("value_error", hdr::ARRAY_TOO_BIG))?,
        grid.v,
    )
}

/// `sdr_luma_grid(rgb, factor)` as `gxr-cli hdr-luma` runs it.
/// `height`, `width` and `factor` are JS numbers. `--rgb-shape` on the CLI passes the same axes.
#[wasm_bindgen]
pub fn hdr_luma(rgb: &[u8], height: f64, width: f64, factor: f64) -> Result<HdrLuma, JsValue> {
    luma_core(rgb, height, width, factor).map_err(rejected)
}

/// `compute_gain(sdr_y, raw_y, clip, Params(**params))` and the uint8 quantisation.
/// `clip` is one byte per pixel (non-zero = clipped); `raw` is NaN outside the photo.
#[wasm_bindgen]
pub fn hdr_gain(
    sdr: &[f32],
    raw: &[f32],
    clip: &[u8],
    height: f64,
    width: f64,
    params_json: &str,
) -> Result<HdrGain, JsValue> {
    gain_core(sdr, raw, clip, height, width, params_json).map_err(rejected)
}

/// `assemble(primary, gain_jpeg, GainMeta(**meta))`: the HDR JPEG bytes.
#[wasm_bindgen]
pub fn hdr_assemble(primary: &[u8], gain_jpeg: &[u8], meta_json: &str) -> Result<Vec<u8>, JsValue> {
    let meta = hdr::meta_from_json(meta_json).map_err(rejected)?;
    hdr::assemble(primary, gain_jpeg, &meta).map_err(rejected)
}

// ---- hdr-fft ----

fn fft_core(
    values: &[f32],
    rows: f64,
    n: f64,
    inverse: bool,
    two_d: bool,
) -> Result<Vec<u8>, CoreError> {
    let rows_u64 = checked_dim_u64(rows)?;
    let n_u64 = checked_dim_u64(n)?;
    let w = hdr::count(&[n_u64, 2])?;
    let w_u64 = u64::try_from(w).unwrap_or(u64::MAX);
    let count = hdr::count(&[rows_u64, w_u64])?;
    if count != values.len() {
        return Err(hdr::unsupported("bad_input", "数组长度和形状对不上"));
    }
    // `n == 0` fails before `rows` is inspected; keep `Plane32` constructible on wasm32 when `rows > u32::MAX`.
    let rows_safe = if w == 0 { 0 } else { rows_u64 };
    let data = hdr::Plane32::from_slice(values, rows_safe, w_u64)?;
    let out = data.fft_complex(inverse, two_d, None)?;
    hdr::f32_le_bytes(&out.v)
}

/// numpy `fft` / `ifft` along the last axis (or `fft2` / `ifft2` when `two_d`) of complex64
/// data given as `(re, im)` float32 pairs, `rows × n`; the bytes `gxr-cli hdr-fft` writes.
#[wasm_bindgen]
pub fn hdr_fft(
    values: &[f32],
    rows: f64,
    n: f64,
    inverse: bool,
    two_d: bool,
) -> Result<Vec<u8>, JsValue> {
    fft_core(values, rows, n, inverse, two_d).map_err(rejected)
}

/// `_phase_shift`'s float32 plane and its peak.
#[wasm_bindgen]
pub struct HdrPhasePlane {
    report: String,
    plane: Vec<u8>,
}

#[wasm_bindgen]
impl HdrPhasePlane {
    /// `{"dy", "dx", "plane": {"dtype", "shape"}}`, as `gxr-cli hdr-phase-plane` prints it.
    #[wasm_bindgen(getter)]
    pub fn report(&self) -> String {
        self.report.clone()
    }

    /// The row-major float32 plane, little-endian. Consumes the object.
    pub fn plane(self) -> Vec<u8> {
        self.plane
    }
}

fn phase_plane_core(
    a: &[f32],
    b: &[f32],
    height: f64,
    width: f64,
) -> Result<HdrPhasePlane, CoreError> {
    let (h, w) = validate_plane_u64(a, height, width)?;
    let _ = validate_plane_u64(b, height, width)?;
    if h == 0 || w == 0 {
        return Err(hdr::unsupported("bad_input", "FFT 形状对不上"));
    }
    let pa = hdr::Plane32::from_slice(a, h, w)?;
    let pb = hdr::Plane32::from_slice(b, h, w)?;
    let (pc, (dy, dx)) = pa.phase_plane(&pb, None)?;
    let dims = [pc.h, pc.w].map(|d| i64::try_from(d).unwrap_or(i64::MAX));
    let report = gxr_raw::Json::object([
        ("dy", gxr_raw::Json::Int(dy)),
        ("dx", gxr_raw::Json::Int(dx)),
        (
            "plane",
            gxr_raw::Json::object([
                ("dtype", gxr_raw::Json::str("<f4")),
                ("shape", gxr_raw::Json::ints(&dims)),
            ]),
        ),
    ]);
    Ok(HdrPhasePlane {
        report: format!("{}\n", report.render()),
        plane: hdr::f32_le_bytes(&pc.v)?,
    })
}

/// `gxr-cli hdr-phase-plane` on two `height × width` float32 planes.
#[wasm_bindgen]
pub fn hdr_phase_plane(
    a: &[f32],
    b: &[f32],
    height: f64,
    width: f64,
) -> Result<HdrPhasePlane, JsValue> {
    phase_plane_core(a, b, height, width).map_err(rejected)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hdr_dim_validation_rejects_bad_floats_and_preserves_large_u64() {
        assert_eq!(checked_dim_u64(0.0).unwrap(), 0);
        assert_eq!(checked_dim_u64(32.0).unwrap(), 32);
        assert_eq!(checked_dim_u64(4_294_967_297.0).unwrap(), 4_294_967_297);
        assert_eq!(
            checked_dim_u64(9_007_199_254_740_992.0).unwrap(),
            9_007_199_254_740_992
        );
        assert_eq!(checked_dim_u64(U64_UPPER_BOUND_F64).unwrap(), u64::MAX);
        for bad in [-1.0, 1.5, f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let err = checked_dim_u64(bad).unwrap_err();
            assert_eq!(err.kind, "bad_input");
            assert_eq!(err.message, "数组长度和形状对不上");
        }
    }

    #[test]
    fn hdr_empty_shapes_with_large_other_axis_preserve_native_error_order() {
        for (h, w) in [
            (0.0, 0.0),
            (4_294_967_297.0, 0.0),
            (9_007_199_254_740_992.0, 0.0),
            (0.0, 4_294_967_297.0),
            (0.0, 9_007_199_254_740_992.0),
        ] {
            let err = align_core(&[], h, w, &[], h, w).unwrap_err();
            assert_eq!(
                (err.kind, err.message.as_str()),
                ("bad_input", "无法对齐空图")
            );

            let err = gain_core(&[], &[], &[], h, w, "[]").err().unwrap();
            assert_eq!(
                (err.kind, err.message.as_str()),
                ("bad_input", "需要一个 JSON 对象")
            );

            let err = gain_core(&[], &[], &[1], h, w, "").err().unwrap();
            assert_eq!(
                (err.kind, err.message.as_str()),
                ("bad_input", "数组长度和形状对不上")
            );

            let err = gain_core(&[], &[], &[], h, w, "").err().unwrap();
            assert_eq!(
                (err.kind, err.message.as_str()),
                ("no_curve", "可用于拟合的像素太少")
            );

            let err = phase_plane_core(&[], &[], h, w).err().unwrap();
            assert_eq!(
                (err.kind, err.message.as_str()),
                ("bad_input", "FFT 形状对不上")
            );
        }

        for rows in [0.0, 1.0, 4_294_967_297.0, 9_007_199_254_740_992.0] {
            for inverse in [false, true] {
                for two_d in [false, true] {
                    let err = fft_core(&[], rows, 0.0, inverse, two_d).unwrap_err();
                    assert_eq!(
                        (err.kind, err.message.as_str()),
                        (
                            "value_error",
                            "Invalid number of FFT data points (0) specified."
                        )
                    );
                }
            }
        }

        assert!(fft_core(&[], 0.0, 4.0, false, false).unwrap().is_empty());
        let err = fft_core(&[], 0.0, 4.0, false, true).unwrap_err();
        assert_eq!(
            (err.kind, err.message.as_str()),
            (
                "value_error",
                "Invalid number of FFT data points (0) specified."
            )
        );
    }
}
