//! Port of gxr_studio/gpu/source.py: DNG → the RAW layout the GPU engine reads
//! (`frontend/src/shaders/bin.wgsl`), plus the JSON both `gxr-cli` and `gxr-wasm` print.

use crate::codes::{CODE_MAX, wb_gains};
use crate::dng::Dng;
use crate::error::CoreError;
use crate::json::Json;
use crate::linalg::{Mat3, Vec3, matvec};
use crate::pyrepr::py_int;
use crate::scene::{ANCHOR_F, ANCHOR_U_M, SceneColour, SceneFailure, scene_colour};

/// The `layout` dict (`RawLayout` in `frontend/src/engine.ts`).
#[derive(Clone, Debug, PartialEq)]
pub struct RawLayout {
    pub bps: i64,
    pub big_endian: bool,
    pub row_bytes: i64,
    pub strips: Vec<[i64; 2]>,
    pub crop: [i64; 4],
    pub active: [i64; 2],
    pub phase: [i64; 4],
    pub black: [f64; 4],
    pub white: f64,
    pub wb: [f64; 3],
    pub orientation: i64,
    pub width: i64,
    pub height: i64,
    /// Half-resolution colour layer (w, h) after orientation.
    pub layer: [i64; 2],
    /// Full crop size (w, h) after orientation.
    pub full: [i64; 2],
}

/// `xyz = A·codes + b` (`Scene` in `frontend/src/engine.ts`).
#[derive(Clone, Debug, PartialEq)]
pub struct SceneAffine {
    pub a: Mat3,
    pub b: Vec3,
}

/// Port of gxr_studio/gpu/source.py:20 RawSource.
#[derive(Clone, Debug, PartialEq)]
pub struct RawSource {
    pub layout: Option<RawLayout>,
    pub reason: Option<String>,
    pub scene: Option<SceneAffine>,
}

/// Port of gxr_studio/gpu/source.py:26 _black_table: the four black levels at (y%2, x%2) from
/// the ActiveArea origin, or `None` for patterns larger than 2×2 or per-row/column deltas.
fn black_table(dng: &Dng<'_>) -> Option<[f64; 4]> {
    let bp = &dng.black_pattern;
    if bp.shape.len() != 2
        || bp.rows == 0
        || bp.cols == 0
        || bp.rows > 2
        || bp.cols > 2
        || dng.black_dh.is_some()
        || dng.black_dv.is_some()
    {
        return None;
    }
    Some(std::array::from_fn(|k| {
        bp.at((k / 2) % bp.rows, (k % 2) % bp.cols)
    }))
}

/// Port of gxr_studio/gpu/source.py:34 raw_layout: `(layout, None)` or `(None, reason)`.
/// A DNG without a usable AsShotNeutral is an error, as in Python (`wb_gains` raises).
pub fn raw_layout(dng: &Dng<'_>) -> Result<(Option<RawLayout>, Option<String>), CoreError> {
    if dng.bps != 12 && dng.bps != 16 {
        return Ok((None, Some(format!("bits_per_sample_{}", dng.bps))));
    }
    if dng.bps == 12 && dng.width % 2 != 0 {
        return Ok((None, Some("odd_width_12bit".into())));
    }
    if dng.lin_table.is_some() {
        return Ok((None, Some("linearization_table".into())));
    }
    let Some(black) = black_table(dng) else {
        return Ok((None, Some("black_level_pattern".into())));
    };
    if !(1..=8).contains(&dng.orientation) {
        return Ok((None, Some("orientation".into())));
    }
    let (y0, x0, h, w) = dng.crop_window();
    if h < 2 || w < 2 {
        return Ok((None, Some("too_small".into())));
    }
    let wb = wb_gains(dng)?;
    let (offs, cnts) = dng.strip_values();
    let strips = offs
        .iter()
        .zip(cnts.iter())
        .map(|(o, n)| Ok([py_int(o)?, py_int(n)?]))
        .collect::<Result<Vec<_>, crate::error::PyErr>>()
        .map_err(|e| CoreError::new("unexpected", format!("{}: {}", e.type_name(), e.text())))?;
    let p = dng.crop_cfa_phase();
    let (w2, h2) = (w.div_euclid(2), h.div_euclid(2));
    let swap = dng.orientation >= 5;
    Ok((
        Some(RawLayout {
            bps: dng.bps,
            big_endian: dng.big_endian,
            row_bytes: dng
                .width
                .checked_mul(dng.bps)
                .and_then(|n| n.checked_add(7))
                .map(|n| n.div_euclid(8))
                .ok_or_else(crate::bounds::too_big_core)?,
            strips,
            crop: [y0, x0, h, w],
            active: [dng.active[0], dng.active[1]],
            phase: [p[0][0], p[0][1], p[1][0], p[1][1]],
            black,
            white: dng.white,
            wb,
            orientation: dng.orientation,
            width: dng.width,
            height: dng.height,
            layer: if swap { [h2, w2] } else { [w2, h2] },
            full: if swap { [h, w] } else { [w, h] },
        }),
        None,
    ))
}

/// Port of gxr_studio/gpu/source.py:77 affine_of: `SceneColour.xyz` as `A·codes + b`.
pub fn affine_of(colour: &SceneColour) -> Option<SceneAffine> {
    let (u, f) = (ANCHOR_U_M, ANCHOR_F);
    let white = matvec(&colour.matrix, &colour.neutral);
    let scale = 0.18 / ((u - f) * white[1]);
    let weighted: Mat3 = std::array::from_fn(|i| {
        std::array::from_fn(|j| colour.matrix[i][j] * colour.neutral[j] * scale)
    });
    let a: Mat3 = weighted.map(|row| row.map(|v| v / CODE_MAX));
    let b: Vec3 = std::array::from_fn(|i| -f * (weighted[i][0] + weighted[i][1] + weighted[i][2]));
    let finite = a.iter().flatten().all(|v| v.is_finite()) && b.iter().all(|v| v.is_finite());
    finite.then_some(SceneAffine { a, b })
}

/// Port of gxr_studio/gpu/source.py:90 scene_affine: codes → scene XYZ for log LUTs, or `None`
/// when the DNG has no usable colour matrix. Errors Python would not catch are passed on.
pub fn scene_affine(dng: &Dng<'_>) -> Result<Option<SceneAffine>, CoreError> {
    match scene_colour(dng) {
        Ok(colour) => Ok(affine_of(&colour)),
        Err(SceneFailure::Scene(_)) => Ok(None),
        Err(SceneFailure::Unexpected(e)) => Err(e),
    }
}

/// Port of gxr_studio/gpu/source.py:98 describe.
pub fn describe(dng: &Dng<'_>, with_scene: bool) -> Result<RawSource, CoreError> {
    let (layout, reason) = raw_layout(dng)?;
    let scene = if with_scene && layout.is_some() {
        scene_affine(dng)?
    } else {
        None
    };
    Ok(RawSource {
        layout,
        reason,
        scene,
    })
}

impl RawLayout {
    /// The layout dict as JSON, keys in Python's order.
    pub fn to_json(&self) -> Json {
        Json::object([
            ("bps", Json::Int(self.bps)),
            ("big_endian", Json::Bool(self.big_endian)),
            ("row_bytes", Json::Int(self.row_bytes)),
            (
                "strips",
                Json::Array(self.strips.iter().map(|s| Json::ints(s)).collect()),
            ),
            ("crop", Json::ints(&self.crop)),
            ("active", Json::ints(&self.active)),
            ("phase", Json::ints(&self.phase)),
            ("black", Json::floats(&self.black)),
            ("white", Json::Float(self.white)),
            ("wb", Json::floats(&self.wb)),
            ("orientation", Json::Int(self.orientation)),
            ("width", Json::Int(self.width)),
            ("height", Json::Int(self.height)),
            ("layer", Json::ints(&self.layer)),
            ("full", Json::ints(&self.full)),
        ])
    }
}

impl SceneAffine {
    /// `{"A": [[…], […], […]], "b": […]}`.
    pub fn to_json(&self) -> Json {
        Json::object([
            (
                "A",
                Json::Array(self.a.iter().map(|row| Json::floats(row)).collect()),
            ),
            ("b", Json::floats(&self.b)),
        ])
    }
}

impl RawSource {
    /// `dataclasses.asdict(describe(...))` as JSON: `{"layout", "reason", "scene"}`.
    pub fn to_json(&self) -> Json {
        Json::object([
            (
                "layout",
                Json::opt(self.layout.as_ref().map(RawLayout::to_json)),
            ),
            ("reason", Json::opt(self.reason.clone().map(Json::Str))),
            (
                "scene",
                Json::opt(self.scene.as_ref().map(SceneAffine::to_json)),
            ),
        ])
    }
}

/// `{"error": {"kind", "message"}}`, the error shape of the CLI and the WASM exports.
pub fn error_json(e: &CoreError) -> Json {
    Json::object([(
        "error",
        Json::object([
            ("kind", Json::str(e.kind)),
            ("message", Json::str(e.message.clone())),
        ]),
    )])
}

/// `describe(open_dng(data, name), with_scene=True)` as the JSON text the CLI and WASM print:
/// the `RawSource`, or `{"error": …}`. Returns whether it succeeded.
pub fn describe_json(data: &[u8], name: &str) -> (bool, String) {
    let result = Dng::open(data, name).and_then(|dng| describe(&dng, true));
    match result {
        Ok(src) => (true, src.to_json().render()),
        Err(e) => (false, error_json(&e).render()),
    }
}
