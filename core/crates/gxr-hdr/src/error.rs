//! HDR errors. `kind` is `HdrUnsupported.reason`, or `"container"` for `ContainerError`.

use gxr_raw::CoreError;

/// Port of gxr_studio/hdr/gain.py:46 HdrUnsupported.
///
/// `kind` is the machine `reason`. `message` is `detail or reason`, which is `str(exc)`.
pub fn unsupported(reason: &'static str, detail: &str) -> CoreError {
    let message = if detail.is_empty() {
        reason.to_string()
    } else {
        detail.to_string()
    };
    CoreError::new(reason, message)
}

/// Port of gxr_studio/hdr/container.py:31 ContainerError.
pub fn container(message: impl Into<String>) -> CoreError {
    CoreError::new("container", message)
}
