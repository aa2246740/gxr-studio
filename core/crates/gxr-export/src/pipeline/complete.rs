//! `export_complete`: the inked canvas → the file bytes and the result JSON.
//!
//! Port of the tail of `finish/serve.py:export_finish` (1.0.1): `_fit` to the long edge,
//! `write_rgb_with_exif` once, then, for HDR, `hdr_export.finish`; then the result fields and the
//! `_gxr` / `_hdr` name.

use gxr_frame::{Val, parse_json};
use gxr_jpeg::ExifSpec;
use gxr_raw::{CoreError, Json};

use super::encode::{Rgb, fit, write_rgb_with_exif};
use super::hdr::{StampMask, finish_hdr};
use super::result::{ExportFail, Labels, result_json, result_labels, validate_options};

/// Largest pixel count any image input may have (`gxr-raw`'s shared limit, 2**32 − 1 elements).
const MAX_ELEMENTS: u64 = u32::MAX as u64;

fn bad_state(what: &str) -> CoreError {
    CoreError::new("bad_state", format!("导出状态无效：{what}"))
}

/// The small JSON `export_prepare` hands to `export_complete` (besides the HDR layer).
#[derive(Clone, Debug, PartialEq)]
pub struct State {
    pub name: String,
    pub quality: i64,
    pub long_edge: Option<i64>,
    pub labels: Labels,
    /// `None` when HDR was not asked for.
    pub hdr: Option<Val>,
}

/// A JSON integer as f64; a JSON float becomes NaN so `validate_options` rejects it, as
/// Python's `isinstance(x, int)` rejects `95.0`.
fn num(v: Option<&Val>) -> Option<f64> {
    match v {
        Some(Val::Int(n)) => Some(*n as f64),
        Some(Val::Float(_)) => Some(f64::NAN),
        _ => None,
    }
}

impl State {
    pub fn parse(text: &[u8]) -> Result<Self, ExportFail> {
        let v = parse_json(text)?;
        if v.as_object().is_none() {
            return Err(bad_state("要是对象").into());
        }
        let name = match v.get("name") {
            Some(Val::Str(s)) => s.clone(),
            _ => return Err(bad_state("name").into()),
        };
        let quality = num(v.get("quality")).ok_or_else(|| bad_state("quality"))?;
        let long_edge = match v.get("long_edge") {
            None | Some(Val::Null) => None,
            other => Some(num(other).ok_or_else(|| bad_state("long_edge"))?),
        };
        let (quality, long_edge) = validate_options(quality, long_edge)?;
        let labels = Labels::from_val(v.get("labels").ok_or_else(|| bad_state("labels"))?)?;
        let hdr = match v.get("hdr") {
            None | Some(Val::Null) => None,
            Some(h) => Some(h.clone()),
        };
        Ok(Self {
            name,
            quality,
            long_edge,
            labels,
            hdr,
        })
    }
}

/// Check `w × h × 3 == len` in u64 against the shared limit before touching the bytes.
pub fn checked_rgb(data: &[u8], w: u64, h: u64) -> Result<(usize, usize), CoreError> {
    let bad = || CoreError::new("bad_image", "图像尺寸和字节数不符");
    if w == 0 || h == 0 {
        return Err(CoreError::new("bad_image", "图像尺寸必须大于 0"));
    }
    let px = w.checked_mul(h).ok_or_else(bad)?;
    let n = px.checked_mul(3).ok_or_else(bad)?;
    if px > MAX_ELEMENTS || n > MAX_ELEMENTS {
        return Err(CoreError::new("too_big", "图像太大"));
    }
    if u64::try_from(data.len()).map_err(|_| bad())? != n {
        return Err(bad());
    }
    Ok((
        usize::try_from(w).map_err(|_| bad())?,
        usize::try_from(h).map_err(|_| bad())?,
    ))
}

/// The finished file and `ExportResult` as JSON.
#[derive(Clone, Debug, PartialEq)]
pub struct Completed {
    pub file: Vec<u8>,
    pub result: Json,
}

/// HDR inputs `export_complete` needs when `state.hdr` is set: the track's float32 layer from
/// `export_prepare` and the browser's stamp mask (absent when no stamp is on the photo).
pub struct HdrInputs<'a> {
    pub layer: &'a [f32],
    pub mask: Option<StampMask<'a>>,
}

/// `export_complete`: the inked canvas, the state and the source EXIF (export-meta's `base` /
/// `sub`, without 270) → `_fit`, one encode, then, when `state.hdr` is set,
/// `hdr_export.finish` → the file and the result.
pub fn complete_export(
    canvas: &[u8],
    w: u64,
    h: u64,
    state: &State,
    source: &ExifSpec,
    hdr_in: Option<HdrInputs<'_>>,
) -> Result<(Completed, Rgb), ExportFail> {
    let (wu, hu) = checked_rgb(canvas, w, h)?;
    let mut data = Vec::new();
    data.try_reserve_exact(canvas.len())
        .map_err(|_| CoreError::new("too_big", "图像太大"))?;
    data.extend_from_slice(canvas);
    let rgb = Rgb {
        width: wu,
        height: hu,
        data,
    };
    let rgb = fit(rgb, state.long_edge)?;
    let note = state.labels.note().to_string();
    let payload = write_rgb_with_exif(&rgb, source, &note, state.quality)?;
    let rl = result_labels(&state.labels);
    let (file, hdr) = match &state.hdr {
        None => (payload, None),
        Some(h) => {
            let (layer, mask) = match hdr_in {
                Some(i) => (i.layer, i.mask),
                None => (&[][..], None),
            };
            let (bytes, outcome) = finish_hdr(h, layer, mask, payload, &rgb)?;
            (bytes, Some(outcome))
        }
    };
    let result = result_json(
        &state.name,
        rgb.width,
        rgb.height,
        file.len(),
        &rl,
        hdr.as_ref(),
    );
    Ok((Completed { file, result }, rgb))
}
