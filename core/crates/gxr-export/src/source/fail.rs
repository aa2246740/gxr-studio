//! The Python exceptions the source selection raises and catches, and the labels dict.

use gxr_raw::{CoreError, Json};

/// `finish/serve.py:26 NO_RAW`.
pub const NO_RAW: &str = "配方需要 RAW 文件。拍摄时将画质设为 RAW+JPEG。";

/// Which Python exception a failure stands for. `color_image` catches `NoRaw`,
/// `RenderUnavailable` and `RawDecodeError` (a `CoreError` of kind `raw_decode`) and lets
/// everything else through, so the class matters as much as the text.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Fail {
    /// `render.service.RenderUnavailable(code, message, state)`.
    Unavailable {
        code: String,
        message: String,
        state: &'static str,
    },
    /// `render.service.NoRaw(message)`.
    NoRaw(String),
    /// `render.params.EditError(code, message, detail)`.
    Edit {
        code: String,
        message: String,
        detail: String,
    },
    /// Any other error; `raw_decode` is `RawDecodeError`.
    Core(CoreError),
}

impl From<CoreError> for Fail {
    fn from(e: CoreError) -> Self {
        Self::Core(e)
    }
}

impl Fail {
    pub fn unavailable(code: &str, message: &str, state: &'static str) -> Self {
        Self::Unavailable {
            code: code.to_string(),
            message: message.to_string(),
            state,
        }
    }

    pub fn edit(code: &str, message: &str) -> Self {
        Self::Edit {
            code: code.to_string(),
            message: message.to_string(),
            detail: String::new(),
        }
    }

    /// `isinstance(exc, (NoRaw, RenderUnavailable, RawDecodeError))`.
    pub fn is_caught_by_color_image(&self) -> bool {
        match self {
            Self::Unavailable { .. } | Self::NoRaw(_) => true,
            Self::Core(e) => e.kind == "raw_decode",
            Self::Edit { .. } => false,
        }
    }

    pub fn is_raw_decode(&self) -> bool {
        matches!(self, Self::Core(e) if e.kind == "raw_decode")
    }

    /// `str(exc)`: every one of these exceptions passes its message to `Exception.__init__`.
    pub fn text(&self) -> &str {
        match self {
            Self::Unavailable { message, .. } | Self::NoRaw(message) => message,
            Self::Edit { message, .. } => message,
            Self::Core(e) => &e.message,
        }
    }

    /// `{"error": {...}}`. `kind` and `message` are what the API layer reports; `state`
    /// and `detail` are added where the Python exception carries them.
    pub fn to_json(&self) -> Json {
        let body = match self {
            Self::Unavailable {
                code,
                message,
                state,
            } => Json::object([
                ("kind", Json::str(code.clone())),
                ("message", Json::str(message.clone())),
                ("state", Json::str(*state)),
            ]),
            Self::NoRaw(message) => Json::object([
                ("kind", Json::str("no_raw")),
                ("message", Json::str(message.clone())),
            ]),
            Self::Edit {
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
        };
        Json::object([("error", body)])
    }
}

/// A Python `dict[str, str]` of labels, in insertion order.
#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct Labels(pub Vec<(String, String)>);

impl Labels {
    pub fn of(pairs: &[(&str, &str)]) -> Self {
        Self(
            pairs
                .iter()
                .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
                .collect(),
        )
    }

    pub fn get(&self, key: &str) -> Option<&str> {
        self.0
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
    }

    /// `labels[key] = value`: replaces in place, or appends.
    pub fn set(&mut self, key: &str, value: impl Into<String>) {
        let value = value.into();
        if let Some(slot) = self.0.iter_mut().find(|(k, _)| k == key) {
            slot.1 = value;
        } else {
            self.0.push((key.to_string(), value));
        }
    }

    pub fn to_json(&self) -> Json {
        Json::Object(
            self.0
                .iter()
                .map(|(k, v)| (k.clone(), Json::str(v.clone())))
                .collect(),
        )
    }
}
