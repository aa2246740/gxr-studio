//! Glue over `gxr_raw::scene` and `gxr_raw::colorimetry` for `gxr_studio/lut/scene.py:32-103`.
//!
//! Unlike `gxr_raw::gpu_source::scene_affine`, the LUT `scene_colour` surface maps
//! `SceneError.code` (`no_neutral`, `no_matrix`, `bad_matrix`, `bad_colour`) directly to
//! `CoreError.kind`.

use gxr_raw::Dng;
use gxr_raw::error::CoreError;
pub use gxr_raw::gpu_source::{SceneAffine, affine_of, scene_affine};
pub use gxr_raw::scene::{ANCHOR_F, ANCHOR_U_M, SceneColour, SceneError, SceneFailure};

use crate::bounds::try_vec_cap;
use crate::photo::apply_u8;
use crate::spaces::Target;

/// Convert `SceneError` preserving `e.code` as `CoreError.kind`.
pub fn scene_error_to_core(err: SceneError) -> CoreError {
    CoreError::new(err.code, err.message)
}

/// Port of gxr_studio/lut/scene.py:57 scene_colour (exact `SceneError.code` as `CoreError.kind`).
pub fn scene_colour(dng: &Dng<'_>) -> Result<SceneColour, CoreError> {
    match gxr_raw::scene::scene_colour(dng) {
        Ok(col) => Ok(col),
        Err(SceneFailure::Scene(e)) => Err(scene_error_to_core(e)),
        Err(SceneFailure::Unexpected(e)) => Err(e),
    }
}

/// Port of gxr_studio/lut/scene.py:45 SceneColour.xyz over a slice of `(n, 3)` sensor codes.
pub fn scene_xyz(colour: &SceneColour, codes: &[[f64; 3]]) -> Result<Vec<[f64; 3]>, CoreError> {
    let mut out = try_vec_cap(codes.len())?;
    for px in codes {
        out.push(colour.xyz(px).map_err(scene_error_to_core)?);
    }
    Ok(out)
}

/// Port of gxr_studio/lut/photo.py:42-48 (log LUT apply using `SceneColour.xyz` + `Target.render`).
pub fn apply_log_with_dng(
    target: &Target<'_>,
    dng: &Dng<'_>,
    stock_rgb: &[[f64; 3]],
    codes: &[[f64; 3]],
) -> Result<Vec<[u8; 3]>, CoreError> {
    let colour = scene_colour(dng)?;
    let xyz = scene_xyz(&colour, codes)?;
    apply_u8(target, stock_rgb, Some(&xyz))
}
