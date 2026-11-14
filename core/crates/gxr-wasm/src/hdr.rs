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

/// The caller's dimensions go through the shared count limit and are compared with the slice
/// length before anything is copied (`Plane32::from_slice`, UNIT-COMMON allocation rule 1).
fn plane(values: &[f32], height: f64, width: f64) -> Result<hdr::Plane32, JsValue> {
    let h = checked_dim_u64(height).map_err(rejected)?;
    let w = checked_dim_u64(width).map_err(rejected)?;
    hdr::Plane32::from_slice(values, h, w).map_err(rejected)
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
    let report = hdr::align_report(
        &plane(sdr, sdr_height, sdr_width)?,
        &plane(raw, raw_height, raw_width)?,
    )
    .map_err(rejected)?;
    Ok(format!("{}\n", report.render()))
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
    let s = plane(sdr, height, width)?;
    let r = plane(raw, height, width)?;
    // Same order as `gxr-cli hdr-gain`: Params are read before the clip length is checked.
    let params = if params_json.is_empty() {
        hdr::Params::default()
    } else {
        hdr::params_from_json(params_json).map_err(rejected)?
    };
    let mask = hdr::clip_mask(clip, s.h, s.w).map_err(rejected)?;
    let result = hdr::compute_gain(&s.v, &r.v, &mask, s.h, s.w, &params).map_err(rejected)?;
    let report = hdr::gain_report(&result).map_err(rejected)?;
    let out_h = u32::try_from(s.h)
        .map_err(|_| rejected(CoreError::new("value_error", hdr::ARRAY_TOO_BIG)))?;
    let out_w = u32::try_from(s.w)
        .map_err(|_| rejected(CoreError::new("value_error", hdr::ARRAY_TOO_BIG)))?;
    Ok(HdrGain {
        height: out_h,
        width: out_w,
        codes: hdr::quantise_gain(&result.delta, &result.meta),
        report: format!("{}\n", report.render()),
    })
}

/// `assemble(primary, gain_jpeg, GainMeta(**meta))`: the HDR JPEG bytes.
#[wasm_bindgen]
pub fn hdr_assemble(primary: &[u8], gain_jpeg: &[u8], meta_json: &str) -> Result<Vec<u8>, JsValue> {
    let meta = hdr::meta_from_json(meta_json).map_err(rejected)?;
    hdr::assemble(primary, gain_jpeg, &meta).map_err(rejected)
}

// ---- hdr-fft ----

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
    let rows_u64 = checked_dim_u64(rows).map_err(rejected)?;
    let n_u64 = checked_dim_u64(n).map_err(rejected)?;
    let w = hdr::count(&[n_u64, 2]).map_err(rejected)?;
    let w = u64::try_from(w).unwrap_or(u64::MAX);
    let data = hdr::Plane32::from_slice(values, rows_u64, w).map_err(rejected)?;
    let out = data.fft_complex(inverse, two_d, None).map_err(rejected)?;
    hdr::f32_le_bytes(&out.v).map_err(rejected)
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

/// `gxr-cli hdr-phase-plane` on two `height × width` float32 planes.
#[wasm_bindgen]
pub fn hdr_phase_plane(
    a: &[f32],
    b: &[f32],
    height: f64,
    width: f64,
) -> Result<HdrPhasePlane, JsValue> {
    let (pa, pb) = (plane(a, height, width)?, plane(b, height, width)?);
    let (pc, (dy, dx)) = pa.phase_plane(&pb, None).map_err(rejected)?;
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
        plane: hdr::f32_le_bytes(&pc.v).map_err(rejected)?,
    })
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
}
