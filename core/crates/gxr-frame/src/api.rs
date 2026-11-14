//! The request format the CLI and the WASM bindings share.
//!
//! Frame request: `{"w": int, "h": int, "meta": {str: str}, "logos": [LogoInfo...],
//! "mark": {"w", "h", "ink_top", "ink_bottom"}}`. `mark` is the built-in mark's box (see
//! `layout::Mark`); only the Leica style's built-in logo reads it.
//! Stamp request: the same plus `"taken"` and `"file_time"` (`[year, month, day, hour, minute]`
//! or `null`) and `"frame"` (a frame spec, or `null`). The stamp text is `format_stamp(spec,
//! taken, file_time)`; with a frame spec, the stamp is laid out on that frame's border (the
//! caller decides when that applies, as `compose.finish_array` does for instant prints).
//! `apply_stamp` skips a disabled stamp; `stamp_layout` itself does not look at `enabled`, and
//! neither does this.
//!
//! The spec is the dataclass fields (`FrameSpec.to_json()` / `StampSpec.to_json()` shape) and is
//! built like `FrameSpec(**fields)`: defaults and `__post_init__`, no value checks. Validate user
//! input with `frame-spec` / `stamp-spec` (`parse_frame` / `parse_stamp`) first, as the desktop
//! routes do; the layout functions themselves accept, say, a caption longer than 24 characters.
//!
//! `w` and `h` must be 1 … 65535, JPEG's largest side (the phone exports JPEG); Python has no such
//! check. The bound keeps every layout product far inside `i64` and caps the number of items a
//! style can emit (the film strip's sprocket holes grow with the long side when the short side is
//! tiny), so a huge request fails with this one `bad_request` on native and WASM alike instead of
//! running out of memory on wasm32.

use gxr_raw::{CoreError, Json, error_json};

use crate::Meta;
use crate::layout::{FrameLayout, Logos, Mark, Moment, format_stamp, frame_layout, stamp_layout};
use crate::logo::LogoInfo;
use crate::metrics::Fonts;
use crate::spec::{frame_spec_from_fields, stamp_spec_from_fields};
use crate::value::{Val, parse_json};

const MAX_SIDE: i64 = 65535;

fn bad(message: impl Into<String>) -> CoreError {
    CoreError::new("bad_request", message)
}

fn side(request: &Val, key: &str) -> Result<i64, CoreError> {
    match request.get(key) {
        Some(Val::Int(n)) if (1..=MAX_SIDE).contains(n) => Ok(*n),
        _ => Err(bad(format!("{key} 要是 1 到 {MAX_SIDE} 的整数"))),
    }
}

fn meta_of(request: &Val) -> Result<Meta, CoreError> {
    let mut meta = Meta::new();
    match request.get("meta") {
        None | Some(Val::Null) => {}
        Some(Val::Obj(pairs)) => {
            for (k, v) in pairs {
                let Val::Str(s) = v else {
                    return Err(bad("meta 的值要是字符串"));
                };
                meta.insert(k.clone(), s.clone());
            }
        }
        Some(_) => return Err(bad("meta 要是对象")),
    }
    Ok(meta)
}

fn number(v: Option<&Val>, what: &str) -> Result<f64, CoreError> {
    match v {
        Some(Val::Int(n)) => Ok(crate::py::f(*n)),
        Some(Val::Float(x)) => Ok(*x),
        _ => Err(bad(format!("标志的 {what} 要是数字"))),
    }
}

fn logos_of(request: &Val) -> Result<Logos, CoreError> {
    let mut out = Logos::new();
    let list = match request.get("logos") {
        None | Some(Val::Null) => return Ok(out),
        Some(Val::Arr(list)) => list,
        Some(_) => return Err(bad("logos 要是列表")),
    };
    for raw in list {
        let text = |k: &str| match raw.get(k) {
            Some(Val::Str(s)) => Ok(s.clone()),
            _ => Err(bad(format!("标志的 {k} 要是字符串"))),
        };
        let integer = |k: &str| match raw.get(k) {
            Some(Val::Int(n)) => Ok(*n),
            _ => Err(bad(format!("标志的 {k} 要是整数"))),
        };
        let logo = LogoInfo {
            id: text("id")?,
            name: text("name")?,
            w: integer("w")?,
            h: integer("h")?,
            coverage: number(raw.get("coverage"), "coverage")?,
            opaque: matches!(raw.get("opaque"), Some(Val::Bool(true))),
            lum: number(raw.get("lum"), "lum")?,
        };
        out.insert(logo.id.clone(), logo);
    }
    Ok(out)
}

/// `"mark": {"w", "h", "ink_top", "ink_bottom"}`: the built-in mark's box and inked rows.
/// Optional; a layout that draws the built-in mark without it fails with `Mark::missing`.
fn mark_of(request: &Val) -> Result<Option<Mark>, CoreError> {
    let raw = match request.get("mark") {
        None | Some(Val::Null) => return Ok(None),
        Some(raw @ Val::Obj(_)) => raw,
        Some(_) => return Err(bad("mark 要是对象")),
    };
    let field = |k: &str| match raw.get(k) {
        Some(Val::Int(n)) if (0..=MAX_SIDE).contains(n) => Ok(*n),
        _ => Err(bad(format!("mark 的 {k} 要是 0 到 {MAX_SIDE} 的整数"))),
    };
    let mark = Mark {
        w: field("w")?,
        h: field("h")?,
        ink_top: field("ink_top")?,
        ink_bottom: field("ink_bottom")?,
    };
    if mark.w < 1 || mark.h < 1 || mark.ink_bottom < mark.ink_top {
        return Err(bad("mark 的尺寸要大于 0，墨迹行要从上到下"));
    }
    Ok(Some(mark))
}

fn moment_of(request: &Val, key: &str) -> Result<Option<Moment>, CoreError> {
    match request.get(key) {
        None | Some(Val::Null) => Ok(None),
        Some(Val::Arr(parts)) => match parts.as_slice() {
            [
                Val::Int(y),
                Val::Int(mo),
                Val::Int(d),
                Val::Int(hh),
                Val::Int(mm),
            ] => Ok(Some((*y, *mo, *d, *hh, *mm))),
            _ => Err(bad(format!("{key} 要是 [年, 月, 日, 时, 分]"))),
        },
        Some(_) => Err(bad(format!("{key} 要是 [年, 月, 日, 时, 分]"))),
    }
}

fn frame_for(
    request: &Val,
    spec_raw: &Val,
    fonts: &Fonts,
) -> Result<Option<FrameLayout>, CoreError> {
    let (w, h) = (side(request, "w")?, side(request, "h")?);
    let spec = frame_spec_from_fields(spec_raw)?;
    frame_layout(
        w,
        h,
        &spec,
        &meta_of(request)?,
        fonts,
        &logos_of(request)?,
        mark_of(request)?,
    )
}

/// `frame_layout(...).to_json()`, or `null` for style `none`.
pub fn frame_json(request: &Val, spec_raw: &Val, fonts: &Fonts) -> Result<Json, CoreError> {
    Ok(frame_for(request, spec_raw, fonts)?.map_or(Json::Null, |l| l.to_json()))
}

/// `stamp_layout(w, h, spec, format_stamp(spec, taken, file_time), m, frame).to_json()`.
pub fn stamp_json(request: &Val, spec_raw: &Val, fonts: &Fonts) -> Result<Json, CoreError> {
    let (w, h) = (side(request, "w")?, side(request, "h")?);
    let spec = stamp_spec_from_fields(spec_raw)?;
    let frame = match request.get("frame") {
        None | Some(Val::Null) => None,
        Some(frame_raw) => frame_for(request, frame_raw, fonts)?,
    };
    let text = format_stamp(
        &spec,
        moment_of(request, "taken")?,
        moment_of(request, "file_time")?,
    );
    let layout = stamp_layout(w, h, &spec, &text, fonts, frame.as_ref())?;
    Ok(layout.map_or(Json::Null, |l| l.to_json()))
}

fn one(result: Result<Json, CoreError>) -> Json {
    result.unwrap_or_else(|err| error_json(&err))
}

/// Many layouts in one call: `{"frames": [{"request", "spec"}...], "stamps": [...]}` →
/// `{"frames": [...], "stamps": [...]}`, each a layout, `null`, or `{"error": …}`.
pub fn batch_json(cases: &Val, fonts: &Fonts) -> Result<Json, CoreError> {
    let list = |key: &str| -> Result<Vec<Json>, CoreError> {
        let items = match cases.get(key) {
            None | Some(Val::Null) => return Ok(Vec::new()),
            Some(Val::Arr(items)) => items,
            Some(_) => return Err(bad(format!("{key} 要是列表"))),
        };
        let mut out = Vec::with_capacity(items.len());
        for case in items {
            let request = case.get("request").cloned().unwrap_or(Val::Null);
            let spec = case.get("spec").cloned().unwrap_or(Val::Null);
            out.push(one(if key == "frames" {
                frame_json(&request, &spec, fonts)
            } else {
                stamp_json(&request, &spec, fonts)
            }));
        }
        Ok(out)
    };
    Ok(Json::object([
        ("frames", Json::Array(list("frames")?)),
        ("stamps", Json::Array(list("stamps")?)),
    ]))
}

/// Faces from `(name, bytes)` pairs (`cjk` names the CJK fallback).
pub fn fonts_from(faces: &[(&str, &[u8])]) -> Result<Fonts, CoreError> {
    let mut fonts = Fonts::new();
    for (name, bytes) in faces {
        fonts.add(name, bytes)?;
    }
    Ok(fonts)
}

/// Faces from one buffer: `index_json` is `[[name, length], ...]` over the concatenated bytes.
pub fn fonts_from_pack(index_json: &str, pack: &[u8]) -> Result<Fonts, CoreError> {
    let index = parse_json(index_json.as_bytes())?;
    let Val::Arr(entries) = index else {
        return Err(bad("字体索引要是列表"));
    };
    let mut fonts = Fonts::new();
    let mut at = 0usize;
    for entry in &entries {
        let (Some(Val::Str(name)), Some(Val::Int(len))) = (entry.get_index(0), entry.get_index(1))
        else {
            return Err(bad("字体索引的每项要是 [名字, 字节数]"));
        };
        // Compare in u64 first, so native (64-bit usize) and wasm32 reject the same lengths with
        // the same text.
        let len = u64::try_from(*len).map_err(|_| bad("字体字节数无效"))?;
        let left = u64::try_from(pack.len() - at).map_err(|_| bad("字体字节数无效"))?;
        if len > left {
            return Err(bad("字体字节数超过数据长度"));
        }
        let end = at + usize::try_from(len).map_err(|_| bad("字体字节数超过数据长度"))?;
        let bytes = pack
            .get(at..end)
            .ok_or_else(|| bad("字体字节数超过数据长度"))?;
        fonts.add(name, bytes)?;
        at = end;
    }
    if at != pack.len() {
        return Err(bad("字体数据有多余的字节"));
    }
    Ok(fonts)
}
