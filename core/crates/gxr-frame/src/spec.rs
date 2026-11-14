//! Frame and stamp specs. Port of `gxr_studio/finish/spec.py` parsers.
//!
//! `parse_geometry` stays with the geometry unit. This file ports frame and stamp specs,
//! `stored_frame`, and the factory defaults.

use gxr_raw::{CoreError, Json};

use crate::value::Val;

const INVALID_REQUEST: &str = "请求无效。请刷新页面后重试。";
pub(crate) const STAMP_TEXT_MAX: usize = 24;

const FRAME_STYLES: &[&str] = &[
    "none",
    "leica",
    "blur_fill",
    "gallery",
    "hasselblad",
    "instant",
    "film",
    "thin_keyline",
    "cinema",
    "exhibition_label",
    "slide_mount",
    "darkroom",
    "postage_stamp",
];

const OUTPUT_RATIOS: &[&str] = &["1:1", "4:5", "3:4", "2:3", "9:16", "16:9", "3:2"];

const FRAME_KEYS: &[&str] = &[
    "version",
    "style",
    "color",
    "weight",
    "show_logo",
    "show_params",
    "show_date",
    "text",
    "output_ratio",
    "fmt",
    "table",
    "logo",
    "logo_mode",
    "logo_size",
    "model",
];

/// Port of `gxr_studio/finish/spec.py:70` `FrameSpec`, after `__post_init__`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FrameSpec {
    pub style: String,
    pub color: String,
    pub weight: String,
    pub show_logo: bool,
    pub show_params: bool,
    pub show_date: bool,
    pub text: String,
    pub output_ratio: String,
    pub fmt: String,
    pub table: String,
    pub logo: String,
    pub logo_mode: String,
    pub logo_size: String,
    pub model: bool,
}

impl FrameSpec {
    /// Port of `gxr_studio/finish/spec.py:100` `FrameSpec.to_json`.
    pub fn to_json(&self) -> Json {
        Json::object([
            ("version", Json::Int(2)),
            ("style", Json::str(&self.style)),
            ("color", Json::str(&self.color)),
            ("weight", Json::str(&self.weight)),
            ("show_logo", Json::Bool(self.show_logo)),
            ("show_params", Json::Bool(self.show_params)),
            ("show_date", Json::Bool(self.show_date)),
            ("text", Json::str(&self.text)),
            ("output_ratio", Json::str(&self.output_ratio)),
            ("fmt", Json::str(&self.fmt)),
            ("table", Json::str(&self.table)),
            ("logo", Json::str(&self.logo)),
            ("logo_mode", Json::str(&self.logo_mode)),
            ("logo_size", Json::str(&self.logo_size)),
            ("model", Json::Bool(self.model)),
        ])
    }
}

/// Port of `gxr_studio/finish/spec.py:121` `StampSpec`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StampSpec {
    pub enabled: bool,
    pub content: String,
    pub date_format: String,
    pub show_time: bool,
    pub date_override: Option<String>,
    pub text: String,
    pub font: String,
    pub color: String,
    pub size: String,
    pub corner: String,
    pub glow: bool,
    pub placement: String,
}

impl StampSpec {
    /// Port of `gxr_studio/finish/spec.py:135` `StampSpec.to_json`.
    ///
    /// `placement` is omitted when it is `"photo"`.
    pub fn to_json(&self) -> Json {
        let mut pairs = vec![
            ("enabled".to_string(), Json::Bool(self.enabled)),
            ("content".to_string(), Json::str(&self.content)),
            ("date_format".to_string(), Json::str(&self.date_format)),
            ("show_time".to_string(), Json::Bool(self.show_time)),
            (
                "date_override".to_string(),
                match &self.date_override {
                    Some(text) => Json::str(text),
                    None => Json::Null,
                },
            ),
            ("text".to_string(), Json::str(&self.text)),
            ("font".to_string(), Json::str(&self.font)),
            ("color".to_string(), Json::str(&self.color)),
            ("size".to_string(), Json::str(&self.size)),
            ("corner".to_string(), Json::str(&self.corner)),
            ("glow".to_string(), Json::Bool(self.glow)),
        ];
        if self.placement != "photo" {
            pairs.push(("placement".to_string(), Json::str(&self.placement)));
        }
        Json::object(pairs)
    }
}

/// Port of `gxr_studio/finish/spec.py:352` `factory_frame`.
pub fn factory_frame() -> FrameSpec {
    apply_frame_defaults(RawFrame {
        style: "none".into(),
        color: String::new(),
        weight: "medium".into(),
        show_logo: true,
        show_params: true,
        show_date: None,
        text: String::new(),
        output_ratio: "3:4".into(),
        fmt: String::new(),
        table: "light".into(),
        logo: String::new(),
        logo_mode: "auto".into(),
        logo_size: "medium".into(),
        model: true,
    })
}

/// Port of `gxr_studio/finish/spec.py:356` `factory_stamp`.
pub fn factory_stamp() -> StampSpec {
    StampSpec {
        enabled: false,
        content: "date".into(),
        date_format: "'yy mm dd".into(),
        show_time: false,
        date_override: None,
        text: String::new(),
        font: "seg7".into(),
        color: "orange".into(),
        size: "medium".into(),
        corner: "br".into(),
        glow: true,
        placement: "photo".into(),
    }
}

struct RawFrame {
    style: String,
    color: String,
    weight: String,
    show_logo: bool,
    show_params: bool,
    show_date: Option<bool>,
    text: String,
    output_ratio: String,
    fmt: String,
    table: String,
    logo: String,
    logo_mode: String,
    logo_size: String,
    model: bool,
}

/// Port of `gxr_studio/finish/spec.py:86` `FrameSpec.__post_init__`.
fn apply_frame_defaults(raw: RawFrame) -> FrameSpec {
    let mut color = raw.color;
    if color.is_empty() {
        color = if raw.style == "thin_keyline" || raw.style == "slide_mount" {
            "cream"
        } else {
            "white"
        }
        .into();
    }
    let show_date = raw
        .show_date
        .unwrap_or(raw.style == "leica" || raw.style == "hasselblad");
    let mut fmt = raw.fmt;
    if fmt.is_empty() {
        fmt = if raw.style == "instant" {
            "polaroid"
        } else {
            "135"
        }
        .into();
    }
    let mut logo = raw.logo;
    let mut show_logo = raw.show_logo;
    if logo.is_empty() {
        logo = if show_logo { "ricoh" } else { "none" }.into();
    } else if logo == "none" && show_logo {
        show_logo = false;
    } else if logo != "none" && !show_logo {
        show_logo = true;
    }
    FrameSpec {
        style: raw.style,
        color,
        weight: raw.weight,
        show_logo,
        show_params: raw.show_params,
        show_date,
        text: raw.text,
        output_ratio: raw.output_ratio,
        fmt,
        table: raw.table,
        logo,
        logo_mode: raw.logo_mode,
        logo_size: raw.logo_size,
        model: raw.model,
    }
}

fn field_str(raw: &Val, key: &str, default: &str) -> Result<String, CoreError> {
    match raw.get(key) {
        None => Ok(default.to_string()),
        Some(Val::Str(s)) => Ok(s.clone()),
        Some(_) => Err(CoreError::new("bad_request", format!("{key} 要是字符串"))),
    }
}

fn field_bool(raw: &Val, key: &str, default: bool) -> Result<bool, CoreError> {
    match raw.get(key) {
        None => Ok(default),
        Some(Val::Bool(b)) => Ok(*b),
        Some(_) => Err(CoreError::new("bad_request", format!("{key} 要是布尔值"))),
    }
}

/// `FrameSpec(**fields)`: the dataclass built directly, `__post_init__` included, with no value
/// checks (that is `parse_frame`'s job). `version` is ignored, as `to_json` adds it.
pub fn frame_spec_from_fields(raw: &Val) -> Result<FrameSpec, CoreError> {
    if raw.as_object().is_none() {
        return Err(CoreError::new("bad_request", "边框规格要是对象"));
    }
    let show_date = match raw.get("show_date") {
        None | Some(Val::Null) => None,
        Some(Val::Bool(b)) => Some(*b),
        Some(_) => return Err(CoreError::new("bad_request", "show_date 要是布尔值或 null")),
    };
    Ok(apply_frame_defaults(RawFrame {
        style: field_str(raw, "style", "none")?,
        color: field_str(raw, "color", "")?,
        weight: field_str(raw, "weight", "medium")?,
        show_logo: field_bool(raw, "show_logo", true)?,
        show_params: field_bool(raw, "show_params", true)?,
        show_date,
        text: field_str(raw, "text", "")?,
        output_ratio: field_str(raw, "output_ratio", "3:4")?,
        fmt: field_str(raw, "fmt", "")?,
        table: field_str(raw, "table", "light")?,
        logo: field_str(raw, "logo", "")?,
        logo_mode: field_str(raw, "logo_mode", "auto")?,
        logo_size: field_str(raw, "logo_size", "medium")?,
        model: field_bool(raw, "model", true)?,
    }))
}

/// `StampSpec(**fields)`, built directly with no value checks (`parse_stamp` checks).
pub fn stamp_spec_from_fields(raw: &Val) -> Result<StampSpec, CoreError> {
    if raw.as_object().is_none() {
        return Err(CoreError::new("bad_request", "时间戳规格要是对象"));
    }
    let base = factory_stamp();
    let date_override = match raw.get("date_override") {
        None | Some(Val::Null) => None,
        Some(Val::Str(s)) => Some(s.clone()),
        Some(_) => {
            return Err(CoreError::new(
                "bad_request",
                "date_override 要是字符串或 null",
            ));
        }
    };
    Ok(StampSpec {
        enabled: field_bool(raw, "enabled", base.enabled)?,
        content: field_str(raw, "content", &base.content)?,
        date_format: field_str(raw, "date_format", &base.date_format)?,
        show_time: field_bool(raw, "show_time", base.show_time)?,
        date_override,
        text: field_str(raw, "text", &base.text)?,
        font: field_str(raw, "font", &base.font)?,
        color: field_str(raw, "color", &base.color)?,
        size: field_str(raw, "size", &base.size)?,
        corner: field_str(raw, "corner", &base.corner)?,
        glow: field_bool(raw, "glow", base.glow)?,
        placement: field_str(raw, "placement", &base.placement)?,
    })
}

/// Port of `gxr_studio/finish/spec.py:216` `parse_frame`.
pub fn parse_frame(raw: &Val) -> Result<Option<FrameSpec>, CoreError> {
    if matches!(raw, Val::Null) {
        return Ok(None);
    }
    let Some(_) = raw.as_object() else {
        return Err(edit(
            "bad_frame",
            "边框设置格式不对。请重新选择样式后再试。",
        ));
    };
    if raw.keys().iter().any(|key| !FRAME_KEYS.contains(key))
        || (raw.contains("version") && !version_is_2(raw.get("version")))
    {
        return Err(edit("bad_frame", "边框设置已更新。请重新选择边框样式。"));
    }
    let style = match raw.get("style") {
        None => "none".to_string(),
        Some(Val::Str(text)) if FRAME_STYLES.contains(&text.as_str()) => text.clone(),
        _ => return Err(edit("bad_frame", INVALID_REQUEST)),
    };
    let color = match raw.get("color") {
        None => if style == "thin_keyline" || style == "slide_mount" {
            "cream"
        } else {
            "white"
        }
        .to_string(),
        Some(Val::Str(text)) if matches!(text.as_str(), "white" | "black" | "cream") => {
            text.clone()
        }
        _ => return Err(edit("bad_frame", INVALID_REQUEST)),
    };
    let weight = one_of(raw, "weight", "medium", &["thin", "medium", "thick"])?;
    let ratio = match raw.get("output_ratio") {
        None => "3:4".to_string(),
        Some(Val::Str(text)) if OUTPUT_RATIOS.contains(&text.as_str()) || text == "original" => {
            text.clone()
        }
        _ => return Err(edit("bad_frame", "请选择成品比例后再试。")),
    };
    let text = match raw.get("text") {
        None => String::new(),
        Some(Val::Str(text)) if text.chars().count() <= STAMP_TEXT_MAX => text.clone(),
        _ => return Err(edit("bad_frame", INVALID_REQUEST)),
    };
    for key in ["show_logo", "show_params", "show_date"] {
        if let Some(value) = raw.get(key)
            && !matches!(value, Val::Bool(_))
        {
            return Err(edit("bad_frame", INVALID_REQUEST));
        }
    }
    let fmt_default = if style == "instant" {
        "polaroid"
    } else {
        "135"
    };
    let formats: &[&str] = if style == "instant" {
        &["polaroid", "instax_square", "instax_wide", "instax_mini"]
    } else {
        &["135", "120"]
    };
    let fmt = match raw.get("fmt") {
        None => fmt_default.to_string(),
        Some(Val::Str(text)) if formats.contains(&text.as_str()) => text.clone(),
        _ => return Err(edit("bad_frame", "请选择这个边框支持的画幅。")),
    };
    let table = match raw.get("table") {
        None => "light".to_string(),
        Some(Val::Str(text)) if text == "light" || text == "black" => text.clone(),
        _ => return Err(edit("bad_frame", "胶片背景只能选灯台或黑色。")),
    };
    let (logo, show_logo, logo_mode, logo_size, model) = parse_logo_fields(raw)?;
    let show_params = flag(raw, "show_params", true)?;
    let show_date = flag(raw, "show_date", style == "leica" || style == "hasselblad")?;
    Ok(Some(apply_frame_defaults(RawFrame {
        style,
        color,
        weight,
        show_logo,
        show_params,
        show_date: Some(show_date),
        text,
        output_ratio: ratio,
        fmt,
        table,
        logo,
        logo_mode,
        logo_size,
        model,
    })))
}

/// Port of `gxr_studio/finish/spec.py:192` `_parse_logo_fields`.
fn parse_logo_fields(raw: &Val) -> Result<(String, bool, String, String, bool), CoreError> {
    let mut show_logo = flag(raw, "show_logo", true)?;
    let logo = if !raw.contains("logo") {
        if show_logo { "ricoh" } else { "none" }.to_string()
    } else {
        let Some(Val::Str(raw_logo)) = raw.get("logo") else {
            return Err(edit("bad_frame", INVALID_REQUEST));
        };
        if raw_logo != "ricoh" && raw_logo != "none" && !logo_id(raw_logo) {
            return Err(edit("bad_frame", INVALID_REQUEST));
        }
        if raw_logo == "none"
            || (raw_logo == "ricoh" && matches!(raw.get("show_logo"), Some(Val::Bool(false))))
        {
            show_logo = false;
            "none".to_string()
        } else {
            show_logo = true;
            raw_logo.clone()
        }
    };
    let logo_mode = match raw.get("logo_mode") {
        None => "auto".to_string(),
        Some(Val::Str(text)) if matches!(text.as_str(), "auto" | "orig" | "mono") => text.clone(),
        _ => return Err(edit("bad_frame", INVALID_REQUEST)),
    };
    let logo_size = match raw.get("logo_size") {
        None => "medium".to_string(),
        Some(Val::Str(text)) if matches!(text.as_str(), "small" | "medium" | "large") => {
            text.clone()
        }
        _ => return Err(edit("bad_frame", INVALID_REQUEST)),
    };
    let model = match raw.get("model") {
        None => true,
        Some(Val::Bool(value)) => *value,
        _ => return Err(edit("bad_frame", INVALID_REQUEST)),
    };
    Ok((logo, show_logo, logo_mode, logo_size, model))
}

/// Port of `gxr_studio/finish/spec.py:284` `stored_frame`.
pub fn stored_frame(raw: &Val) -> Result<Option<FrameSpec>, CoreError> {
    if matches!(raw, Val::Null) {
        return Ok(None);
    }
    if raw.as_object().is_none() || !version_is_2(raw.get("version")) {
        return Ok(Some(factory_frame()));
    }
    match parse_frame(raw) {
        Ok(spec) => Ok(spec),
        Err(err) if err.kind == "bad_frame" || err.kind == "bad_stamp" => Ok(Some(factory_frame())),
        Err(err) => Err(err),
    }
}

/// Port of `gxr_studio/finish/spec.py:296` `parse_stamp`.
pub fn parse_stamp(raw: &Val) -> Result<Option<StampSpec>, CoreError> {
    if matches!(raw, Val::Null) {
        return Ok(None);
    }
    if raw.as_object().is_none() {
        return Err(edit("bad_stamp", "时间戳设置格式不对。请重新打开后再试。"));
    }
    let placement = one_of_err(
        raw,
        "placement",
        "photo",
        &["photo", "border"],
        "bad_stamp",
        INVALID_REQUEST,
    )?;
    let content = member(raw, "content", "date", &["date", "text", "date+text"])?;
    let font = member(raw, "font", "seg7", &["seg7", "typewriter", "sans"])?;
    let color = member(
        raw,
        "color",
        "orange",
        &["orange", "red", "yellow", "white"],
    )?;
    let size = member(raw, "size", "medium", &["small", "medium", "large"])?;
    let corner = member(raw, "corner", "br", &["tl", "tr", "bl", "br"])?;
    let text = match raw.get("text") {
        None => String::new(),
        Some(Val::Str(text)) if text.chars().count() <= STAMP_TEXT_MAX => text.clone(),
        _ => return Err(edit("bad_stamp", INVALID_REQUEST)),
    };
    let date_override = match raw.get("date_override") {
        None | Some(Val::Null) => None,
        Some(Val::Str(text)) if crate::py::strptime_ymd(text).is_some() => Some(text.clone()),
        Some(Val::Str(_)) => return Err(edit("bad_stamp", INVALID_REQUEST)),
        _ => {
            return Err(edit(
                "bad_stamp",
                "指定日期要用 YYYY-MM-DD。请重新选择日期后再试。",
            ));
        }
    };
    for key in ["enabled", "show_time", "glow"] {
        if let Some(value) = raw.get(key)
            && !matches!(value, Val::Bool(_))
        {
            return Err(edit("bad_stamp", INVALID_REQUEST));
        }
    }
    let date_format = match raw.get("date_format") {
        None => "'yy mm dd".to_string(),
        Some(Val::Str(text))
            if matches!(
                text.as_str(),
                "'yy mm dd" | "yy mm dd" | "yyyy.mm.dd" | "mm dd 'yy"
            ) =>
        {
            text.clone()
        }
        _ => return Err(edit("bad_stamp", INVALID_REQUEST)),
    };
    Ok(Some(StampSpec {
        enabled: flag(raw, "enabled", false)?,
        content,
        date_format,
        show_time: flag(raw, "show_time", false)?,
        date_override,
        text,
        font,
        color,
        size,
        corner,
        glow: flag(raw, "glow", true)?,
        placement,
    }))
}

fn one_of(raw: &Val, key: &str, default: &str, allowed: &[&str]) -> Result<String, CoreError> {
    one_of_err(raw, key, default, allowed, "bad_frame", INVALID_REQUEST)
}

fn one_of_err(
    raw: &Val,
    key: &str,
    default: &str,
    allowed: &[&str],
    kind: &'static str,
    message: &str,
) -> Result<String, CoreError> {
    match raw.get(key) {
        None => Ok(default.to_string()),
        Some(Val::Str(text)) if allowed.contains(&text.as_str()) => Ok(text.clone()),
        _ => Err(edit(kind, message)),
    }
}

/// `value not in {...}` with no `isinstance` check first, as `parse_stamp` writes it: a list or a
/// dict is unhashable, so Python raises `TypeError` before it can say the value is not allowed.
/// The text is CPython 3.14's.
fn member(raw: &Val, key: &str, default: &str, allowed: &[&str]) -> Result<String, CoreError> {
    match raw.get(key) {
        Some(Val::Arr(_)) => Err(CoreError::new(
            "type_error",
            "cannot use 'list' as a set element (unhashable type: 'list')",
        )),
        Some(Val::Obj(_)) => Err(CoreError::new(
            "type_error",
            "cannot use 'dict' as a set element (unhashable type: 'dict')",
        )),
        _ => one_of_err(raw, key, default, allowed, "bad_stamp", INVALID_REQUEST),
    }
}

fn flag(raw: &Val, key: &str, default: bool) -> Result<bool, CoreError> {
    match raw.get(key) {
        None => Ok(default),
        Some(Val::Bool(value)) => Ok(*value),
        // Callers reject a present non-bool before this. A leftover is the same EditError.
        _ => Err(edit("bad_frame", INVALID_REQUEST)),
    }
}

fn version_is_2(value: Option<&Val>) -> bool {
    matches!(value, Some(Val::Int(2)))
}

/// `_LOGO_ID_RE.match(text)` for `^[a-z0-9_-]{1,64}$`. Python's `$` also matches just before a
/// final newline, so one trailing `\n` is accepted.
fn logo_id(text: &str) -> bool {
    let body = text.strip_suffix('\n').unwrap_or(text);
    let len = body.chars().count();
    (1..=64).contains(&len)
        && body
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
}

fn edit(kind: &'static str, message: &str) -> CoreError {
    CoreError::new(kind, message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::value::parse_json;

    #[test]
    fn empty_stamp_is_the_factory_stamp() {
        let raw = parse_json(b"{}").unwrap();
        let parsed = parse_stamp(&raw).unwrap().unwrap();
        assert_eq!(parsed, factory_stamp());
    }
}
