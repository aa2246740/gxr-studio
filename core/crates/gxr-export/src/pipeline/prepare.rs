//! `export_prepare` after the base picture is chosen: the part of `finish/serve.py:_stage_rgb`
//! and `compose.finish_array` that isn't ink.
//!
//! - `_plan`: the resolved frame and stamp, and whether the export is the untouched camera JPG;
//! - `finish_unframed` without `apply_stamp` (gxr-finish raster);
//! - the stamp layout (gxr-frame), which the browser paints: on the photo before the frame, or,
//!   for an instant print with `placement == "border"`, on the canvas after it;
//! - the frame layout (gxr-frame) and the canvas with only the background and the photo
//!   (gxr-finish raster `draw_frame_canvas`);
//! - the labels, as `_stage_rgb` and `_original_labels` finish them.

use gxr_frame::{
    FrameSpec, StampSpec, Val, factory_frame, factory_stamp, format_stamp, frame_json, parse_json,
    parse_stamp, stamp_json, stored_frame,
};
use gxr_raw::{CoreError, Json};

use super::encode::Rgb;

/// `[year, month, day, hour, minute]`, as gxr-frame takes a datetime.
type Moment = (i64, i64, i64, i64, i64);
use super::result::{ExportFail, Labels, validate_options};

/// The shot facts `export_prepare` needs (`shot` in the desktop library).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ShotFacts {
    pub name: String,
    pub width: i64,
    pub height: i64,
    pub has_jpg: bool,
    pub has_dng: bool,
}

fn bad(what: &str) -> CoreError {
    CoreError::new("bad_request", format!("请求无效：{what}"))
}

/// Python truthiness of a JSON value.
pub(crate) fn truthy(v: Option<&Val>) -> bool {
    match v {
        None | Some(Val::Null) => false,
        Some(Val::Bool(b)) => *b,
        Some(Val::Int(n)) => *n != 0,
        Some(Val::Float(f)) => *f != 0.0,
        Some(Val::Str(s)) => !s.is_empty(),
        Some(Val::Arr(a)) => !a.is_empty(),
        Some(Val::Obj(o)) => !o.is_empty(),
    }
}

/// A gxr-frame JSON value as `gxr_raw::Json` (floats keep their bits through the shortest repr).
pub(crate) fn val_json(v: &Val) -> Json {
    match v {
        Val::Null => Json::Null,
        Val::Bool(b) => Json::Bool(*b),
        Val::Int(n) => Json::Int(*n),
        Val::Float(f) => Json::Float(*f),
        Val::Str(s) => Json::Str(s.clone()),
        Val::Arr(a) => Json::Array(a.iter().map(val_json).collect()),
        Val::Obj(o) => Json::Object(o.iter().map(|(k, v)| (k.clone(), val_json(v))).collect()),
    }
}

fn json_val(j: &Json) -> Result<Val, CoreError> {
    parse_json(j.render().as_bytes())
}

impl ShotFacts {
    pub fn parse(v: &Val) -> Result<Self, CoreError> {
        let name = match v.get("name") {
            Some(Val::Str(s)) => s.clone(),
            _ => return Err(bad("shot.name")),
        };
        let dim = |k: &str| match v.get(k) {
            None | Some(Val::Null) => Ok(0),
            Some(Val::Int(n)) => Ok(*n),
            _ => Err(bad(k)),
        };
        let files = v.get("files").ok_or_else(|| bad("shot.files"))?;
        if files.as_object().is_none() {
            return Err(bad("shot.files"));
        }
        Ok(Self {
            name,
            width: dim("width")?,
            height: dim("height")?,
            has_jpg: files.contains("jpg"),
            has_dng: files.contains("dng"),
        })
    }

    /// `serve.full_edge`.
    pub fn full_edge(&self) -> i64 {
        self.width.max(self.height)
    }
}

/// `serve.load_defaults` on the phone's stored defaults (`{"frame", "stamp"}` or nothing): a
/// broken entry falls back to the factory value, as the desktop does.
pub fn load_defaults(text: &[u8]) -> Result<(FrameSpec, StampSpec), CoreError> {
    if text.iter().all(u8::is_ascii_whitespace) {
        return Ok((factory_frame(), factory_stamp()));
    }
    let Ok(raw) = parse_json(text) else {
        return Ok((factory_frame(), factory_stamp()));
    };
    if raw.as_object().is_none() {
        return Ok((factory_frame(), factory_stamp()));
    }
    let frame = stored_frame(raw.get("frame").unwrap_or(&Val::Null))?.unwrap_or_else(factory_frame);
    let stamp = match parse_stamp(raw.get("stamp").unwrap_or(&Val::Null)) {
        Ok(Some(s)) => s,
        Ok(None) => factory_stamp(),
        Err(e) if e.kind.starts_with("bad_") => factory_stamp(),
        Err(e) => return Err(e),
    };
    Ok((frame, stamp))
}

/// `render.params.stored_border`.
fn stored_border(side: &Val) -> Option<Val> {
    for key in ["border", "frame"] {
        let raw = side.get(key);
        if key == "border"
            && let Some(raw) = raw
            && !matches!(raw, Val::Null)
        {
            return Some(if raw.as_object().is_some() {
                raw.clone()
            } else {
                Val::Obj(vec![("invalid".into(), Val::Bool(true))])
            });
        }
        if let Some(raw) = raw
            && raw.as_object().is_some()
            && raw.contains("style")
        {
            return Some(raw.clone());
        }
    }
    None
}

/// The parts of `render.params.parse_edit` that the finish uses, besides colour.
#[derive(Clone, Debug)]
pub struct FinishEdit {
    pub colour: gxr_color::resolve::EditState,
    /// The effective geometry as `Geometry.to_json()`, after the `crop.angle_deg` rule.
    pub geometry: gxr_finish::Geometry,
    pub frame: Option<FrameSpec>,
    pub stamp: Option<StampSpec>,
}

fn edit_err(e: gxr_color::resolve::ResolveError) -> ExportFail {
    match e {
        gxr_color::resolve::ResolveError::Edit(e) => ExportFail::Export {
            code: e.code.to_string(),
            message: e.message,
            detail: e.detail.as_str().unwrap_or("").to_string(),
        },
        gxr_color::resolve::ResolveError::Core(e) => ExportFail::Core(e),
    }
}

/// Port of `gxr_studio/render/params.py:245 parse_edit`.
pub fn parse_finish_edit(side_text: &[u8], side: &Val) -> Result<FinishEdit, ExportFail> {
    let py = gxr_color::resolve::parse_py_json(side_text)?;
    let colour = gxr_color::resolve::parse_edit_state(&py).map_err(edit_err)?;
    let raw_geo = side.get("geometry");
    let mut geometry = if truthy(raw_geo) {
        let text = val_json(raw_geo.unwrap_or(&Val::Null)).render();
        Some(gxr_finish::parse_geometry(&text)?)
    } else {
        None
    };
    let crop = side.get("crop");
    let angle = if truthy(crop) {
        match crop.and_then(|c| c.get("angle_deg")) {
            Some(Val::Int(n)) => Some(*n as f64),
            Some(Val::Float(f)) => Some(*f),
            _ => None,
        }
    } else {
        None
    };
    if let Some(a) = angle
        && a.abs() > 1e-9
    {
        match &mut geometry {
            None => {
                let text = Json::object([("straighten", Json::Float(a))]).render();
                geometry = Some(gxr_finish::parse_geometry(&text)?);
            }
            Some(g) if g.straighten.abs() < 1e-9 => g.straighten = a,
            Some(_) => {}
        }
    }
    let frame = match stored_border(side) {
        Some(raw) => stored_frame(&raw)?,
        None => None,
    };
    let stamp = match side.get("stamp") {
        Some(raw) => parse_stamp(raw)?,
        None => None,
    };
    Ok(FinishEdit {
        colour,
        geometry: geometry.unwrap_or_default(),
        frame,
        stamp,
    })
}

/// Port of `compose.py:has_visible_edit`.
pub fn has_visible_edit(
    edit: &FinishEdit,
    side: &Val,
    frame: &FrameSpec,
    stamp: &StampSpec,
) -> bool {
    let c = &edit.colour;
    if c.recipe.is_some() || c.lut.is_some() || c.curve.is_some() || !c.adjust.is_identity() {
        return true;
    }
    if !edit.geometry.is_identity() {
        return true;
    }
    if truthy(side.get("crop")) {
        return true;
    }
    frame.style != "none" || stamp.enabled
}

/// `serve._plan` for `stage="final"`.
#[derive(Clone, Debug)]
pub struct Plan {
    /// The export is the untouched camera JPG.
    pub original: bool,
    pub frame: FrameSpec,
    pub stamp: StampSpec,
    pub edit: FinishEdit,
}

pub fn plan(
    shot: &ShotFacts,
    side_text: &[u8],
    defaults: &[u8],
    base: &str,
) -> Result<(Plan, Val), ExportFail> {
    let side = if side_text.iter().all(u8::is_ascii_whitespace) {
        Val::Obj(Vec::new())
    } else {
        parse_json(side_text)?
    };
    if side.as_object().is_none() {
        return Err(bad("sidecar 要是对象").into());
    }
    let edit = parse_finish_edit(side_text, &side)?;
    let (frame_default, stamp_default) = load_defaults(defaults)?;
    let frame = edit.frame.clone().unwrap_or(frame_default);
    let stamp = edit.stamp.clone().unwrap_or(stamp_default);
    let untouched = !has_visible_edit(&edit, &side, &frame, &stamp);
    let original = untouched && shot.has_jpg && matches!(base, "auto" | "camera-jpg");
    Ok((
        Plan {
            original,
            frame,
            stamp,
            edit,
        },
        side,
    ))
}

/// `serve._scale` with `made_edge == out_edge` (export is full size before `_fit`).
fn scale_text(shot: &ShotFacts, base_edge: i64, made_edge: i64) -> String {
    let full = shot.full_edge();
    if full == 0 || base_edge == 0 {
        return String::new();
    }
    let made = if made_edge == 0 { 1 } else { made_edge } as f64;
    format!("{:.4}", full as f64 / base_edge as f64 * made / made)
}

/// `serve._original_labels(shot, None)`.
pub fn original_labels(shot: &ShotFacts) -> Labels {
    let full = shot.full_edge();
    let scale = scale_text(shot, full, 0);
    let pairs: [(&str, &str); 11] = [
        ("kind", "original"),
        ("label", "original"),
        ("text", "原图"),
        ("id", "camera-jpg"),
        ("tag", ""),
        ("description", "原图"),
        ("detail", ""),
        ("valid", ""),
        ("calibrated", "0"),
        ("cache", "miss"),
        ("key", ""),
    ];
    let mut out: Vec<(String, String)> = pairs
        .iter()
        .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
        .collect();
    out.push(("scale".into(), scale));
    Labels(out)
}

/// The layout facts the browser and the frame need: `meta`, `logos`, `mark`, `taken`,
/// `file_time` (gxr-frame's request format, without `w` and `h`).
#[derive(Clone, Debug)]
pub struct LayoutFacts {
    pub request: Val,
}

impl LayoutFacts {
    pub fn parse(text: &[u8]) -> Result<Self, CoreError> {
        let v = if text.iter().all(u8::is_ascii_whitespace) {
            Val::Obj(Vec::new())
        } else {
            parse_json(text)?
        };
        if v.as_object().is_none() {
            return Err(bad("layout 要是对象"));
        }
        Ok(Self { request: v })
    }

    fn with_size(&self, w: i64, h: i64, frame: Option<&Json>) -> Result<Val, CoreError> {
        let mut pairs: Vec<(String, Val)> = self
            .request
            .as_object()
            .unwrap_or(&[])
            .iter()
            .filter(|(k, _)| !matches!(k.as_str(), "w" | "h" | "frame"))
            .cloned()
            .collect();
        pairs.push(("w".into(), Val::Int(w)));
        pairs.push(("h".into(), Val::Int(h)));
        if let Some(f) = frame {
            pairs.push(("frame".into(), json_val(f)?));
        }
        Ok(Val::Obj(pairs))
    }

    fn moment(&self, key: &str) -> Result<Option<Moment>, CoreError> {
        match self.request.get(key) {
            None | Some(Val::Null) => Ok(None),
            Some(Val::Arr(p)) => match p.as_slice() {
                [
                    Val::Int(y),
                    Val::Int(mo),
                    Val::Int(d),
                    Val::Int(hh),
                    Val::Int(mm),
                ] => Ok(Some((*y, *mo, *d, *hh, *mm))),
                _ => Err(bad(key)),
            },
            Some(_) => Err(bad(key)),
        }
    }
}

/// What `export_prepare` returns besides the canvas.
#[derive(Clone, Debug)]
pub struct Prepared {
    pub canvas: Rgb,
    /// `{"frame", "stamp", "stamp_on", "photo", "labels", "state"}`.
    pub json: Json,
}

/// Options `export_finish` validates first.
#[derive(Clone, Copy, Debug)]
pub struct ExportOpts {
    pub quality: f64,
    pub long_edge: Option<f64>,
    pub hdr: bool,
}

fn state_json(
    shot: &ShotFacts,
    q: i64,
    edge: Option<i64>,
    labels: &Labels,
    hdr: Option<Json>,
) -> Json {
    Json::object([
        ("name", Json::str(shot.name.clone())),
        // `serve._source_path`: which file the caller passes to `export_complete` for EXIF.
        (
            "source",
            Json::str(if shot.has_jpg { "jpg" } else { "dng" }),
        ),
        ("quality", Json::Int(q)),
        ("long_edge", edge.map_or(Json::Null, Json::Int)),
        ("labels", labels.to_json()),
        ("hdr", hdr.unwrap_or(Json::Null)),
    ])
}

/// The untouched camera JPG: the decoded picture is the canvas, with no layouts.
pub fn prepare_original(
    shot: &ShotFacts,
    jpg: Rgb,
    opts: ExportOpts,
) -> Result<Prepared, ExportFail> {
    let (q, edge) = validate_options(opts.quality, opts.long_edge)?;
    let labels = original_labels(shot);
    let photo = photo_rect(0, 0, jpg.width, jpg.height);
    let hdr = opts
        .hdr
        .then(|| Json::object([("asked", Json::Bool(true))]));
    let json = Json::object([
        ("canvas", Json::str("full")),
        ("frame", Json::Null),
        ("stamp", Json::Null),
        ("stamp_on", Json::Null),
        ("photo", photo),
        ("labels", labels.to_json()),
        ("state", state_json(shot, q, edge, &labels, hdr)),
    ]);
    Ok(Prepared { canvas: jpg, json })
}

fn photo_rect(x: i64, y: i64, w: usize, h: usize) -> Json {
    Json::object([
        ("x", Json::Int(x)),
        ("y", Json::Int(y)),
        ("w", Json::Int(w as i64)),
        ("h", Json::Int(h as i64)),
    ])
}

/// `_stage_rgb`'s crop: `side.get("crop")` when it is a dict; `rect` when a list of 4 numbers,
/// `preset` when a string. (Python's `float(x)` also takes numeric strings; the phone writes its
/// own sidecar with numbers, so anything else is `bad_request`.)
pub fn crop_of(side: &Val) -> Result<(Option<[f64; 4]>, Option<String>), ExportFail> {
    let crop = side.get("crop").filter(|c| c.as_object().is_some());
    let rect = match crop.and_then(|c| c.get("rect")) {
        Some(Val::Arr(items)) if items.len() == 4 => {
            let mut out = [0.0_f64; 4];
            for (slot, it) in out.iter_mut().zip(items.iter()) {
                *slot = match it {
                    Val::Int(n) => *n as f64,
                    Val::Float(f) => *f,
                    Val::Bool(b) => {
                        if *b {
                            1.0
                        } else {
                            0.0
                        }
                    }
                    _ => return Err(bad("crop.rect").into()),
                };
            }
            Some(out)
        }
        _ => None,
    };
    let preset = match crop.and_then(|c| c.get("preset")) {
        Some(Val::Str(s)) => Some(s.clone()),
        _ => None,
    };
    Ok((rect, preset))
}

/// The base picture `color_image` chose (`from_raw`, labels), through the finish.
pub struct Picked {
    pub rgb: Rgb,
    pub from_raw: bool,
    pub labels: Labels,
}

/// Port of `serve._stage_rgb` (stage `final`, full size) after `color_image`, and
/// `compose.finish_array` without ink.
pub fn prepare_picked(
    shot: &ShotFacts,
    plan: &Plan,
    side: &Val,
    picked: Picked,
    layout: &LayoutFacts,
    fonts: &gxr_frame::Fonts,
    opts: ExportOpts,
) -> Result<Prepared, ExportFail> {
    let (q, edge) = validate_options(opts.quality, opts.long_edge)?;
    let Picked {
        rgb,
        from_raw,
        mut labels,
    } = picked;
    let base_edge = rgb.width.max(rgb.height) as i64;
    let (crop_rect, crop_preset) = crop_of(side)?;
    let rect = crop_rect.map_or(Json::Null, |r| Json::floats(&r));
    let preset = crop_preset.map_or(Json::Null, Json::Str);
    let frame = &plan.frame;
    let stamp = &plan.stamp;
    let border_stamp = stamp.enabled && stamp.placement == "border" && frame.style == "instant";

    let mut edit_pairs = Vec::new();
    if let Some(a) = side.get("adjust") {
        edit_pairs.push(("adjust", val_json(a)));
    }
    if let Some(c) = side.get("curve") {
        edit_pairs.push(("curve", val_json(c)));
    }
    edit_pairs.push(("geometry", plan.edit.geometry.to_json()));
    let edit_text = Json::object(edit_pairs).render();
    let options = Json::object([
        ("stage", Json::str("final")),
        ("from_raw", Json::Bool(from_raw)),
        ("crop_rect", rect),
        ("crop_preset", preset),
    ])
    .render();
    let fin = gxr_finish::finish_array_u64(
        &rgb.data,
        rgb.width as u64,
        rgb.height as u64,
        &edit_text,
        &options,
    )?;
    drop(rgb);
    let (pw, ph) = (fin.width as i64, fin.height as i64);
    let taken = layout.moment("taken")?;
    let file_time = layout.moment("file_time")?;
    let stamp_text = format_stamp(stamp, taken, file_time);

    let stamp_spec = json_val(&stamp.to_json())?;
    let photo_stamp = if stamp.enabled && !border_stamp && !stamp_text.is_empty() {
        stamp_json(&layout.with_size(pw, ph, None)?, &stamp_spec, fonts)?
    } else {
        Json::Null
    };
    let frame_layout = if frame.style == "none" {
        Json::Null
    } else {
        frame_json(
            &layout.with_size(pw, ph, None)?,
            &json_val(&frame.to_json())?,
            fonts,
        )?
    };
    // `canvas`: "full" when the returned picture is the whole canvas (background and photo, or
    // the photo when there is no frame); "photo" for a blur backdrop or paper grain, which the
    // browser paints (PORT-WAVE-1 decision 3): it then draws the backdrop and pastes the photo
    // at `photo`.
    let (canvas, photo, canvas_kind) = match &frame_layout {
        Json::Null => (
            Rgb {
                width: fin.width,
                height: fin.height,
                data: fin.rgb,
            },
            photo_rect(0, 0, fin.width, fin.height),
            "full",
        ),
        lay => {
            let text = lay.render();
            let v = parse_json(text.as_bytes())?;
            let (x, y) = match v.get("photo") {
                Some(Val::Arr(p)) => match p.as_slice() {
                    [Val::Int(x), Val::Int(y)] => (*x, *y),
                    _ => (0, 0),
                },
                _ => (0, 0),
            };
            match gxr_finish::draw_frame_canvas_u64(
                &fin.rgb,
                fin.width as u64,
                fin.height as u64,
                &text,
            ) {
                Ok(c) => (
                    Rgb {
                        width: c.width,
                        height: c.height,
                        data: c.rgb,
                    },
                    photo_rect(x, y, fin.width, fin.height),
                    "full",
                ),
                Err(e) if e.kind == "needs_browser_backdrop" => (
                    Rgb {
                        width: fin.width,
                        height: fin.height,
                        data: fin.rgb,
                    },
                    photo_rect(x, y, fin.width, fin.height),
                    "photo",
                ),
                Err(e) => return Err(e.into()),
            }
        }
    };
    let border = if border_stamp && !matches!(frame_layout, Json::Null) && !stamp_text.is_empty() {
        let frame_spec = frame.to_json();
        stamp_json(
            &layout.with_size(pw, ph, Some(&frame_spec))?,
            &stamp_spec,
            fonts,
        )?
    } else {
        Json::Null
    };
    let (stamp_out, stamp_on) = if !matches!(photo_stamp, Json::Null) {
        (photo_stamp, Json::str("photo"))
    } else if !matches!(border, Json::Null) {
        (border, Json::str("canvas"))
    } else {
        (Json::Null, Json::Null)
    };
    labels_set(&mut labels, "valid", "");
    labels_default(&mut labels, "label");
    labels_default_text(&mut labels);
    labels_set(
        &mut labels,
        "scale",
        &scale_text(shot, base_edge, pw.max(ph)),
    );
    let hdr = opts
        .hdr
        .then(|| Json::object([("asked", Json::Bool(true))]));
    let json = Json::object([
        ("canvas", Json::str(canvas_kind)),
        ("frame", frame_layout),
        ("stamp", stamp_out),
        ("stamp_on", stamp_on),
        ("photo", photo),
        ("labels", labels.to_json()),
        ("state", state_json(shot, q, edge, &labels, hdr)),
    ]);
    Ok(Prepared { canvas, json })
}

fn labels_set(l: &mut Labels, k: &str, v: &str) {
    if let Some(slot) = l.0.iter_mut().find(|(key, _)| key == k) {
        slot.1 = v.to_string();
    } else {
        l.0.push((k.to_string(), v.to_string()));
    }
}

/// `labels.setdefault("label", labels.get("kind") or "original")`.
fn labels_default(l: &mut Labels, k: &str) {
    if l.get(k).is_none() {
        let kind = l
            .get("kind")
            .filter(|s| !s.is_empty())
            .unwrap_or("original")
            .to_string();
        l.0.push((k.to_string(), kind));
    }
}

/// `labels.setdefault("text", "原图")`.
fn labels_default_text(l: &mut Labels) {
    if l.get("text").is_none() {
        l.0.push(("text".into(), "原图".into()));
    }
}
