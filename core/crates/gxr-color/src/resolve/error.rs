//! Error types for edit parsing and recipe resolution.

use gxr_raw::{CoreError, Json, error_json};

pub const INVALID_REQUEST: &str = "请求无效。请刷新页面后重试。";

/// Port of `gxr_studio/render/params.py:29 EditError`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EditError {
    pub code: &'static str,
    pub message: String,
    pub detail: String,
}

impl EditError {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            detail: String::new(),
        }
    }

    pub fn with_detail(
        code: &'static str,
        message: impl Into<String>,
        detail: impl Into<String>,
    ) -> Self {
        Self {
            code,
            message: message.into(),
            detail: detail.into(),
        }
    }

    pub fn to_json(&self) -> Json {
        Json::object([(
            "error",
            Json::object([
                ("kind", Json::str(self.code)),
                ("message", Json::str(self.message.clone())),
                ("detail", Json::str(self.detail.clone())),
            ]),
        )])
    }
}

/// Either a structured `EditError` or a core error (JSON, camdata, knobs).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ResolveError {
    Edit(EditError),
    Core(CoreError),
}

impl ResolveError {
    pub fn to_json(&self) -> Json {
        match self {
            Self::Edit(e) => e.to_json(),
            Self::Core(e) => error_json(e),
        }
    }

    pub fn into_core_error(self) -> CoreError {
        match self {
            Self::Edit(e) => CoreError::new(e.code, e.message),
            Self::Core(e) => e,
        }
    }
}

impl From<EditError> for ResolveError {
    fn from(value: EditError) -> Self {
        Self::Edit(value)
    }
}

impl From<CoreError> for ResolveError {
    fn from(value: CoreError) -> Self {
        Self::Core(value)
    }
}
