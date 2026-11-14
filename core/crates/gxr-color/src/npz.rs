//! Reader for `np.savez` output: a zip of stored (uncompressed) `.npy` members.
//!
//! The archive is opened the way CPython's `zipfile` does it, so a damaged file fails where
//! `np.load` fails: the end-of-central-directory record (with its ZIP64 form), then every
//! central-directory record, then, per member, the local header. The CRC-32 of a member is
//! checked when the member is read (`Member::payload`), as `ZipExtFile` does at end of file.
//! Error kind is `zip`; the text is `zipfile.BadZipFile`'s where Python has one.
//!
//! On top of Python's checks, each member's local header must agree with its central record
//! (method, CRC, sizes) and its data must end before the central directory. Deflate members,
//! data descriptors and encryption are refused; numpy never writes them for `np.savez`.

use gxr_raw::CoreError;

use crate::crc::crc32;

fn bad(message: impl Into<String>) -> CoreError {
    CoreError::new("zip", message)
}

const EOCD: &[u8; 4] = b"PK\x05\x06";
const EOCD64: &[u8; 4] = b"PK\x06\x06";
const EOCD64_LOCATOR: &[u8; 4] = b"PK\x06\x07";
const CENTRAL: &[u8; 4] = b"PK\x01\x02";
const LOCAL: &[u8; 4] = b"PK\x03\x04";
const EOCD_LEN: usize = 22;
const EOCD64_LEN: usize = 56;
const LOCATOR_LEN: usize = 20;
const CENTRAL_LEN: usize = 46;
const LOCAL_LEN: usize = 30;
const MAX32: u64 = 0xFFFF_FFFF;

/// One member, located and cross-checked; its bytes are CRC-checked on [`Member::payload`].
#[derive(Clone, Debug)]
pub struct Member {
    /// The name as numpy keys it (`.npy` removed).
    pub key: String,
    name: String,
    data: Vec<u8>,
    crc: u32,
}

impl Member {
    /// The stored bytes, after the CRC-32 check `ZipExtFile` makes.
    pub fn payload(&self) -> Result<&[u8], CoreError> {
        if crc32(&self.data) != self.crc {
            return Err(bad(format!("Bad CRC-32 for file '{}'", self.name)));
        }
        Ok(&self.data)
    }
}

fn u16_at(b: &[u8], at: usize) -> u16 {
    u16::from_le_bytes([b[at], b[at + 1]])
}

fn u32_at(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
}

fn u64_at(b: &[u8], at: usize) -> u64 {
    let mut x = [0u8; 8];
    x.copy_from_slice(&b[at..at + 8]);
    u64::from_le_bytes(x)
}

/// What `_EndRecData` (+ `_EndRecData64`) returns: central directory size and offset, where
/// the end record starts, and whether it was the ZIP64 form.
struct EndRecord {
    size_cd: u64,
    offset_cd: u64,
    location: u64,
    zip64: bool,
}

/// Port of CPython `zipfile._EndRecData`: the record in the last 22 bytes, else the last
/// `rfind` of its signature within the final 64 KiB + 22 bytes.
fn end_record(bytes: &[u8]) -> Result<EndRecord, CoreError> {
    let not_zip = || bad("File is not a zip file");
    let len = bytes.len();
    if len < EOCD_LEN {
        return Err(not_zip());
    }
    let tail = len - EOCD_LEN;
    let at = if &bytes[tail..tail + 4] == EOCD && bytes[len - 2..] == [0, 0] {
        tail
    } else {
        let from = len.saturating_sub((1 << 16) + EOCD_LEN);
        let window = &bytes[from..];
        let start = window
            .windows(4)
            .rposition(|w| w == EOCD)
            .ok_or_else(not_zip)?;
        if window.len() - start < EOCD_LEN {
            return Err(not_zip());
        }
        from + start
    };
    let rec = &bytes[at..at + EOCD_LEN];
    let mut end = EndRecord {
        size_cd: u64::from(u32_at(rec, 12)),
        offset_cd: u64::from(u32_at(rec, 16)),
        location: u64::try_from(at).map_err(|_| not_zip())?,
        zip64: false,
    };
    end_record64(bytes, at, &mut end)?;
    Ok(end)
}

/// Port of CPython `zipfile._EndRecData64`: a ZIP64 locator just before the end record
/// replaces the directory size and offset.
fn end_record64(bytes: &[u8], at: usize, end: &mut EndRecord) -> Result<(), CoreError> {
    let Some(loc_at) = at.checked_sub(LOCATOR_LEN) else {
        return Ok(());
    };
    let loc = &bytes[loc_at..at];
    if &loc[..4] != EOCD64_LOCATOR {
        return Ok(());
    }
    if u32_at(loc, 4) != 0 || u32_at(loc, 16) > 1 {
        return Err(bad("zipfiles that span multiple disks are not supported"));
    }
    let Some(rec_at) = loc_at.checked_sub(EOCD64_LEN) else {
        return Ok(());
    };
    let rec = &bytes[rec_at..loc_at];
    if &rec[..4] != EOCD64 {
        return Ok(());
    }
    end.size_cd = u64_at(rec, 40);
    end.offset_cd = u64_at(rec, 48);
    end.zip64 = true;
    Ok(())
}

/// The ZIP64 extra field (id 1): replaces, in order, each of file size, compressed size and
/// header offset that was stored as `0xFFFFFFFF` (`ZipInfo._decodeExtra`).
fn zip64_extra(extra: &[u8], sizes: &mut [u64; 3]) -> Result<(), CoreError> {
    let mut at = 0;
    while at + 4 <= extra.len() {
        let id = u16_at(extra, at);
        let len = usize::from(u16_at(extra, at + 2));
        let body = extra
            .get(at + 4..at + 4 + len)
            .ok_or_else(|| bad("Corrupt extra field"))?;
        if id == 1 {
            let mut off = 0;
            for (field, value) in ["File size", "Compress size", "Header offset"]
                .iter()
                .zip(sizes.iter_mut())
            {
                if *value == MAX32 {
                    let raw = body.get(off..off + 8).ok_or_else(|| {
                        bad(format!("Corrupt zip64 extra field. {field} not found."))
                    })?;
                    *value = u64_at(raw, 0);
                    off += 8;
                }
            }
        }
        at += 4 + len;
    }
    Ok(())
}

/// A member name as `zipfile` decodes it: UTF-8 when flag 0x800 is set, else cp437. Only
/// ASCII is accepted without the flag (numpy's names are ASCII).
fn member_name(raw: &[u8], flags: u16) -> Result<String, CoreError> {
    if flags & 0x800 != 0 || raw.is_ascii() {
        return String::from_utf8(raw.to_vec()).map_err(|_| bad("zip member name is not UTF-8"));
    }
    Err(bad("zip member name is not ASCII"))
}

/// A central-directory size or offset as an index into the archive.
fn index(v: u64) -> Result<usize, CoreError> {
    usize::try_from(v).map_err(|_| bad("zip entry is too big"))
}

/// Every member of the archive (`zipfile.ZipFile(...)` plus the per-member header checks of
/// `ZipFile.open`), in central-directory order.
pub fn load(bytes: &[u8]) -> Result<Vec<Member>, CoreError> {
    // `np.load` dispatch: a zip (or an empty zip) is an `NpzFile`; a bare `.npy` is an array,
    // which `with ... as z` then rejects; anything else is the pickle path.
    if bytes.starts_with(b"\x93NUMPY") {
        return Err(CoreError::new(
            "type",
            "'numpy.ndarray' object does not support the context manager protocol",
        ));
    }
    if !bytes.starts_with(LOCAL) && !bytes.starts_with(EOCD) {
        return Err(CoreError::new(
            "value",
            "This file contains pickled (object) data. If you trust the file you can load it unsafely using the `allow_pickle=` keyword argument or `pickle.load()`.",
        ));
    }
    let end = end_record(bytes)?;
    // `_RealGetContents`: `concat` is any data prepended to the archive.
    let mut concat = i128::from(end.location) - i128::from(end.size_cd) - i128::from(end.offset_cd);
    if end.zip64 {
        concat -= 76; // sizeEndCentDir64 + sizeEndCentDir64Locator
    }
    let start_dir = i128::from(end.offset_cd) + concat;
    if start_dir < 0 {
        return Err(bad("Bad offset for central directory"));
    }
    let start_dir =
        u64::try_from(start_dir).map_err(|_| bad("Bad offset for central directory"))?;
    let len = u64::try_from(bytes.len()).map_err(|_| bad("zip entry is too big"))?;
    let cd_from = start_dir.min(len);
    let cd_to = start_dir.saturating_add(end.size_cd).min(len);
    let cd = &bytes[index(cd_from)?..index(cd_to)?];
    let mut members = Vec::new();
    let mut total: u64 = 0;
    let mut at = 0usize;
    while total < end.size_cd {
        let rec = cd
            .get(at..at + CENTRAL_LEN)
            .ok_or_else(|| bad("Truncated central directory"))?;
        if &rec[..4] != CENTRAL {
            return Err(bad("Bad magic number for central directory"));
        }
        let flags = u16_at(rec, 8);
        let method = u16_at(rec, 10);
        let crc = u32_at(rec, 16);
        let (name_len, extra_len, comment_len) = (
            usize::from(u16_at(rec, 28)),
            usize::from(u16_at(rec, 30)),
            usize::from(u16_at(rec, 32)),
        );
        let name_raw = cd
            .get(at + CENTRAL_LEN..at + CENTRAL_LEN + name_len)
            .ok_or_else(|| bad("Truncated central directory"))?;
        let extra_at = at + CENTRAL_LEN + name_len;
        // `fp.read(extra_len)` on the directory bytes: a short read keeps what is there.
        let extra = &cd[extra_at.min(cd.len())..(extra_at + extra_len).min(cd.len())];
        let name = member_name(name_raw, flags)?;
        // file size, compressed size, header offset
        let mut sizes = [
            u64::from(u32_at(rec, 24)),
            u64::from(u32_at(rec, 20)),
            u64::from(u32_at(rec, 42)),
        ];
        zip64_extra(extra, &mut sizes)?;
        let header = i128::from(sizes[2]) + concat;
        members.push(member(
            bytes, &name, flags, method, crc, sizes[0], sizes[1], header, start_dir,
        )?);
        let step = CENTRAL_LEN + name_len + extra_len + comment_len;
        total += u64::try_from(step).map_err(|_| bad("Truncated central directory"))?;
        at += step;
    }
    Ok(members)
}

/// One member: the `ZipFile.open` header checks, then this port's agreement checks.
#[allow(clippy::too_many_arguments)]
fn member(
    bytes: &[u8],
    name: &str,
    flags: u16,
    method: u16,
    crc: u32,
    file_size: u64,
    compress_size: u64,
    header: i128,
    start_dir: u64,
) -> Result<Member, CoreError> {
    if flags & 0x1 != 0 {
        return Err(bad(format!(
            "File '{name}' is encrypted, password required for extraction"
        )));
    }
    if method != 0 {
        return Err(bad("compressed zip entries are not supported"));
    }
    if flags & 0x8 != 0 {
        return Err(bad("zip data descriptors are not supported"));
    }
    let header = usize::try_from(header).map_err(|_| bad("Truncated file header"))?;
    let local = bytes
        .get(header..header.saturating_add(LOCAL_LEN))
        .ok_or_else(|| bad("Truncated file header"))?;
    if &local[..4] != LOCAL {
        return Err(bad("Bad magic number for file header"));
    }
    let (name_len, extra_len) = (
        usize::from(u16_at(local, 26)),
        usize::from(u16_at(local, 28)),
    );
    let name_at = header + LOCAL_LEN;
    let local_name = bytes
        .get(name_at..name_at + name_len)
        .ok_or_else(|| bad("Truncated file header"))?;
    if member_name(local_name, u16_at(local, 6)).ok().as_deref() != Some(name) {
        return Err(bad(format!(
            "File name in directory '{name}' and header b'{}' differ.",
            String::from_utf8_lossy(local_name)
        )));
    }
    let extra = bytes
        .get(name_at + name_len..name_at + name_len + extra_len)
        .ok_or_else(|| bad("Truncated file header"))?;
    let mut local_sizes = [
        u64::from(u32_at(local, 22)),
        u64::from(u32_at(local, 18)),
        0,
    ];
    zip64_extra(extra, &mut local_sizes)?;
    if u16_at(local, 8) != method
        || u32_at(local, 14) != crc
        || local_sizes[0] != file_size
        || local_sizes[1] != compress_size
    {
        return Err(bad(format!(
            "zip local header for '{name}' does not match the central directory"
        )));
    }
    if compress_size > MAX32 {
        return Err(bad("zip entry is too big"));
    }
    if file_size != compress_size {
        return Err(bad(format!(
            "stored zip entry '{name}' has different sizes"
        )));
    }
    let data_at =
        u64::try_from(name_at + name_len + extra_len).map_err(|_| bad("zip entry is short"))?;
    let data_end = data_at
        .checked_add(compress_size)
        .ok_or_else(|| bad("zip entry is too big"))?;
    if data_end > start_dir {
        return Err(bad("zip entry is short"));
    }
    let data = crate::size::try_copy(&bytes[index(data_at)?..index(data_end)?])?;
    let key = name.strip_suffix(".npy").unwrap_or(name).to_string();
    Ok(Member {
        key,
        name: name.to_string(),
        data,
        crc,
    })
}

#[cfg(test)]
mod tests {
    use super::load;

    #[test]
    fn rejects_input_that_is_not_an_archive() {
        assert_eq!(
            load(b"PK\x03\x04short").unwrap_err().message,
            "File is not a zip file"
        );
        assert_eq!(load(b"hello").unwrap_err().kind, "value");
    }
}
