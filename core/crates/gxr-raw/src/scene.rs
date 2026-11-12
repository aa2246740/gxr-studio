//! Port of gxr_studio/lut/scene.py lines 1–106: `ANCHOR`, `SceneError`, `SceneColour`,
//! `scene_colour`.

use crate::colorimetry::{ColourTags, DngColour, white_from_neutral};
use crate::dng::Dng;
use crate::error::CoreError;
use crate::linalg::{Mat3, matvec};
use crate::pyrepr::py_int;
use crate::tiff::{Ifd, Tiff, val};

/// Port of gxr_studio/lut/scene.py:26 ANCHOR (`u_m`, `f`): the fixed mid-grey anchor.
pub const ANCHOR_U_M: f64 = 0.10250885;
/// Port of gxr_studio/lut/scene.py:26 ANCHOR (`f`).
pub const ANCHOR_F: f64 = -0.0022470695785486096;

/// Port of gxr_studio/lut/scene.py:32 SceneError. As a `CoreError` its kind is `"scene"`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SceneError {
    /// `no_neutral`, `bad_matrix`, `no_matrix` or `bad_colour`.
    pub code: &'static str,
    pub message: &'static str,
}

impl From<SceneError> for CoreError {
    fn from(e: SceneError) -> Self {
        CoreError::new("scene", e.message)
    }
}

const NO_NEUTRAL: SceneError = SceneError {
    code: "no_neutral",
    message: "这张 RAW 没有白平衡信息，不能用来换算 Log 调色文件",
};
const BAD_MATRIX: SceneError = SceneError {
    code: "bad_matrix",
    message: "这张 RAW 的色彩矩阵无效，不能用来换算 Log 调色文件",
};
const NO_MATRIX: SceneError = SceneError {
    code: "no_matrix",
    message: "这张 RAW 没有色彩矩阵，不能用来换算 Log 调色文件",
};
const BAD_COLOUR: SceneError = SceneError {
    code: "bad_colour",
    message: "这张 RAW 的颜色换算出现无效值，不能用来换算 Log 调色文件",
};

/// Port of gxr_studio/lut/scene.py:39 SceneColour: AsShotNeutral and camera → XYZ(D65).
#[derive(Clone, Debug, PartialEq)]
pub struct SceneColour {
    pub neutral: [f64; 3],
    pub matrix: Mat3,
}

impl SceneColour {
    /// Port of gxr_studio/lut/scene.py:45 SceneColour.xyz for one code triple.
    pub fn xyz(&self, codes: &[f64; 3]) -> Result<[f64; 3], SceneError> {
        let (u, f) = (ANCHOR_U_M, ANCHOR_F);
        let white = matvec(&self.matrix, &self.neutral);
        let scale = 0.18 / ((u - f) * white[1]);
        let raw: [f64; 3] =
            std::array::from_fn(|i| (codes[i] / crate::codes::CODE_MAX - f) * self.neutral[i]);
        let out = matvec(&self.matrix, &raw).map(|v| v * scale);
        if out.iter().all(|v| v.is_finite()) {
            Ok(out)
        } else {
            Err(BAD_COLOUR)
        }
    }
}

/// Why `scene_colour` failed: a `SceneError`, or something Python would let escape.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SceneFailure {
    Scene(SceneError),
    Unexpected(CoreError),
}

/// `mat(tag)` inside scene_colour: absent → None; wrong size or non-finite → bad_matrix.
fn mat(root: &Ifd, tag: u16) -> Result<Option<Mat3>, SceneFailure> {
    let Some(v) = val(root, tag) else {
        return Ok(None);
    };
    let m = v.as_f64().map_err(|e| {
        SceneFailure::Unexpected(CoreError::new(
            "unexpected",
            format!("{}: {}", e.type_name(), e.text()),
        ))
    })?;
    if m.len() != 9 || !m.iter().all(|x| x.is_finite()) {
        return Err(SceneFailure::Scene(BAD_MATRIX));
    }
    Ok(Some(std::array::from_fn(|i| {
        std::array::from_fn(|j| m[3 * i + j])
    })))
}

fn illuminant(root: &Ifd, tag: u16) -> Result<i64, SceneFailure> {
    let first = match val(root, tag) {
        Some(v) => v.first(),
        None => Ok(crate::pyrepr::Scalar::Int(0)),
    };
    first.and_then(|s| py_int(&s)).map_err(|e| {
        SceneFailure::Unexpected(CoreError::new(
            "unexpected",
            format!("{}: {}", e.type_name(), e.text()),
        ))
    })
}

/// Port of gxr_studio/lut/scene.py:57 scene_colour.
pub fn scene_colour(dng: &Dng<'_>) -> Result<SceneColour, SceneFailure> {
    let neutral = match &dng.as_shot_neutral {
        Some(n) if n.iter().all(|v| v.is_finite()) && !n.iter().any(|&v| v <= 0.0) => n.clone(),
        _ => return Err(SceneFailure::Scene(NO_NEUTRAL)),
    };
    let unexpected = |e: crate::error::PyErr| {
        SceneFailure::Unexpected(CoreError::new(
            "unexpected",
            format!("{}: {}", e.type_name(), e.text()),
        ))
    };
    let t = Tiff::new(dng.data).map_err(unexpected)?;
    let root = t.ifd(i64::from(t.root), None).map_err(unexpected)?;
    let tags = ColourTags {
        color_matrix: [mat(&root, 50721)?, mat(&root, 50722)?],
        camera_calibration: [mat(&root, 50723)?, mat(&root, 50724)?],
        forward_matrix: [mat(&root, 50964)?, mat(&root, 50965)?],
        illuminant: [illuminant(&root, 50778)?, illuminant(&root, 50779)?],
        analog_balance: match val(&root, 50727) {
            Some(v) => v.as_f64().map_err(unexpected)?,
            None => vec![1.0, 1.0, 1.0],
        },
    };
    if tags.color_matrix.iter().all(Option::is_none) {
        return Err(SceneFailure::Scene(NO_MATRIX));
    }
    let bad = SceneFailure::Scene(BAD_MATRIX);
    let solved = (|| {
        let col = DngColour::new(&tags)?;
        let white = white_from_neutral(&col, &neutral)?;
        let matrix = col.camera_to_xyz(&white)?;
        Ok::<_, crate::colorimetry::ColourError>((white, matrix))
    })();
    let Ok((white, matrix)) = solved else {
        return Err(bad);
    };
    let neutral: [f64; 3] = match neutral.as_slice() {
        [a, b, c] => [*a, *b, *c],
        _ => return Err(bad),
    };
    let white_y = matvec(&matrix, &neutral)[1];
    if !(white.iter().all(|v| v.is_finite()) && matrix.iter().flatten().all(|v| v.is_finite())) {
        return Err(bad);
    }
    if !(white_y.is_finite() && white_y > 0.0) {
        return Err(bad);
    }
    Ok(SceneColour { neutral, matrix })
}
