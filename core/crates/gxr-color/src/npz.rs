//! Reader for `np.savez` output, as `np.load` reads it through CPython 3.14's `zipfile`.
//!
//! Opening ([`load`]) is `_EndRecData` / `_EndRecData64` and `_RealGetContents`: the end record
//! (with its ZIP64 form) and every central-directory record, eagerly. Reading a member
//! ([`Archive::read`]) is `NpzFile.__getitem__`, `ZipFile.open` and `ZipExtFile` for a stored
//! member: the local header is checked only then (signature, name, overlap), and the payload
//! size and CRC-32 come from the central directory, so data descriptors need nothing special.
//! Texts are `zipfile`'s; kinds map the Python exception (`BadZipFile` → `zip`,
//! `NotImplementedError` → `not_implemented`, `RuntimeError` → `runtime`, `EOFError` → `eof`,
//! `ValueError` → `value`, `KeyError` → `key`).
//!
//! Not as Python: compressed members are refused (`zip`, no inflate dependency), and the
//! CRC-32 covers the whole stored payload, which is what numpy reads for the small members it
//! writes.

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
const EOCD64_LEN: i128 = 56;
const LOCATOR_LEN: i128 = 20;
const CENTRAL_LEN: usize = 46;
const LOCAL_LEN: usize = 30;
const MAX_COMMENT: usize = (1 << 16) - 1;
const MAX_EXTRACT_VERSION: u8 = 63;
const MAX32: u64 = 0xFFFF_FFFF;

const MASK_ENCRYPTED: u16 = 1 << 0;
const MASK_COMPRESSED_PATCH: u16 = 1 << 5;
const MASK_STRONG_ENCRYPTION: u16 = 1 << 6;
const MASK_UTF_FILENAME: u16 = 1 << 11;

/// cp437 for bytes 0x80..=0xFF (`bytes(range(128, 256)).decode("cp437")`).
const CP437_HIGH: &str = "\u{00c7}\u{00fc}\u{00e9}\u{00e2}\u{00e4}\u{00e0}\u{00e5}\u{00e7}\u{00ea}\u{00eb}\u{00e8}\u{00ef}\u{00ee}\u{00ec}\u{00c4}\u{00c5}\u{00c9}\u{00e6}\u{00c6}\u{00f4}\u{00f6}\u{00f2}\u{00fb}\u{00f9}\u{00ff}\u{00d6}\u{00dc}\u{00a2}\u{00a3}\u{00a5}\u{20a7}\u{0192}\u{00e1}\u{00ed}\u{00f3}\u{00fa}\u{00f1}\u{00d1}\u{00aa}\u{00ba}\u{00bf}\u{2310}\u{00ac}\u{00bd}\u{00bc}\u{00a1}\u{00ab}\u{00bb}\u{2591}\u{2592}\u{2593}\u{2502}\u{2524}\u{2561}\u{2562}\u{2556}\u{2555}\u{2563}\u{2551}\u{2557}\u{255d}\u{255c}\u{255b}\u{2510}\u{2514}\u{2534}\u{252c}\u{251c}\u{2500}\u{253c}\u{255e}\u{255f}\u{255a}\u{2554}\u{2569}\u{2566}\u{2560}\u{2550}\u{256c}\u{2567}\u{2568}\u{2564}\u{2565}\u{2559}\u{2558}\u{2552}\u{2553}\u{256b}\u{256a}\u{2518}\u{250c}\u{2588}\u{2584}\u{258c}\u{2590}\u{2580}\u{03b1}\u{00df}\u{0393}\u{03c0}\u{03a3}\u{03c3}\u{00b5}\u{03c4}\u{03a6}\u{0398}\u{03a9}\u{03b4}\u{221e}\u{03c6}\u{03b5}\u{2229}\u{2261}\u{00b1}\u{2265}\u{2264}\u{2320}\u{2321}\u{00f7}\u{2248}\u{00b0}\u{2219}\u{00b7}\u{221a}\u{207f}\u{00b2}\u{25a0}\u{00a0}";

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

/// `bytes[from .. from + n]` the way a seek-then-read returns it: short (or empty) past the
/// end, never an error.
fn read_at(bytes: &[u8], from: i128, n: usize) -> &[u8] {
    let len = bytes.len();
    let Ok(start) = usize::try_from(from) else {
        return &[];
    };
    let start = start.min(len);
    &bytes[start..start.saturating_add(n).min(len)]
}

fn len_i128(bytes: &[u8]) -> i128 {
    i128::try_from(bytes.len()).unwrap_or(i128::MAX)
}

/// Python's `str.__repr__` for the names `zipfile` quotes with `%r`.
fn py_repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if u32::from(c) < 0x20 || (0x7f..0xa0).contains(&u32::from(c)) => {
                out.push_str(&format!("\\x{:02x}", u32::from(c)));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Python's `bytes.__repr__`.
fn py_repr_bytes(b: &[u8]) -> String {
    let quote = if b.contains(&b'\'') && !b.contains(&b'"') {
        b'"'
    } else {
        b'\''
    };
    let mut out = String::from("b");
    out.push(char::from(quote));
    for &c in b {
        match c {
            b'\\' => out.push_str("\\\\"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            b'\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(char::from(c));
            }
            0x20..=0x7e => out.push(char::from(c)),
            c => out.push_str(&format!("\\x{c:02x}")),
        }
    }
    out.push(char::from(quote));
    out
}

const INVALID_START: &str = "invalid start byte";
const INVALID_CONTINUATION: &str = "invalid continuation byte";
const UNEXPECTED_END: &str = "unexpected end of data";

fn is_continuation(b: u8) -> bool {
    (0x80..=0xBF).contains(&b)
}

/// The first error of a strict UTF-8 decode, as CPython's decoder reports it
/// (`stringlib/codecs.h` `utf8_decode`, then `unicode_decode_utf8`): the error range
/// `[start, end)` and its reason. `None` when the bytes are valid UTF-8.
fn utf8_error(raw: &[u8]) -> Option<(usize, usize, &'static str)> {
    let n = raw.len();
    let mut s = 0;
    while s < n {
        let ch = raw[s];
        if ch < 0x80 {
            s += 1;
            continue;
        }
        let left = n - s;
        let start = Some((s, s + 1, INVALID_START));
        let cont = |len: usize| Some((s, s + len, INVALID_CONTINUATION));
        let end = Some((s, n, UNEXPECTED_END));
        if ch < 0xE0 {
            if ch < 0xC2 {
                return start;
            }
            if left < 2 {
                return end;
            }
            if !is_continuation(raw[s + 1]) {
                return cont(1);
            }
            s += 2;
        } else if ch < 0xF0 {
            if left < 3 {
                if left < 2 {
                    return end;
                }
                let ch2 = raw[s + 1];
                if !is_continuation(ch2) || (if ch2 < 0xA0 { ch == 0xE0 } else { ch == 0xED }) {
                    return cont(1);
                }
                return end;
            }
            let (ch2, ch3) = (raw[s + 1], raw[s + 2]);
            if !is_continuation(ch2) || (ch == 0xE0 && ch2 < 0xA0) || (ch == 0xED && ch2 >= 0xA0) {
                return cont(1);
            }
            if !is_continuation(ch3) {
                return cont(2);
            }
            s += 3;
        } else if ch < 0xF5 {
            if left < 4 {
                if left < 2 {
                    return end;
                }
                let ch2 = raw[s + 1];
                if !is_continuation(ch2) || (if ch2 < 0x90 { ch == 0xF0 } else { ch == 0xF4 }) {
                    return cont(1);
                }
                if left < 3 {
                    return end;
                }
                if !is_continuation(raw[s + 2]) {
                    return cont(2);
                }
                return end;
            }
            let (ch2, ch3, ch4) = (raw[s + 1], raw[s + 2], raw[s + 3]);
            if !is_continuation(ch2) || (ch == 0xF0 && ch2 < 0x90) || (ch == 0xF4 && ch2 >= 0x90) {
                return cont(1);
            }
            if !is_continuation(ch3) {
                return cont(2);
            }
            if !is_continuation(ch4) {
                return cont(3);
            }
            s += 4;
        } else {
            return start;
        }
    }
    None
}

/// `bytes.decode("utf-8")`, failing with `UnicodeDecodeError.__str__`'s text.
fn decode_utf8(raw: &[u8]) -> Result<String, CoreError> {
    if let Some((start, end, reason)) = utf8_error(raw) {
        let message = if end == start + 1 {
            format!(
                "'utf-8' codec can't decode byte 0x{:02x} in position {start}: {reason}",
                raw[start]
            )
        } else {
            format!(
                "'utf-8' codec can't decode bytes in position {start}-{}: {reason}",
                end - 1
            )
        };
        return Err(CoreError::new("unicode", message));
    }
    String::from_utf8(raw.to_vec())
        .map_err(|_| CoreError::new("unicode", "'utf-8' codec can't decode bytes"))
}

/// A name decoded the way `zipfile` does: UTF-8 with flag bit 11, else cp437.
fn decode_name(raw: &[u8], flags: u16) -> Result<String, CoreError> {
    if flags & MASK_UTF_FILENAME != 0 {
        return decode_utf8(raw);
    }
    let high: Vec<char> = CP437_HIGH.chars().collect();
    Ok(raw
        .iter()
        .map(|&c| {
            if c < 0x80 {
                char::from(c)
            } else {
                high[usize::from(c - 0x80)]
            }
        })
        .collect())
}

/// `_sanitize_filename` on POSIX: the name ends at its first NUL.
fn sanitize(name: &str) -> String {
    name.split('\0').next().unwrap_or("").to_string()
}

/// One central-directory record (`ZipInfo` after `_decodeExtra`).
#[derive(Clone, Debug)]
struct Info {
    filename: String,
    orig_filename: String,
    flags: u16,
    method: u16,
    crc: u32,
    compress_size: u64,
    file_size: u64,
    header_offset: i128,
    end_offset: i128,
}

/// The `np.load` result for a zip: its central directory, read; members are read lazily.
#[derive(Clone, Debug)]
pub struct Archive {
    bytes: Vec<u8>,
    infos: Vec<Info>,
}

/// What `_EndRecData` returns: the central directory's size and offset and where the end
/// record starts (moved to the ZIP64 record when there is one).
struct EndRecord {
    size_cd: u64,
    offset_cd: u64,
    location: i128,
}

/// Port of CPython 3.14 `zipfile._EndRecData`. `None` means "File is not a zip file".
fn end_record(bytes: &[u8]) -> Result<Option<EndRecord>, CoreError> {
    let len = bytes.len();
    if len < EOCD_LEN {
        return Ok(None);
    }
    let tail = len - EOCD_LEN;
    let at = if &bytes[tail..tail + 4] == EOCD && bytes[len - 2..] == [0, 0] {
        tail
    } else {
        let from = len.saturating_sub(MAX_COMMENT + EOCD_LEN);
        let window = &bytes[from..];
        let Some(start) = window.windows(4).rposition(|w| w == EOCD) else {
            return Ok(None);
        };
        if window.len() - start < EOCD_LEN {
            return Ok(None);
        }
        from + start
    };
    let rec = &bytes[at..at + EOCD_LEN];
    let end = EndRecord {
        size_cd: u64::from(u32_at(rec, 12)),
        offset_cd: u64::from(u32_at(rec, 16)),
        location: i128::try_from(at).unwrap_or(i128::MAX),
    };
    end_record64(bytes, end)
}

/// Port of CPython 3.14 `zipfile._EndRecData64`. A short read is `OSError`, which
/// `_RealGetContents` turns into "File is not a zip file" (`Ok(None)` here).
fn end_record64(bytes: &[u8], mut end: EndRecord) -> Result<Option<EndRecord>, CoreError> {
    let mut offset = end.location - LOCATOR_LEN;
    if offset < 0 {
        return Ok(Some(end));
    }
    let loc = read_at(bytes, offset, 20);
    if loc.len() != 20 {
        return Ok(None);
    }
    if &loc[..4] != EOCD64_LOCATOR {
        return Ok(Some(end));
    }
    let (diskno, reloff, disks) = (u32_at(loc, 4), i128::from(u64_at(loc, 8)), u32_at(loc, 16));
    if diskno != 0 || disks > 1 {
        return Err(bad("zipfiles that span multiple disks are not supported"));
    }
    offset -= EOCD64_LEN;
    if reloff > offset {
        return Err(bad("Corrupt zip64 end of central directory locator"));
    }
    let mut extrasz = offset - reloff;
    let mut data = read_at(bytes, reloff, 56);
    if data.len() != 56 {
        return Ok(None);
    }
    if !data.starts_with(EOCD64) && reloff != offset {
        extrasz = 0;
        data = read_at(bytes, offset, 56);
        if data.len() != 56 {
            return Ok(None);
        }
    }
    if !data.starts_with(EOCD64) {
        return Err(bad("Zip64 end of central directory record not found"));
    }
    let sz = i128::from(u64_at(data, 4));
    let (dirsize, diroffset) = (u64_at(data, 40), u64_at(data, 48));
    if i128::from(diroffset) + i128::from(dirsize) != reloff || sz + 12 != EOCD64_LEN + extrasz {
        return Err(bad("Corrupt zip64 end of central directory record"));
    }
    end.size_cd = dirsize;
    end.offset_cd = diroffset;
    end.location = offset - extrasz;
    Ok(Some(end))
}

/// Port of `ZipInfo._decodeExtra`: ZIP64 sizes (id 1) and the Unicode path field (0x7075).
fn decode_extra(info: &mut Info, mut extra: &[u8], filename_crc: u32) -> Result<(), CoreError> {
    while extra.len() >= 4 {
        let (tp, ln) = (u16_at(extra, 0), usize::from(u16_at(extra, 2)));
        if ln + 4 > extra.len() {
            return Err(bad(format!("Corrupt extra field {tp:04x} (size={ln})")));
        }
        let mut data = &extra[4..ln + 4];
        if tp == 0x0001 {
            let mut take = |field: &str| -> Result<u64, CoreError> {
                let raw = data
                    .get(..8)
                    .ok_or_else(|| bad(format!("Corrupt zip64 extra field. {field} not found.")))?;
                let v = u64_at(raw, 0);
                data = &data[8..];
                Ok(v)
            };
            if info.file_size == u64::MAX || info.file_size == MAX32 {
                info.file_size = take("File size")?;
            }
            if info.compress_size == MAX32 {
                info.compress_size = take("Compress size")?;
            }
            if info.header_offset == i128::from(MAX32) {
                info.header_offset = i128::from(take("Header offset")?);
            }
        } else if tp == 0x7075 {
            if data.len() < 5 {
                return Err(bad("Corrupt unicode path extra field (0x7075)"));
            }
            if data[0] == 1 && u32_at(data, 1) == filename_crc {
                let name = std::str::from_utf8(&data[5..]).map_err(|_| {
                    bad("Corrupt unicode path extra field (0x7075): invalid utf-8 bytes")
                })?;
                if !name.is_empty() {
                    info.filename = sanitize(name);
                }
            }
        }
        extra = &extra[ln + 4..];
    }
    Ok(())
}

/// `np.load` on archive bytes: the dispatch on the first bytes, then `ZipFile(...)`.
pub fn load(bytes: &[u8]) -> Result<Archive, CoreError> {
    // A zip (or an empty zip) is an `NpzFile`; a bare `.npy` is an array, which
    // `with ... as z` then rejects; anything else is the pickle path.
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
    let infos = central_directory(bytes)?;
    Ok(Archive {
        bytes: crate::size::try_copy(bytes)?,
        infos,
    })
}

/// Port of `ZipFile._RealGetContents`.
fn central_directory(bytes: &[u8]) -> Result<Vec<Info>, CoreError> {
    let end = end_record(bytes)?.ok_or_else(|| bad("File is not a zip file"))?;
    let concat = end.location - i128::from(end.size_cd) - i128::from(end.offset_cd);
    let start_dir = i128::from(end.offset_cd) + concat;
    if start_dir < 0 {
        return Err(bad("Bad offset for central directory"));
    }
    let cd = read_at(
        bytes,
        start_dir,
        usize::try_from(end.size_cd).unwrap_or(usize::MAX),
    );
    // `io.BytesIO(data)`: every read advances by what it actually returned.
    let mut pos = 0usize;
    let mut read = |n: usize| -> Vec<u8> {
        let from = pos.min(cd.len());
        let to = from.saturating_add(n).min(cd.len());
        pos = to;
        cd[from..to].to_vec()
    };
    let mut infos: Vec<Info> = Vec::new();
    let mut total: u64 = 0;
    while total < end.size_cd {
        let rec = read(CENTRAL_LEN);
        if rec.len() != CENTRAL_LEN {
            return Err(bad("Truncated central directory"));
        }
        if &rec[..4] != CENTRAL {
            return Err(bad("Bad magic number for central directory"));
        }
        let (name_len, extra_len, comment_len) =
            (u16_at(&rec, 28), u16_at(&rec, 30), u16_at(&rec, 32));
        let raw_name = read(usize::from(name_len));
        let flags = u16_at(&rec, 8);
        let decoded = decode_name(&raw_name, flags)?;
        let extra = read(usize::from(extra_len));
        let _comment = read(usize::from(comment_len));
        if rec[6] > MAX_EXTRACT_VERSION {
            return Err(CoreError::new(
                "not_implemented",
                format!("zip file version {:.1}", f64::from(rec[6]) / 10.0),
            ));
        }
        let mut info = Info {
            filename: sanitize(&decoded),
            orig_filename: decoded,
            flags,
            method: u16_at(&rec, 10),
            crc: u32_at(&rec, 16),
            compress_size: u64::from(u32_at(&rec, 20)),
            file_size: u64::from(u32_at(&rec, 24)),
            header_offset: i128::from(u32_at(&rec, 42)),
            end_offset: start_dir,
        };
        decode_extra(&mut info, &extra, crc32(&raw_name))?;
        info.header_offset += concat;
        infos.push(info);
        total += 46 + u64::from(name_len) + u64::from(extra_len) + u64::from(comment_len);
    }
    // `_end_offset`: each member ends where the next one (by header offset) starts.
    let mut order: Vec<usize> = (0..infos.len()).collect();
    order.sort_by_key(|&i| infos[i].header_offset);
    let mut end_offset = start_dir;
    for &i in order.iter().rev() {
        infos[i].end_offset = end_offset;
        end_offset = infos[i].header_offset;
    }
    Ok(infos)
}

impl Archive {
    /// `NpzFile._files[key]`: names without `.npy` map to their full name (a later member
    /// wins), then every full name maps to itself.
    fn full_name(&self, key: &str) -> Option<String> {
        if self.infos.iter().any(|i| i.filename == key) {
            return Some(key.to_string());
        }
        self.infos
            .iter()
            .rev()
            .find(|i| i.filename.strip_suffix(".npy").unwrap_or(&i.filename) == key)
            .map(|i| i.filename.clone())
    }

    /// `np.load(...)[key]`'s bytes: `NpzFile.__getitem__`, `ZipFile.open` on the last central
    /// record of the name (`NameToInfo`), and a full `ZipExtFile.read()` of a stored member.
    pub fn read(&self, key: &str) -> Result<Vec<u8>, CoreError> {
        let name = self.full_name(key).ok_or_else(|| {
            CoreError::new("key", format!("'{key} is not a file in the archive'"))
        })?;
        let info = self
            .infos
            .iter()
            .rev()
            .find(|i| i.filename == *name)
            .ok_or_else(|| bad("missing member"))?;
        self.open_and_read(info, &name)
    }

    /// Port of `ZipFile.open` (reading) and `ZipExtFile` for `ZIP_STORED`.
    fn open_and_read(&self, info: &Info, name: &str) -> Result<Vec<u8>, CoreError> {
        let bytes = &self.bytes;
        if info.header_offset < 0 {
            return Err(CoreError::new(
                "value",
                format!("negative seek value {}", info.header_offset),
            ));
        }
        let fheader = read_at(bytes, info.header_offset, LOCAL_LEN);
        if fheader.len() != LOCAL_LEN {
            return Err(bad("Truncated file header"));
        }
        if &fheader[..4] != LOCAL {
            return Err(bad("Bad magic number for file header"));
        }
        let (local_flags, name_len, extra_len) = (
            u16_at(fheader, 6),
            usize::from(u16_at(fheader, 26)),
            u16_at(fheader, 28),
        );
        let name_at = info.header_offset + 30;
        let fname = read_at(bytes, name_at, name_len);
        // `zef_file.seek(extra_len, whence=1)` never checks the bound.
        let data_at = name_at + len_i128(fname) + i128::from(extra_len);
        if info.flags & MASK_COMPRESSED_PATCH != 0 {
            return Err(CoreError::new(
                "not_implemented",
                "compressed patched data (flag bit 5)",
            ));
        }
        if info.flags & MASK_STRONG_ENCRYPTION != 0 {
            return Err(CoreError::new(
                "not_implemented",
                "strong encryption (flag bit 6)",
            ));
        }
        let fname_str = decode_name(fname, local_flags)?;
        if fname_str != info.orig_filename {
            return Err(bad(format!(
                "File name in directory {} and header {} differ.",
                py_repr_str(&info.orig_filename),
                py_repr_bytes(fname)
            )));
        }
        if data_at + i128::from(info.compress_size) > info.end_offset
            && info.end_offset != info.header_offset
        {
            return Err(bad(format!(
                "Overlapped entries: {} (possible zip bomb)",
                py_repr_str(&info.orig_filename)
            )));
        }
        if info.flags & MASK_ENCRYPTED != 0 {
            return Err(CoreError::new(
                "runtime",
                format!(
                    "File {} is encrypted, password required for extraction",
                    py_repr_str(name)
                ),
            ));
        }
        if info.method != 0 {
            return Err(bad("compressed zip entries are not supported"));
        }
        // A stored member yields min(compress_size, file_size) bytes; running out of archive
        // before that is `EOFError` from `ZipExtFile._read2`.
        let need = info.compress_size.min(info.file_size);
        if len_i128(bytes) - data_at < i128::from(need) {
            return Err(CoreError::new("eof", ""));
        }
        let need = usize::try_from(need).map_err(|_| crate::size::too_big())?;
        let payload = crate::size::try_copy(read_at(bytes, data_at, need))?;
        if crc32(&payload) != info.crc {
            return Err(bad(format!(
                "Bad CRC-32 for file {}",
                py_repr_str(&info.filename)
            )));
        }
        Ok(payload)
    }
}

#[cfg(test)]
mod tests {
    use super::{load, py_repr_bytes, py_repr_str};

    #[test]
    fn rejects_input_that_is_not_an_archive() {
        assert_eq!(
            load(b"PK\x03\x04short").unwrap_err().message,
            "File is not a zip file"
        );
        assert_eq!(load(b"hello").unwrap_err().kind, "value");
    }

    #[test]
    fn utf8_errors_read_like_python() {
        let msg = |b: &[u8]| super::decode_utf8(b).unwrap_err().message;
        assert_eq!(
            msg(b"ignored-\xff.npy"),
            "'utf-8' codec can't decode byte 0xff in position 8: invalid start byte"
        );
        assert_eq!(
            msg(b"\xc0\xaf"),
            "'utf-8' codec can't decode byte 0xc0 in position 0: invalid start byte"
        );
        assert_eq!(
            msg(b"ab\xe2\x82."),
            "'utf-8' codec can't decode bytes in position 2-3: invalid continuation byte"
        );
        assert_eq!(
            msg(b"\xed\xa0\x80"),
            "'utf-8' codec can't decode byte 0xed in position 0: invalid continuation byte"
        );
        assert_eq!(
            msg(b"x\xe2\x82"),
            "'utf-8' codec can't decode bytes in position 1-2: unexpected end of data"
        );
        assert_eq!(
            msg(b"x\xf0\x9f\x98"),
            "'utf-8' codec can't decode bytes in position 1-3: unexpected end of data"
        );
        assert_eq!(
            super::decode_utf8("caf\u{e9}".as_bytes()).unwrap(),
            "caf\u{e9}"
        );
    }

    #[test]
    fn quotes_names_like_python() {
        assert_eq!(py_repr_str("stock.npy"), "'stock.npy'");
        assert_eq!(py_repr_str("it's"), "\"it's\"");
        assert_eq!(py_repr_bytes(b"a\x01"), "b'a\\x01'");
    }
}
