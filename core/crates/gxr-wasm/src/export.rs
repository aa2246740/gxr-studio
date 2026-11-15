//! Export exports. Owner: wave-2 export (export-meta: `export_meta_*`; export-pipeline:
//! `export_plan`, `export_prepare`, `export_complete`, `export_output_sizes`).
//!
//! Each export calls `gxr_export::pipeline::api`, the same code `gxr-cli export-run-*` runs, and
//! returns the same JSON text the CLI prints. Sizes and options arrive as `f64` and are checked
//! before they are narrowed (UNIT-COMMON "WASM number parameters").

use wasm_bindgen::prelude::{JsValue, wasm_bindgen};

use gxr_export::meta::{
    DEFAULT_NOW_YEAR, dng_preview_location, photo_meta_from_bytes, read_source_exif,
};
use gxr_export::pipeline::api::{self, Picture, PrepareArgs, SizesArgs, SourceFiles};
use gxr_export::pipeline::{ExportFail, ExportOpts};
use gxr_export::source::pack_from_index;
use gxr_frame::{Fonts, fonts_from_pack};
use gxr_raw::sha256::sha256_hex;
use gxr_raw::{Json, error_json};

fn rejected(fail: ExportFail) -> JsValue {
    JsValue::from_str(&fail.to_json().render())
}

fn fonts(index: &str, pack: &[u8]) -> Result<Fonts, JsValue> {
    if index.trim().is_empty() && pack.is_empty() {
        return Ok(Fonts::new());
    }
    fonts_from_pack(index, pack).map_err(|e| rejected(e.into()))
}

/// `export_prepare`'s canvas (background and photo, no ink) and its JSON.
#[wasm_bindgen]
pub struct ExportPrepared {
    width: f64,
    height: f64,
    canvas: Vec<u8>,
    json: String,
}

#[wasm_bindgen]
impl ExportPrepared {
    #[wasm_bindgen(getter)]
    pub fn width(&self) -> f64 {
        self.width
    }

    #[wasm_bindgen(getter)]
    pub fn height(&self) -> f64 {
        self.height
    }

    /// The canvas, row-major RGB `(height * width * 3)`.
    pub fn canvas(&self) -> Vec<u8> {
        self.canvas.clone()
    }

    /// `{"original", "prepared": {"frame", "stamp", "stamp_on", "photo", "labels", "state"}}`.
    pub fn json(&self) -> String {
        self.json.clone()
    }
}

/// The finished file and its JSON.
#[wasm_bindgen]
pub struct ExportCompleted {
    file: Vec<u8>,
    json: String,
}

#[wasm_bindgen]
impl ExportCompleted {
    pub fn file(&self) -> Vec<u8> {
        self.file.clone()
    }

    /// `{"result": {...ExportResult fields}, "sha256"}`.
    pub fn json(&self) -> String {
        self.json.clone()
    }
}

/// `serve._plan`: `{"original": true}` when the export is the untouched camera JPG, so the
/// caller knows to pass the decoded camera JPG to `export_prepare` instead of `color_image`'s
/// picture.
#[wasm_bindgen]
pub fn export_plan(shot: &str, side: &str, defaults: &str, base: &str) -> Result<String, JsValue> {
    api::plan_json(shot.as_bytes(), side.as_bytes(), defaults.as_bytes(), base)
        .map(|j| format!("{}\n", j.render()))
        .map_err(rejected)
}

/// An empty picture means "not given".
fn pic(data: &[u8], width: f64, height: f64) -> Option<Picture<'_>> {
    (!data.is_empty()).then_some(Picture {
        data,
        width,
        height,
    })
}

/// `export_prepare`. Source inputs follow export-source: empty bytes mean "not given".
/// `jpg` is the decoded camera JPG (`createImageBitmap`) and `jpg_bytes` its original file
/// (hdr-track's capture check); `preview` the decoded embedded preview. The calibration pack is
/// `calib_index` (`[[name, bytes], ...]`) over `calib_blob`. `long_edge` is `undefined` for
/// the original size.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn export_prepare(
    shot: &str,
    side: &str,
    defaults: &str,
    layout: &str,
    base: &str,
    quality: f64,
    long_edge: Option<f64>,
    hdr: bool,
    font_index: &str,
    font_pack: &[u8],
    dat: &[u8],
    entry: &[u8],
    calib_index: &str,
    calib_blob: &[u8],
    binding: &str,
    dng: &[u8],
    jpg_bytes: &[u8],
    cube: &[u8],
    space: &str,
    jpg: &[u8],
    jpg_width: f64,
    jpg_height: f64,
    preview: &[u8],
    preview_width: f64,
    preview_height: f64,
) -> Result<ExportPrepared, JsValue> {
    let fonts = fonts(font_index, font_pack)?;
    let pack = if calib_index.trim().is_empty() && calib_blob.is_empty() {
        None
    } else {
        Some(pack_from_index(calib_index.as_bytes(), calib_blob).map_err(|e| rejected(e.into()))?)
    };
    let files = SourceFiles {
        dat,
        entry,
        pack: pack.as_ref(),
        binding,
        dng,
        jpg_bytes,
        cube,
        space,
    };
    let args = PrepareArgs {
        shot: shot.as_bytes(),
        side: side.as_bytes(),
        defaults: defaults.as_bytes(),
        layout: layout.as_bytes(),
        base,
        opts: ExportOpts {
            quality,
            long_edge,
            hdr,
        },
    };
    let (canvas, json, original) = api::prepare(
        &args,
        &files,
        pic(jpg, jpg_width, jpg_height),
        pic(preview, preview_width, preview_height),
        &fonts,
    )
    .map_err(rejected)?;
    // The CLI prints the same text with `println!`.
    let json = format!(
        "{}\n",
        Json::object([("original", Json::Bool(original)), ("prepared", json)]).render()
    );
    Ok(ExportPrepared {
        width: canvas.width as f64,
        height: canvas.height as f64,
        canvas: canvas.data,
        json,
    })
}

/// `export_complete` (SDR): the inked canvas, `prepared.state` and the source EXIF JSON.
#[wasm_bindgen]
pub fn export_complete(
    canvas: &[u8],
    width: f64,
    height: f64,
    state: &str,
    exif_json: &str,
) -> Result<ExportCompleted, JsValue> {
    let picture = Picture {
        data: canvas,
        width,
        height,
    };
    let done = api::complete(picture, state.as_bytes(), exif_json).map_err(rejected)?;
    let json = format!(
        "{}\n",
        Json::object([
            ("result", done.result),
            ("sha256", Json::str(sha256_hex(&done.file))),
        ])
        .render()
    );
    Ok(ExportCompleted {
        file: done.file,
        json,
    })
}

/// `finish/sizes.output_sizes` for a source of `width × height`.
#[wasm_bindgen]
#[allow(clippy::too_many_arguments)]
pub fn export_output_sizes(
    width: f64,
    height: f64,
    side: &str,
    defaults: &str,
    layout: &str,
    rect: &str,
    preset: Option<String>,
    font_index: &str,
    font_pack: &[u8],
) -> Result<String, JsValue> {
    let fonts = fonts(font_index, font_pack)?;
    let args = SizesArgs {
        width,
        height,
        side: side.as_bytes(),
        defaults: defaults.as_bytes(),
        layout: layout.as_bytes(),
        rect: rect.as_bytes(),
        preset: preset.as_deref(),
    };
    api::sizes(&args, &fonts)
        .map(|j| format!("{}\n", j.render()))
        .map_err(rejected)
}

// ---- export-meta

fn meta_rejected(err: &gxr_raw::CoreError) -> JsValue {
    JsValue::from_str(&error_json(err).render())
}

/// Extract source EXIF `{base, sub}` JSON matching `gxr_studio/export.py:write_with_exif`.
#[wasm_bindgen]
pub fn export_meta_exif(bytes: &[u8]) -> Result<String, JsValue> {
    let text = read_source_exif(bytes).to_json().render();
    Ok(format!("{text}\n"))
}

/// Format frame metadata strings and ISO `taken` / `file_time` from JPG and/or DNG bytes.
///
/// `tz_offset_min` is minutes east of UTC (`local = UTC + tz_offset_min`).
#[wasm_bindgen]
pub fn export_meta_photo(
    jpg_or_null: Option<Vec<u8>>,
    dng_or_null: Option<Vec<u8>>,
    name: &str,
    mtime_ms: Option<f64>,
    tz_offset_min: Option<f64>,
) -> Result<String, JsValue> {
    let res = photo_meta_from_bytes(
        jpg_or_null.as_deref(),
        dng_or_null.as_deref(),
        name,
        mtime_ms,
        tz_offset_min.unwrap_or(0.0),
        DEFAULT_NOW_YEAR,
    )
    .map_err(|e| meta_rejected(&e))?;
    Ok(format!("{}\n", res.to_json().render()))
}

/// Find `{offset, length}` of the largest structurally selected embedded JPEG preview SubIFD in a DNG, or `null`.
/// Decoding (and rejecting corrupt JPEG payloads) stays with the caller.
#[wasm_bindgen]
pub fn export_meta_preview(dng: &[u8]) -> Result<String, JsValue> {
    let json = dng_preview_location(dng).map_or(Json::Null, |r| r.to_json());
    Ok(format!("{}\n", json.render()))
}
