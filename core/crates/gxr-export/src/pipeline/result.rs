//! The export's options, failures, labels and result, as `finish/serve.py:export_finish` and
//! `export.py` (`ExportOptions.validate`, `ExportError`, `ExportResult`, `claim_path`) define them.

use gxr_raw::{CoreError, Json};

/// `export.py ExportError(code, message, detail)`, or a core error passed through.
#[derive(Clone, Debug, PartialEq)]
pub enum ExportFail {
    Export {
        code: String,
        message: String,
        detail: String,
    },
    Core(CoreError),
    /// An error JSON another unit already rendered (export-source's `{"error": {...}}`, with
    /// its `state` or `detail`), passed through unchanged.
    Rendered {
        kind: String,
        json: Json,
    },
}

impl From<CoreError> for ExportFail {
    fn from(e: CoreError) -> Self {
        Self::Core(e)
    }
}

pub(crate) const BAD_REQUEST: &str = "请求无效。请刷新页面后重试。";

impl ExportFail {
    pub fn export(code: &str, message: &str, detail: impl Into<String>) -> Self {
        Self::Export {
            code: code.to_string(),
            message: message.to_string(),
            detail: detail.into(),
        }
    }

    pub fn kind(&self) -> &str {
        match self {
            Self::Export { code, .. } => code,
            Self::Core(e) => e.kind,
            Self::Rendered { kind, .. } => kind,
        }
    }

    /// `{"error": {"kind", "message"[, "detail"]}}`.
    pub fn to_json(&self) -> Json {
        if let Self::Rendered { json, .. } = self {
            return json.clone();
        }
        let body = match self {
            Self::Export {
                code,
                message,
                detail,
            } => Json::object([
                ("kind", Json::str(code.clone())),
                ("message", Json::str(message.clone())),
                ("detail", Json::str(detail.clone())),
            ]),
            Self::Core(e) => Json::object([
                ("kind", Json::str(e.kind)),
                ("message", Json::str(e.message.clone())),
            ]),
            Self::Rendered { json, .. } => return json.clone(),
        };
        Json::object([("error", body)])
    }
}

/// Port of `export.py:63 ExportOptions.validate` for the phone: the format is always JPEG
/// (PNG is out of scope), so only `quality` and `long_edge` are checked. Both arrive as
/// JSON numbers or `f64`; a non-integer is rejected as Python's `isinstance(..., int)` would
/// reject a float.
pub fn validate_options(
    quality: f64,
    long_edge: Option<f64>,
) -> Result<(i64, Option<i64>), ExportFail> {
    let q = int_in(quality, 1, 100).ok_or_else(|| {
        ExportFail::export("bad_quality", BAD_REQUEST, "品质要在 1 到 100 之间。")
    })?;
    let edge = match long_edge {
        None => None,
        Some(e) => Some(int_in(e, 64, 30000).ok_or_else(|| {
            ExportFail::export("bad_size", BAD_REQUEST, "长边要在 64 到 30000 之间。")
        })?),
    };
    Ok((q, edge))
}

fn int_in(v: f64, lo: i64, hi: i64) -> Option<i64> {
    if !v.is_finite() || v.fract() != 0.0 || v < lo as f64 || v > hi as f64 {
        return None;
    }
    Some(v as i64)
}

/// Port of the name rule in `export.py:337 claim_path`: `stem_gxr{tag}.jpg` for the first
/// free slot `n == 1`, `stem_gxr{tag}-{n}.jpg` after that. The phone saves through the share
/// sheet, so only the rule is ported, not the filesystem probe.
pub fn file_name(stem: &str, tag: &str, n: u32) -> String {
    if n <= 1 {
        format!("{stem}_gxr{tag}.jpg")
    } else {
        format!("{stem}_gxr{tag}-{n}.jpg")
    }
}

/// A Python `dict[str, str]` of labels, in insertion order.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Labels(pub Vec<(String, String)>);

impl Labels {
    pub fn get(&self, key: &str) -> Option<&str> {
        self.0
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
    }

    /// `labels.get(key) or ""`.
    fn or_empty(&self, key: &str) -> &str {
        self.get(key).unwrap_or("")
    }

    pub fn to_json(&self) -> Json {
        Json::Object(
            self.0
                .iter()
                .map(|(k, v)| (k.clone(), Json::str(v.clone())))
                .collect(),
        )
    }

    pub fn from_val(v: &gxr_frame::Val) -> Result<Self, CoreError> {
        let Some(pairs) = v.as_object() else {
            return Err(CoreError::new("bad_request", "labels 要是对象"));
        };
        let mut out: Vec<(String, String)> = Vec::new();
        for (k, v) in pairs {
            let gxr_frame::Val::Str(s) = v else {
                return Err(CoreError::new("bad_request", "labels 的值要是字符串"));
            };
            if let Some(slot) = out.iter_mut().find(|(key, _)| key == k) {
                slot.1 = s.clone();
            } else {
                out.push((k.clone(), s.clone()));
            }
        }
        Ok(Self(out))
    }

    /// `export_finish`'s `note`: the EXIF ImageDescription.
    pub fn note(&self) -> &str {
        let d = self.or_empty("description");
        if !d.is_empty() {
            return d;
        }
        let t = self.or_empty("text");
        if t.is_empty() { "原图" } else { t }
    }
}

/// The labels-derived fields of `export_finish`'s `ExportResult`, in its order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResultLabels {
    pub render: String,
    pub render_label: String,
    pub render_kind: String,
    pub calibrated: Option<bool>,
    pub detail: String,
    pub tag: String,
}

/// Port of `finish/serve.py:507-512` (`kind`, `ident`, `text`, `tag`, `calibrated`).
pub fn result_labels(labels: &Labels) -> ResultLabels {
    let label = labels.or_empty("label");
    let kind = if !label.is_empty() {
        label
    } else {
        let k = labels.or_empty("kind");
        if k.is_empty() { "original" } else { k }
    };
    let id = labels.or_empty("id");
    let ident = if !id.is_empty() {
        id
    } else if kind == "original" {
        "camera-jpg"
    } else {
        "recipe-prediction"
    };
    let text = labels.or_empty("text");
    let calibrated = match labels.get("calibrated") {
        Some("1") => Some(true),
        Some("0") => Some(false),
        _ => None,
    };
    ResultLabels {
        render: ident.to_string(),
        render_label: if text.is_empty() { "原图" } else { text }.to_string(),
        render_kind: kind.to_string(),
        calibrated,
        detail: labels.or_empty("detail").to_string(),
        tag: labels.or_empty("tag").to_string(),
    }
}

/// HDR outcome: `None` when HDR was not asked for.
#[derive(Clone, Debug, PartialEq)]
pub struct HdrOutcome {
    pub hdr: bool,
    pub reason: Option<String>,
}

/// `ExportResult` as JSON, without `path` (the phone has none). `tag` is the file-name tag
/// after `_hdr` is added, and `file_name` the first name `claim_path` would try.
pub fn result_json(
    stem: &str,
    width: usize,
    height: usize,
    bytes: usize,
    rl: &ResultLabels,
    hdr: Option<&HdrOutcome>,
) -> Json {
    let mut tag = rl.tag.clone();
    if hdr.is_some_and(|h| h.hdr) {
        tag.push_str("_hdr");
    }
    let int = |n: usize| Json::Int(i64::try_from(n).unwrap_or(i64::MAX));
    Json::object([
        ("width", int(width)),
        ("height", int(height)),
        ("bytes", int(bytes)),
        ("render", Json::str(rl.render.clone())),
        ("render_label", Json::str(rl.render_label.clone())),
        ("format", Json::str("jpeg")),
        ("render_kind", Json::str(rl.render_kind.clone())),
        ("calibrated", rl.calibrated.map_or(Json::Null, Json::Bool)),
        ("detail", Json::str(rl.detail.clone())),
        ("hdr", hdr.map_or(Json::Null, |h| Json::Bool(h.hdr))),
        (
            "hdr_reason",
            hdr.and_then(|h| h.reason.clone())
                .map_or(Json::Null, Json::Str),
        ),
        ("recipe_binding", Json::Null),
        ("file_name", Json::str(file_name(stem, &tag, 1))),
        ("tag", Json::str(tag)),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_follow_claim_path() {
        assert_eq!(file_name("R0001", "", 1), "R0001_gxr.jpg");
        assert_eq!(
            file_name("R0001", "_neutral_hdr", 2),
            "R0001_gxr_neutral_hdr-2.jpg"
        );
    }

    #[test]
    fn options_reject_floats_and_ranges() {
        assert!(validate_options(95.0, None).is_ok());
        assert_eq!(
            validate_options(95.5, None).unwrap_err().kind(),
            "bad_quality"
        );
        assert_eq!(
            validate_options(0.0, None).unwrap_err().kind(),
            "bad_quality"
        );
        assert_eq!(
            validate_options(f64::NAN, None).unwrap_err().kind(),
            "bad_quality"
        );
        assert_eq!(
            validate_options(95.0, Some(63.0)).unwrap_err().kind(),
            "bad_size"
        );
        assert_eq!(
            validate_options(95.0, Some(30001.0)).unwrap_err().kind(),
            "bad_size"
        );
        assert_eq!(validate_options(95.0, Some(64.0)).unwrap().1, Some(64));
    }
}
