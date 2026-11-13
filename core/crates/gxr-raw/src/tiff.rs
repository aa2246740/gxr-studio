//! Port of gxr_studio/raw/tiff.py: a tiny read-only classic TIFF reader.

use std::collections::BTreeMap;

use crate::error::{PyErr, PyResult};
use crate::pyrepr::{Scalar, py_str_repr};

/// A tag's decoded value, typed the way Python's `Tiff.ifd` returns it.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    /// ASCII (2): trailing NULs and spaces stripped, non-ASCII bytes replaced by U+FFFD.
    Ascii(String),
    /// UNDEFINED (7): the raw bytes.
    Bytes(Vec<u8>),
    /// BYTE, SHORT, LONG, SBYTE, SSHORT, SLONG.
    Ints(Vec<i64>),
    /// RATIONAL, SRATIONAL (zero denominator → NaN), FLOAT, DOUBLE.
    Floats(Vec<f64>),
}

impl Value {
    /// Python `list(value)` / iteration: characters for ASCII, ints for bytes.
    pub fn items(&self) -> Vec<Scalar> {
        match self {
            Self::Ascii(s) => s.chars().map(Scalar::Char).collect(),
            Self::Bytes(b) => b.iter().map(|&v| Scalar::Int(i64::from(v))).collect(),
            Self::Ints(v) => v.iter().map(|&i| Scalar::Int(i)).collect(),
            Self::Floats(v) => v.iter().map(|&f| Scalar::Float(f)).collect(),
        }
    }

    /// Python `value[0]`, with the `IndexError` text of the value's Python type.
    pub(crate) fn first(&self) -> PyResult<Scalar> {
        let message = match self {
            Self::Ascii(_) => "string index out of range",
            Self::Bytes(_) => "index out of range",
            Self::Ints(_) | Self::Floats(_) => "list index out of range",
        };
        self.items()
            .into_iter()
            .next()
            .ok_or_else(|| PyErr::Index(message.into()))
    }

    /// Python `np.asarray(value, dtype=np.float64)`.
    pub(crate) fn as_f64(&self) -> PyResult<Vec<f64>> {
        match self {
            #[allow(clippy::cast_precision_loss)]
            Self::Ints(v) => Ok(v.iter().map(|&i| i as f64).collect()),
            Self::Floats(v) => Ok(v.clone()),
            Self::Ascii(s) => s.parse::<f64>().map(|v| vec![v]).map_err(|_| {
                PyErr::Value(format!(
                    "could not convert string to float: {}",
                    py_str_repr(s)
                ))
            }),
            Self::Bytes(b) => Err(PyErr::Value(format!(
                "could not convert string to float: {}",
                crate::pyrepr::py_bytes_repr(b)
            ))),
        }
    }
}

/// One IFD entry: `{"offset", "raw", "value", "type", "count"}` in Python.
#[derive(Clone, Debug, PartialEq)]
pub struct Entry {
    pub offset: usize,
    pub length: usize,
    pub value: Value,
    pub typ: u16,
    pub count: u32,
}

/// An IFD: tag → entry. A repeated tag keeps the last entry, like the Python dict.
pub type Ifd = BTreeMap<u16, Entry>;

/// Port of gxr_studio/raw/tiff.py:17 Tiff.
#[derive(Clone, Copy, Debug)]
pub struct Tiff<'a> {
    pub data: &'a [u8],
    /// `self.endian == ">"`.
    pub big_endian: bool,
    pub root: u32,
}

/// `TYPES`: (Python struct format character, size in bytes); `None` format = raw bytes.
fn type_info(typ: u16) -> Option<(Option<char>, usize)> {
    Some(match typ {
        1 => (Some('B'), 1),
        2 | 7 => (None, 1),
        3 => (Some('H'), 2),
        4 => (Some('I'), 4),
        5 => (Some('R'), 8),
        6 => (Some('b'), 1),
        8 => (Some('h'), 2),
        9 => (Some('i'), 4),
        10 => (Some('r'), 8),
        11 => (Some('f'), 4),
        12 => (Some('d'), 8),
        _ => return None,
    })
}

fn u16_at(data: &[u8], at: usize, big: bool) -> u16 {
    let b = [data[at], data[at + 1]];
    if big {
        u16::from_be_bytes(b)
    } else {
        u16::from_le_bytes(b)
    }
}

fn u32_at(data: &[u8], at: usize, big: bool) -> u32 {
    let b = [data[at], data[at + 1], data[at + 2], data[at + 3]];
    if big {
        u32::from_be_bytes(b)
    } else {
        u32::from_le_bytes(b)
    }
}

fn u64_at(data: &[u8], at: usize, big: bool) -> u64 {
    let mut b = [0u8; 8];
    b.copy_from_slice(&data[at..at + 8]);
    if big {
        u64::from_be_bytes(b)
    } else {
        u64::from_le_bytes(b)
    }
}

/// ASCII value: `raw.rstrip(b"\0 ").decode("ascii", errors="replace")`.
fn ascii_value(raw: &[u8]) -> String {
    let mut end = raw.len();
    while end > 0 && (raw[end - 1] == 0 || raw[end - 1] == b' ') {
        end -= 1;
    }
    raw[..end]
        .iter()
        .map(|&b| if b < 0x80 { char::from(b) } else { '\u{fffd}' })
        .collect()
}

fn decode_values(raw: &[u8], fmt: char, n: usize, big: bool) -> Value {
    match fmt {
        'B' => Value::Ints(raw.iter().map(|&b| i64::from(b)).collect()),
        #[allow(clippy::cast_possible_wrap)]
        'b' => Value::Ints(raw.iter().map(|&b| i64::from(b as i8)).collect()),
        'H' => Value::Ints((0..n).map(|i| i64::from(u16_at(raw, 2 * i, big))).collect()),
        #[allow(clippy::cast_possible_wrap)]
        'h' => Value::Ints(
            (0..n)
                .map(|i| i64::from(u16_at(raw, 2 * i, big) as i16))
                .collect(),
        ),
        'I' => Value::Ints((0..n).map(|i| i64::from(u32_at(raw, 4 * i, big))).collect()),
        #[allow(clippy::cast_possible_wrap)]
        'i' => Value::Ints(
            (0..n)
                .map(|i| i64::from(u32_at(raw, 4 * i, big) as i32))
                .collect(),
        ),
        'f' => Value::Floats(
            (0..n)
                .map(|i| f64::from(f32::from_bits(u32_at(raw, 4 * i, big))))
                .collect(),
        ),
        'd' => Value::Floats(
            (0..n)
                .map(|i| f64::from_bits(u64_at(raw, 8 * i, big)))
                .collect(),
        ),
        // RATIONAL / SRATIONAL: `a[j] / a[j + 1] if a[j + 1] else math.nan`
        _ => Value::Floats(
            (0..n)
                .map(|i| {
                    let (num, den) = if fmt == 'R' {
                        (
                            f64::from(u32_at(raw, 8 * i, big)),
                            f64::from(u32_at(raw, 8 * i + 4, big)),
                        )
                    } else {
                        #[allow(clippy::cast_possible_wrap)]
                        (
                            f64::from(u32_at(raw, 8 * i, big) as i32),
                            f64::from(u32_at(raw, 8 * i + 4, big) as i32),
                        )
                    };
                    if den == 0.0 { f64::NAN } else { num / den }
                })
                .collect(),
        ),
    }
}

impl<'a> Tiff<'a> {
    /// Port of gxr_studio/raw/tiff.py:35 Tiff.__init__.
    pub(crate) fn new(data: &'a [u8]) -> PyResult<Self> {
        if data.len() < 8 || !(data[..2] == *b"II" || data[..2] == *b"MM") {
            return Err(PyErr::Value("不是 TIFF/DNG 文件".into()));
        }
        let big_endian = data[..2] == *b"MM";
        if u16_at(data, 2, big_endian) != 42 {
            return Err(PyErr::Value("不是经典 TIFF（魔数不是 42）".into()));
        }
        let root = u32_at(data, 4, big_endian);
        Ok(Self {
            data,
            big_endian,
            root,
        })
    }

    /// Python `T.ifd(offset)` where `offset` came out of a tag value (an int, float or char).
    pub(crate) fn ifd_at(&self, offset: &Scalar) -> PyResult<Ifd> {
        match *offset {
            Scalar::Int(o) => self.ifd(o, None),
            Scalar::Float(f) => {
                #[allow(clippy::cast_precision_loss)]
                let limit = self.data.len() as f64 - 2.0;
                if !(0.0 <= f && f <= limit) {
                    return Err(PyErr::Value("IFD 偏移越界".into()));
                }
                Err(PyErr::Type(
                    "'float' object cannot be interpreted as an integer".into(),
                ))
            }
            Scalar::Char(_) => Err(PyErr::Type(
                "'<=' not supported between instances of 'int' and 'str'".into(),
            )),
        }
    }

    /// Port of gxr_studio/raw/tiff.py:44 Tiff.ifd. `big_endian` overrides the file's byte order.
    pub(crate) fn ifd(&self, offset: i64, big_endian: Option<bool>) -> PyResult<Ifd> {
        let e = match big_endian {
            Some(flag) => flag,
            None => self.big_endian,
        };
        let len = crate::bounds::i64_from_len(self.data.len())
            .map_err(|_| PyErr::Value("IFD 偏移越界".into()))?;
        if !(0 <= offset && offset <= len - 2) {
            return Err(PyErr::Value("IFD 偏移越界".into()));
        }
        let offset = crate::bounds::fit_count(
            u128::try_from(offset).map_err(|_| PyErr::Value("IFD 偏移越界".into()))?,
        )
        .map_err(|_| PyErr::Value("IFD 偏移越界".into()))?;
        let count = usize::from(u16_at(self.data, offset, e));
        let ifd_bytes = count
            .checked_mul(12)
            .and_then(|n| n.checked_add(6))
            .and_then(|n| offset.checked_add(n));
        let Some(ifd_end) = ifd_bytes else {
            return Err(PyErr::Value("IFD 超出文件".into()));
        };
        if ifd_end > self.data.len() {
            return Err(PyErr::Value("IFD 超出文件".into()));
        }
        let mut result = Ifd::new();
        for i in 0..count {
            let p = offset + 2 + i * 12;
            let tag = u16_at(self.data, p, e);
            let typ = u16_at(self.data, p + 2, e);
            let n = u32_at(self.data, p + 4, e);
            let Some((fmt, size)) = type_info(typ) else {
                continue;
            };
            let length = u64::from(n)
                .checked_mul(
                    u64::try_from(size)
                        .map_err(|_| PyErr::Value(format!("标签 {tag} 的数据超出文件")))?,
                )
                .ok_or_else(|| PyErr::Value(format!("标签 {tag} 的数据超出文件")))?;
            let inline = u64::try_from(p)
                .ok()
                .and_then(|p| p.checked_add(8))
                .ok_or_else(|| PyErr::Value(format!("标签 {tag} 的数据超出文件")))?;
            let o = if length <= 4 {
                inline
            } else {
                u64::from(u32_at(self.data, p + 8, e))
            };
            let end = o
                .checked_add(length)
                .ok_or_else(|| PyErr::Value(format!("标签 {tag} 的数据超出文件")))?;
            let file_len = u64::try_from(self.data.len())
                .map_err(|_| PyErr::Value(format!("标签 {tag} 的数据超出文件")))?;
            if end > file_len {
                return Err(PyErr::Value(format!("标签 {tag} 的数据超出文件")));
            }
            let o = usize::try_from(o)
                .map_err(|_| PyErr::Value(format!("标签 {tag} 的数据超出文件")))?;
            let length = usize::try_from(length)
                .map_err(|_| PyErr::Value(format!("标签 {tag} 的数据超出文件")))?;
            let raw = &self.data[o..o + length];
            // `n` is a `u32` tag count. `try_from` fails only if it does not fit `usize`.
            let n_items = usize::try_from(n)
                .map_err(|_| PyErr::Value(format!("标签 {tag} 的数据超出文件")))?;
            let value = match (typ, fmt) {
                (2, _) => Value::Ascii(ascii_value(raw)),
                (_, None) => Value::Bytes(raw.to_vec()),
                (_, Some(f)) => decode_values(raw, f, n_items, e),
            };
            result.insert(
                tag,
                Entry {
                    offset: o,
                    length,
                    value,
                    typ,
                    count: n,
                },
            );
        }
        Ok(result)
    }
}

/// Python `d[tag]["value"] if tag in d else None`.
pub(crate) fn val(d: &Ifd, tag: u16) -> Option<&Value> {
    d.get(&tag).map(|e| &e.value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_non_tiff() {
        assert_eq!(
            Tiff::new(b"PK\x03\x04....").unwrap_err(),
            PyErr::Value("不是 TIFF/DNG 文件".into())
        );
        assert_eq!(
            Tiff::new(b"II\x2b\x00\x08\x00\x00\x00").unwrap_err(),
            PyErr::Value("不是经典 TIFF（魔数不是 42）".into())
        );
    }

    #[test]
    fn reads_inline_and_offset_values_both_endians() {
        // little endian: one SHORT inline, one RATIONAL at an offset, one ASCII with padding
        let mut d = b"II\x2a\x00\x08\x00\x00\x00".to_vec();
        d.extend_from_slice(&3u16.to_le_bytes());
        for (tag, typ, n, field) in [
            (256u16, 3u16, 1u32, 4000u32),
            (282, 5, 1, 50),
            (305, 2, 4, u32::from_le_bytes(*b"ab \0")),
        ] {
            d.extend_from_slice(&tag.to_le_bytes());
            d.extend_from_slice(&typ.to_le_bytes());
            d.extend_from_slice(&n.to_le_bytes());
            d.extend_from_slice(&field.to_le_bytes());
        }
        d.extend_from_slice(&0u32.to_le_bytes());
        d.extend_from_slice(&[0, 0]);
        d.extend_from_slice(&3u32.to_le_bytes());
        d.extend_from_slice(&0u32.to_le_bytes());
        let t = Tiff::new(&d).unwrap();
        let ifd = t.ifd(i64::from(t.root), None).unwrap();
        assert_eq!(ifd[&256].value, Value::Ints(vec![4000]));
        assert!(matches!(&ifd[&282].value, Value::Floats(v) if v[0].is_nan()));
        assert_eq!(ifd[&305].value, Value::Ascii("ab".into()));
        assert_eq!(
            t.ifd(i64::try_from(d.len()).unwrap(), None).unwrap_err(),
            PyErr::Value("IFD 偏移越界".into())
        );
    }
}
