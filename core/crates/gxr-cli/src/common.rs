//! Types and helpers shared by every `gxr-cli` area command.

use gxr_raw::{CoreError, Json};

use crate::npy;

/// A failure: a usage problem (exit 1) or a core error (exit 2).
pub enum Failure {
    Usage(String),
    Core(CoreError),
}

impl From<CoreError> for Failure {
    fn from(e: CoreError) -> Self {
        Self::Core(e)
    }
}

/// Text for stdout, and whether the core accepted the input.
///
/// `ok == false` still prints `text` (the describe path already formatted the error) and the
/// process exits 2.
pub struct Output {
    pub text: String,
    pub ok: bool,
}

/// `--name value` from the arguments after the path.
pub fn option<'a>(args: &'a [String], name: &str) -> Result<&'a str, Failure> {
    let at = args
        .iter()
        .position(|a| a == name)
        .ok_or_else(|| Failure::Usage(format!("missing {name}")))?;
    args.get(at + 1)
        .map(String::as_str)
        .ok_or_else(|| Failure::Usage(format!("{name} needs a value")))
}

/// `A:B` as two integers.
pub fn range(text: &str) -> Result<(i64, i64), Failure> {
    let bad = || Failure::Usage(format!("bad range {text:?}, expected A:B"));
    let (a, b) = text.split_once(':').ok_or_else(bad)?;
    Ok((a.parse().map_err(|_| bad())?, b.parse().map_err(|_| bad())?))
}

/// Python `Dng(path)` reads the file first; `open_dng` turns `OSError` into this text.
pub fn read(path: &str) -> Result<Vec<u8>, Failure> {
    std::fs::read(path).map_err(|e| {
        let text = e.to_string();
        let strerror = text.split(" (os error").next().unwrap_or(&text).to_string();
        Failure::Core(CoreError::raw_decode(format!(
            "读不了这个文件（{strerror}）"
        )))
    })
}

/// The file's base name, as `Path(path).name`.
pub fn base_name(path: &str) -> String {
    std::path::Path::new(path)
        .file_name()
        .map_or_else(String::new, |n| n.to_string_lossy().into_owned())
}

pub fn write_npy<T: npy::NpyElement>(
    out: &str,
    values: &[T],
    shape: &[usize],
    dtype: &str,
) -> Result<Json, Failure> {
    npy::write(out, values, shape)
        .map_err(|e| Failure::Usage(format!("cannot write {out}: {e}")))?;
    let mut dims = Vec::with_capacity(shape.len());
    for &dim in shape {
        let dim = i64::try_from(dim).map_err(|_| {
            Failure::Core(CoreError::raw_decode(
                "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.",
            ))
        })?;
        dims.push(dim);
    }
    Ok(Json::object([
        ("out", Json::str(out)),
        ("dtype", Json::str(dtype)),
        ("shape", Json::ints(&dims)),
    ]))
}
