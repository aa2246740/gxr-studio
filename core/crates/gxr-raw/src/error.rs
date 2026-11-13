//! Errors of the core, and the Python exception they stand for.

use std::fmt;

/// An error the core returns instead of panicking.
///
/// `kind` is a stable string: `"raw_decode"` where Python raises `RawDecodeError`, `"scene"` for
/// `lut.scene.SceneError`. `message` is the Python message, character for character (the UI shows
/// it).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CoreError {
    pub kind: &'static str,
    pub message: String,
}

impl CoreError {
    pub fn new(kind: &'static str, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
        }
    }

    /// Python `RawDecodeError(message)`.
    pub fn raw_decode(message: impl Into<String>) -> Self {
        Self::new("raw_decode", message)
    }
}

impl fmt::Display for CoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.kind, self.message)
    }
}

impl std::error::Error for CoreError {}

/// The Python exception a step of `Dng.__init__` would raise. `open_dng` turns it into the
/// `RawDecodeError` text Python shows; `mosaic` wraps value and index errors its own way.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum PyErr {
    Value(String),
    NotImplemented(String),
    Type(String),
    Index(String),
    /// Python lets `OverflowError` escape `open_dng`; there is no `RawDecodeError` text for it.
    Overflow(String),
    /// `RawDecodeError` raised inside `Dng.__init__` (re-raised by `open_dng`).
    #[allow(dead_code)]
    RawDecode(String),
}

impl PyErr {
    /// `str(exc)`.
    pub(crate) fn text(&self) -> &str {
        match self {
            Self::Value(m)
            | Self::NotImplemented(m)
            | Self::Type(m)
            | Self::Index(m)
            | Self::Overflow(m)
            | Self::RawDecode(m) => m,
        }
    }

    /// `type(exc).__name__`.
    pub(crate) fn type_name(&self) -> &'static str {
        match self {
            Self::Value(_) => "ValueError",
            Self::NotImplemented(_) => "NotImplementedError",
            Self::Type(_) => "TypeError",
            Self::Index(_) => "IndexError",
            Self::Overflow(_) => "OverflowError",
            Self::RawDecode(_) => "RawDecodeError",
        }
    }

    /// Port of gxr_studio/raw/dng.py:305 open_dng (the `except` clauses).
    pub(crate) fn into_open_error(self) -> CoreError {
        match self {
            Self::RawDecode(m) => CoreError::raw_decode(m),
            Self::NotImplemented(m) => {
                CoreError::raw_decode(format!("这份 RAW 的写法暂不支持（{m}）"))
            }
            Self::Overflow(m) => CoreError::new("unexpected", format!("OverflowError: {m}")),
            other => CoreError::raw_decode(format!(
                "不是可识别的 GXR DNG（{}: {}）",
                other.type_name(),
                other.text()
            )),
        }
    }
}

pub(crate) type PyResult<T> = Result<T, PyErr>;
