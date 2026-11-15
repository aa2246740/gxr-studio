//! The byte-and-JSON entry points `gxr-cli` and `gxr-wasm` share, so both run the same code on
//! the same inputs. Sizes and counts arrive as `f64` (WASM numbers) and are checked here before
//! anything is narrowed or copied.

use gxr_frame::{Fonts, Val, parse_json};
use gxr_raw::{CoreError, Json};

use super::complete::{Completed, HdrInputs, State, checked_rgb, complete_export};
use super::encode::Rgb;
use super::hdr::{StampMask, begin_track, hdr_state_json, track_geometry};
use crate::hdr_track::HdrTrackState;
use gxr_raw::Image;

use crate::source::{
    CAMERA_JPG, CalibPack, PREVIEW, Pictures, Request, check_rgb_shape, color_image_json,
};

use super::prepare::{
    ExportOpts, FinishEdit, LayoutFacts, Picked, ShotFacts, crop_of, parse_finish_edit, plan,
    prepare_original, prepare_picked, val_json,
};
use super::result::{ExportFail, Labels, validate_options};
use super::sizes::output_sizes;

/// A JS number that must be a whole, finite, non-negative size (`u64`).
pub fn dim_from_f64(v: f64, what: &str) -> Result<u64, CoreError> {
    if !v.is_finite() || v.fract() != 0.0 || !(0.0..=9_007_199_254_740_991.0).contains(&v) {
        return Err(CoreError::new("bad_image", format!("{what} 要是非负整数")));
    }
    Ok(v as u64)
}

/// A picture handed in by the caller: bytes plus its claimed size.
pub struct Picture<'a> {
    pub data: &'a [u8],
    pub width: f64,
    pub height: f64,
}

/// Everything `export_prepare` reads besides the pictures and the source files.
pub struct PrepareArgs<'a> {
    pub shot: &'a [u8],
    pub side: &'a [u8],
    pub defaults: &'a [u8],
    pub layout: &'a [u8],
    pub base: &'a str,
    pub opts: ExportOpts,
}

/// What `color_image` (export-source) reads: the camera data, the calibration pack, the
/// recipe entry the resolve unit gave, the engine binding for the EXIF description, the DNG
/// bytes, the camera JPG's original bytes (hdr-track's capture check) and a LUT. Empty bytes
/// mean "not given", as in export-source.
#[derive(Default)]
pub struct SourceFiles<'a> {
    pub dat: &'a [u8],
    pub entry: &'a [u8],
    pub pack: Option<&'a CalibPack>,
    pub binding: &'a str,
    pub dng: &'a [u8],
    pub jpg_bytes: &'a [u8],
    pub cube: &'a [u8],
    pub space: &'a str,
    /// The source file's `lastModified` in ms (`photo_meta`'s `file_time`), and the time zone
    /// offset in minutes east of UTC at that moment. Rust never reads the clock or the zone.
    pub mtime_ms: Option<f64>,
    pub tz_offset_min: f64,
}

fn given(b: &[u8]) -> Option<&[u8]> {
    (!b.is_empty()).then_some(b)
}

/// A caller's picture as gxr-raw's `Image`, checked with export-source's own shape rule.
fn image_of(p: &Picture<'_>, what: &str) -> Result<Image<u8>, CoreError> {
    let w = dim_from_f64(p.width, "宽")?;
    let h = dim_from_f64(p.height, "高")?;
    let (wu, hu) = checked_rgb(p.data, w, h)?;
    check_rgb_shape(hu, wu, 3, p.data.len(), what)?;
    let mut values = Vec::new();
    values
        .try_reserve_exact(p.data.len())
        .map_err(|_| CoreError::new("too_big", "图像太大"))?;
    values.extend_from_slice(p.data);
    Ok(Image {
        height: hu,
        width: wu,
        channels: 3,
        values,
    })
}

/// The edit as export-source reads it (`recipe`, `lut`, the tone fields of `adjust`,
/// `curves`), from the sidecar `render.params.parse_edit` already validated.
fn source_edit(edit: &FinishEdit) -> String {
    let c = &edit.colour;
    let a = &c.adjust;
    let adjust = Json::object([
        ("exposure_ev", Json::Float(a.exposure_ev)),
        ("warm", Json::Int(a.warm)),
        ("tint", Json::Int(a.tint)),
        ("contrast", Json::Int(a.contrast)),
        ("highlight", Json::Int(a.highlight)),
        ("shadow", Json::Int(a.shadow)),
    ]);
    let pts = |p: &Option<Vec<(f64, f64)>>| match p {
        None => Json::Null,
        Some(v) => Json::Array(v.iter().map(|&(x, y)| Json::floats(&[x, y])).collect()),
    };
    let curves = match &c.curve {
        None => Json::Null,
        Some(cv) => Json::object([
            ("master", pts(&cv.master)),
            ("r", pts(&cv.r)),
            ("g", pts(&cv.g)),
            ("b", pts(&cv.b)),
        ]),
    };
    let recipe = c
        .recipe
        .as_ref()
        .map_or_else(|| "null".to_string(), |r| r.to_json());
    let lut = c.lut.as_ref().map_or(Json::Null, |l| Json::str(l.clone()));
    format!(
        "{{\"recipe\":{recipe},\"lut\":{},\"adjust\":{},\"curves\":{}}}",
        lut.render(),
        adjust.render(),
        curves.render()
    )
}

/// `serve._meta(shot, lib)`: `photo_meta` from the shot's own files (the camera JPG when the
/// shot has one, else the DNG; export-meta). Without any source bytes (synthetic callers) the
/// layout's own `meta`, `taken` and `file_time` stay.
fn with_photo_meta(
    layout: LayoutFacts,
    shot: &ShotFacts,
    files: &SourceFiles<'_>,
) -> Result<LayoutFacts, ExportFail> {
    let jpg = given(files.jpg_bytes).filter(|_| shot.has_jpg);
    let dng = given(files.dng).filter(|_| shot.has_dng);
    if jpg.is_none() && dng.is_none() {
        return Ok(layout);
    }
    let pm = crate::meta::photo_meta_from_bytes(
        jpg,
        dng,
        &shot.name,
        files.mtime_ms,
        files.tz_offset_min,
        crate::meta::DEFAULT_NOW_YEAR,
    )?;
    let moment = |d: &Option<crate::meta::NaiveDateTime>| match d {
        None => Val::Null,
        Some(d) => Val::Arr(vec![
            Val::Int(i64::from(d.year)),
            Val::Int(i64::from(d.month)),
            Val::Int(i64::from(d.day)),
            Val::Int(i64::from(d.hour)),
            Val::Int(i64::from(d.minute)),
        ]),
    };
    let s = |t: &str| Val::Str(t.to_string());
    let meta = Val::Obj(vec![
        ("lens".into(), s(&pm.lens)),
        ("lens_short".into(), s(&pm.lens_short)),
        ("aperture".into(), s(&pm.aperture)),
        ("shutter".into(), s(&pm.shutter)),
        ("iso".into(), s(&pm.iso)),
        ("date".into(), s(&pm.date)),
        ("frame_no".into(), s(&pm.frame_no)),
    ]);
    let mut pairs: Vec<(String, Val)> = layout
        .request
        .as_object()
        .unwrap_or(&[])
        .iter()
        .filter(|(k, _)| !matches!(k.as_str(), "meta" | "taken" | "file_time"))
        .cloned()
        .collect();
    pairs.push(("meta".into(), meta));
    pairs.push(("taken".into(), moment(&pm.taken)));
    pairs.push(("file_time".into(), moment(&pm.file_time)));
    Ok(LayoutFacts {
        request: Val::Obj(pairs),
    })
}

/// An error JSON text from export-source, kept as it is.
fn rendered(text: &str) -> ExportFail {
    match parse_json(text.as_bytes()) {
        Ok(v) => {
            let kind = match v.get("error").and_then(|e| e.get("kind")) {
                Some(Val::Str(k)) => k.clone(),
                _ => String::new(),
            };
            ExportFail::Rendered {
                kind,
                json: val_json(&v),
            }
        }
        Err(e) => ExportFail::Core(e),
    }
}

/// `export_prepare`: `_plan`, then the untouched camera JPG (`jpg`) or export-source's
/// `color_image` (RAW recipe, neutral RAW, embedded preview, LUT, edited camera JPG), then
/// the finish without ink. Returns the canvas, the prepared JSON, and whether the untouched
/// camera JPG branch was taken.
/// What `export_prepare` returns: the canvas, the prepared JSON, whether the untouched camera
/// JPG branch was taken, and, when HDR was asked for and the track is active, its float32
/// `(h, w, 2)` layer for `export_complete`.
pub struct PrepareOut {
    pub canvas: Rgb,
    pub json: Json,
    pub original: bool,
    pub hdr_layer: Option<Vec<f32>>,
}

/// `HdrTrack.begin` on the base picture, then the photo's geometry; `state.hdr` in `json` is
/// replaced by the track's meta and the frame layout.
fn with_hdr(mut json: Json, track: HdrTrackState) -> (Json, Option<Vec<f32>>) {
    let frame = match &json {
        Json::Object(p) => p
            .iter()
            .find(|(k, _)| k == "frame")
            .map_or(Json::Null, |(_, v)| v.clone()),
        _ => Json::Null,
    };
    let hdr_json = hdr_state_json(&track, &frame);
    if let Json::Object(pairs) = &mut json
        && let Some((_, Json::Object(state))) = pairs.iter_mut().find(|(k, _)| k == "state")
        && let Some(slot) = state.iter_mut().find(|(k, _)| k == "hdr")
    {
        slot.1 = hdr_json;
    }
    let layer = track.active.then_some(track.layer);
    (json, layer)
}

pub fn prepare(
    args: &PrepareArgs<'_>,
    files: &SourceFiles<'_>,
    jpg: Option<Picture<'_>>,
    preview: Option<Picture<'_>>,
    fonts: &Fonts,
) -> Result<PrepareOut, ExportFail> {
    validate_options(args.opts.quality, args.opts.long_edge)?;
    let shot = ShotFacts::parse(&parse_json(args.shot)?)?;
    let (plan, side) = plan(&shot, args.side, args.defaults, args.base)?;
    let jpg = jpg.map(|p| image_of(&p, CAMERA_JPG)).transpose()?;
    if plan.original {
        let jpg = jpg.ok_or_else(|| CoreError::new("bad_request", "需要相机 JPG 的像素"))?;
        let rgb = Rgb {
            width: jpg.width,
            height: jpg.height,
            data: jpg.values,
        };
        let track = if args.opts.hdr {
            Some(begin_track(
                &rgb,
                hdr_dng(&shot, files),
                &dng_name(args.shot)?,
                hdr_jpeg(&shot, files),
            )?)
        } else {
            None
        };
        let p = prepare_original(&shot, rgb, args.opts)?;
        let (json, hdr_layer) = match track {
            Some(t) => with_hdr(p.json, t),
            None => (p.json, None),
        };
        return Ok(PrepareOut {
            canvas: p.canvas,
            json,
            original: true,
            hdr_layer,
        });
    }
    let preview = preview.map(|p| image_of(&p, PREVIEW)).transpose()?;
    let edit_text = source_edit(&plan.edit);
    let req = Request {
        shot: args.shot,
        edit: edit_text.as_bytes(),
        dat: given(files.dat),
        library: None,
        entry: given(files.entry),
        pack: files.pack,
        binding: files.binding,
    };
    let pics = Pictures {
        dng: given(files.dng),
        jpg: jpg.as_ref(),
        preview: preview.as_ref(),
        cube: given(files.cube),
        space: (!files.space.is_empty()).then_some(files.space),
    };
    let (json, image) = color_image_json(&req, args.base, &pics).map_err(|t| rendered(&t))?;
    drop(jpg);
    drop(preview);
    let v = parse_json(json.as_bytes())?;
    let from_raw = matches!(v.get("from_raw"), Some(Val::Bool(true)));
    let labels = Labels::from_val(v.get("labels").unwrap_or(&Val::Obj(Vec::new())))?;
    let picked = Picked {
        rgb: Rgb {
            width: image.width,
            height: image.height,
            data: image.values,
        },
        from_raw,
        labels,
    };
    let track = if args.opts.hdr {
        let t = begin_track(
            &picked.rgb,
            hdr_dng(&shot, files),
            &dng_name(args.shot)?,
            hdr_jpeg(&shot, files),
        )?;
        let (rect, preset) = crop_of(&side)?;
        Some(track_geometry(
            t,
            picked.rgb.width,
            picked.rgb.height,
            &plan.edit.geometry,
            rect,
            preset.as_deref(),
        )?)
    } else {
        None
    };
    let layout = LayoutFacts::parse(args.layout)?;
    let layout = with_photo_meta(layout, &shot, files)?;
    let p = prepare_picked(&shot, &plan, &side, picked, &layout, fonts, args.opts)?;
    let (json, hdr_layer) = match track {
        Some(t) => with_hdr(p.json, t),
        None => (p.json, None),
    };
    Ok(PrepareOut {
        canvas: p.canvas,
        json,
        original: false,
        hdr_layer,
    })
}

/// `HdrTrack(dng=...)`: the DNG when the shot has one and its bytes were given.
fn hdr_dng<'a>(shot: &ShotFacts, files: &SourceFiles<'a>) -> Option<&'a [u8]> {
    given(files.dng).filter(|_| shot.has_dng)
}

/// `HdrTrack(jpeg=...)`: the camera JPG's original bytes when the shot has one.
fn hdr_jpeg<'a>(shot: &ShotFacts, files: &SourceFiles<'a>) -> Option<&'a [u8]> {
    given(files.jpg_bytes).filter(|_| shot.has_jpg)
}

/// `Path(files["dng"]["relpath"]).name`, the name `open_dng` puts in its errors.
fn dng_name(shot: &[u8]) -> Result<String, CoreError> {
    Ok(crate::source::parse_shot(shot)?.dng_name)
}

/// `serve._plan` as `{"original", "frame", "stamp"}`.
pub fn plan_json(
    shot: &[u8],
    side: &[u8],
    defaults: &[u8],
    base: &str,
) -> Result<Json, ExportFail> {
    let facts = ShotFacts::parse(&parse_json(shot)?)?;
    let (p, _side) = plan(&facts, side, defaults, base)?;
    Ok(Json::object([
        ("original", Json::Bool(p.original)),
        ("frame", p.frame.to_json()),
        ("stamp", p.stamp.to_json()),
    ]))
}

/// Where `export_complete` gets the source photo's EXIF.
pub enum ExifSource<'a> {
    /// The source file's bytes (`serve._source_path`: the camera JPG when the shot has one,
    /// else the DNG), read by export-meta exactly as `export.source_exif` reads them.
    File(&'a [u8]),
    /// Already as gxr-jpeg JSON (`base` / `exif`, without 270), for unit tests.
    Json(&'a str),
}

/// `export_complete` without HDR: the inked canvas, the state JSON from `prepare` and the source
/// EXIF.
/// The HDR inputs of `export_complete` as the caller gives them: the layer from `prepare`
/// (empty when the track is inactive) and the stamp mask with its size as JS numbers (an empty
/// mask means no stamp on the photo).
pub struct HdrArgs<'a> {
    pub layer: &'a [f32],
    pub mask: &'a [u8],
    pub mask_width: f64,
    pub mask_height: f64,
}

pub fn complete(
    canvas: Picture<'_>,
    state: &[u8],
    exif: ExifSource<'_>,
    hdr: Option<HdrArgs<'_>>,
) -> Result<Completed, ExportFail> {
    let hdr = match hdr {
        None => None,
        Some(a) => {
            let mask = if a.mask.is_empty() {
                None
            } else {
                Some(StampMask {
                    data: a.mask,
                    width: dim_from_f64(a.mask_width, "掩码宽")?,
                    height: dim_from_f64(a.mask_height, "掩码高")?,
                })
            };
            Some(HdrInputs {
                layer: a.layer,
                mask,
            })
        }
    };
    let w = dim_from_f64(canvas.width, "宽")?;
    let h = dim_from_f64(canvas.height, "高")?;
    checked_rgb(canvas.data, w, h)?;
    let state = State::parse(state)?;
    let source = match exif {
        ExifSource::File(bytes) => crate::meta::read_source_exif(bytes).to_jpeg_exif_spec(),
        ExifSource::Json(text) => gxr_jpeg::parse_exif_json(text)?,
    };
    let (done, _rgb) = complete_export(canvas.data, w, h, &state, &source, hdr)?;
    Ok(done)
}

fn parse_rect(text: &[u8]) -> Result<Option<[f64; 4]>, CoreError> {
    if text.iter().all(u8::is_ascii_whitespace) {
        return Ok(None);
    }
    let bad = || CoreError::new("bad_rect", "裁剪框格式不对");
    match parse_json(text)? {
        Val::Null => Ok(None),
        Val::Arr(items) if items.len() == 4 => {
            let mut r = [0.0_f64; 4];
            for (slot, it) in r.iter_mut().zip(items.iter()) {
                *slot = match it {
                    Val::Int(n) => *n as f64,
                    Val::Float(f) => *f,
                    _ => return Err(bad()),
                };
            }
            Ok(Some(r))
        }
        _ => Err(bad()),
    }
}

/// Inputs of `output_sizes`.
pub struct SizesArgs<'a> {
    pub width: f64,
    pub height: f64,
    pub side: &'a [u8],
    pub defaults: &'a [u8],
    pub layout: &'a [u8],
    pub rect: &'a [u8],
    pub preset: Option<&'a str>,
}

/// `output_sizes` for a source of `width × height`.
pub fn sizes(args: &SizesArgs<'_>, fonts: &Fonts) -> Result<Json, ExportFail> {
    let w = dim_from_f64(args.width, "宽")?;
    let h = dim_from_f64(args.height, "高")?;
    let side_text: &[u8] = if args.side.iter().all(u8::is_ascii_whitespace) {
        b"{}"
    } else {
        args.side
    };
    let side = parse_json(side_text)?;
    if side.as_object().is_none() {
        return Err(CoreError::new("bad_request", "请求无效：sidecar 要是对象").into());
    }
    let edit = parse_finish_edit(side_text, &side)?;
    let rect = parse_rect(args.rect)?;
    let layout = LayoutFacts::parse(args.layout)?;
    output_sizes(
        w,
        h,
        &edit,
        args.defaults,
        rect,
        args.preset,
        &layout,
        fonts,
    )
}
