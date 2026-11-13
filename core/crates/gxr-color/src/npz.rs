//! Reader for `np.savez` output: a zip of stored (uncompressed) `.npy` members.
//!
//! Numpy 2 writes ZIP64 extra fields, so the local header sizes are `0xFFFFFFFF`
//! and the real lengths sit in extra id `0x0001`. Deflate entries are rejected.

use gxr_raw::CoreError;

fn bad(message: impl Into<String>) -> CoreError {
    CoreError::new("zip", message)
}

/// Named `.npy` members, in archive order, still encoded (numpy decodes a member only when
/// it is read). The `.npy` suffix is removed.
pub fn load(bytes: &[u8]) -> Result<Vec<(String, Vec<u8>)>, CoreError> {
    // `np.load` dispatch: a zip (or an empty zip) is an `NpzFile`; a bare `.npy` is an array,
    // which `with ... as z` then rejects; anything else is the pickle path.
    if bytes.starts_with(b"\x93NUMPY") {
        return Err(CoreError::new(
            "type",
            "'numpy.ndarray' object does not support the context manager protocol",
        ));
    }
    if !bytes.starts_with(b"PK\x03\x04") && !bytes.starts_with(b"PK\x05\x06") {
        return Err(CoreError::new(
            "value",
            "This file contains pickled (object) data. If you trust the file you can load it unsafely using the `allow_pickle=` keyword argument or `pickle.load()`.",
        ));
    }
    let mut out = Vec::new();
    let mut at = 0usize;
    while at + 4 <= bytes.len() && &bytes[at..at + 4] == b"PK\x03\x04" {
        let (name, data, next) = local(bytes, at)?;
        if let Some(stem) = name.strip_suffix(".npy") {
            out.push((stem.to_string(), data));
        }
        at = next;
    }
    Ok(out)
}

fn local(bytes: &[u8], at: usize) -> Result<(String, Vec<u8>, usize), CoreError> {
    if bytes.len() < at + 30 {
        return Err(bad("zip header is short"));
    }
    let method = u16::from_le_bytes([bytes[at + 8], bytes[at + 9]]);
    if method != 0 {
        return Err(bad("compressed zip entries are not supported"));
    }
    let flag = u16::from_le_bytes([bytes[at + 6], bytes[at + 7]]);
    if flag & 0x8 != 0 {
        return Err(bad("zip data descriptors are not supported"));
    }
    let mut comp = u32::from_le_bytes([
        bytes[at + 18],
        bytes[at + 19],
        bytes[at + 20],
        bytes[at + 21],
    ]);
    let name_len = usize::from(u16::from_le_bytes([bytes[at + 26], bytes[at + 27]]));
    let extra_len = usize::from(u16::from_le_bytes([bytes[at + 28], bytes[at + 29]]));
    let name_at = at + 30;
    let extra_at = name_at + name_len;
    let data_at = extra_at + extra_len;
    if bytes.len() < data_at {
        return Err(bad("zip header is short"));
    }
    let name = std::str::from_utf8(&bytes[name_at..extra_at])
        .map_err(|_| bad("zip name is not utf-8"))?
        .to_string();
    if comp == u32::MAX {
        comp = zip64_size(&bytes[extra_at..data_at])?;
    }
    let size = usize::try_from(comp).map_err(|_| bad("zip entry is too big"))?;
    let end = data_at
        .checked_add(size)
        .ok_or_else(|| bad("zip entry is too big"))?;
    let data = bytes
        .get(data_at..end)
        .ok_or_else(|| bad("zip entry is short"))?
        .to_vec();
    Ok((name, data, end))
}

/// ZIP64 extra id 1: uncompressed size, then compressed size, when the local header said 0xFFFFFFFF.
fn zip64_size(extra: &[u8]) -> Result<u32, CoreError> {
    let mut at = 0;
    while at + 4 <= extra.len() {
        let id = u16::from_le_bytes([extra[at], extra[at + 1]]);
        let len = usize::from(u16::from_le_bytes([extra[at + 2], extra[at + 3]]));
        let body = extra
            .get(at + 4..at + 4 + len)
            .ok_or_else(|| bad("zip64 extra is short"))?;
        if id == 1 {
            if body.len() < 16 {
                return Err(bad("zip64 extra is short"));
            }
            let comp = u64::from_le_bytes(take8(body, 8)?);
            return u32::try_from(comp).map_err(|_| bad("zip entry is too big"));
        }
        at = at + 4 + len;
    }
    Err(bad("zip64 extra is missing"))
}

fn take8(bytes: &[u8], at: usize) -> Result<[u8; 8], CoreError> {
    let s = bytes
        .get(at..at + 8)
        .ok_or_else(|| bad("zip64 extra is short"))?;
    Ok([s[0], s[1], s[2], s[3], s[4], s[5], s[6], s[7]])
}
