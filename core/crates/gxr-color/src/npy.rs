//! A `.npy` reader for format 1.0 and 2.0, little-endian, C order.
//!
//! `np.savez` stores each array as one of these. Fortran order and unknown dtypes are rejected.

use gxr_raw::CoreError;

/// One parsed array. `values` is the raw little-endian payload.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Npy {
    pub descr: String,
    pub shape: Vec<usize>,
    pub values: Vec<u8>,
}

fn bad(message: impl Into<String>) -> CoreError {
    CoreError::new("npy", message)
}

/// The header and payload `np.load` reads from a calibration array.
pub fn decode(bytes: &[u8]) -> Result<Npy, CoreError> {
    if bytes.len() < 10 || &bytes[..6] != b"\x93NUMPY" {
        return Err(bad("not an npy file"));
    }
    let (header, data_at) = match bytes[6] {
        1 => {
            let len = usize::from(u16::from_le_bytes(read_n(bytes, 8)?));
            (header_text(bytes, 10, len)?, 10 + len)
        }
        2 | 3 => {
            let len = usize::try_from(u32::from_le_bytes(read4(bytes, 8)?)).map_err(|_| bad("npy header is short"))?;
            (header_text(bytes, 12, len)?, 12 + len)
        }
        _ => return Err(bad("unsupported npy version")),
    };
    if header.contains("'fortran_order': True") {
        return Err(bad("fortran order arrays are not supported"));
    }
    let descr = quoted_field(&header, "descr")?;
    let shape = shape_of(&header)?;
    let width = width_of(&descr)?;
    let mut count = 1usize;
    for dim in &shape {
        count = count.checked_mul(*dim).ok_or_else(|| bad("array is too big"))?;
    }
    let nbytes = count.checked_mul(width).ok_or_else(|| bad("array is too big"))?;
    let values = bytes.get(data_at..data_at + nbytes).ok_or_else(|| bad("npy data is short"))?.to_vec();
    Ok(Npy { descr, shape, values })
}

fn read_n(bytes: &[u8], at: usize) -> Result<[u8; 2], CoreError> {
    let s = bytes.get(at..at + 2).ok_or_else(|| bad("npy header is short"))?;
    Ok([s[0], s[1]])
}

fn read4(bytes: &[u8], at: usize) -> Result<[u8; 4], CoreError> {
    let s = bytes.get(at..at + 4).ok_or_else(|| bad("npy header is short"))?;
    Ok([s[0], s[1], s[2], s[3]])
}

fn header_text(bytes: &[u8], at: usize, len: usize) -> Result<String, CoreError> {
    let raw = bytes.get(at..at + len).ok_or_else(|| bad("npy header is short"))?;
    String::from_utf8(raw.to_vec()).map_err(|_| bad("npy header is not utf-8"))
}

fn quoted_field(header: &str, key: &str) -> Result<String, CoreError> {
    let needle = format!("'{key}': '");
    let start = header.find(&needle).ok_or_else(|| bad(format!("npy header has no {key}")))? + needle.len();
    let rest = &header[start..];
    let end = rest.find('\'').ok_or_else(|| bad(format!("npy header has no {key}")))?;
    Ok(rest[..end].to_string())
}

fn shape_of(header: &str) -> Result<Vec<usize>, CoreError> {
    let needle = "'shape': (";
    let start = header.find(needle).ok_or_else(|| bad("npy header has no shape"))? + needle.len();
    let rest = &header[start..];
    let end = rest.find(')').ok_or_else(|| bad("npy header has no shape"))?;
    let mut shape = Vec::new();
    for part in rest[..end].split(',') {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }
        shape.push(part.parse().map_err(|_| bad("npy header has no shape"))?);
    }
    Ok(shape)
}

fn width_of(descr: &str) -> Result<usize, CoreError> {
    match descr {
        "<i4" | "<f4" => Ok(4),
        "<i8" | "<f8" => Ok(8),
        "<c16" => Ok(16),
        other => Err(bad(format!("unsupported dtype {other}"))),
    }
}

fn take<const N: usize>(bytes: &[u8], at: usize) -> Result<[u8; N], CoreError> {
    let s = bytes.get(at..at + N).ok_or_else(|| bad("npy data is short"))?;
    let mut buf = [0u8; N];
    buf.copy_from_slice(s);
    Ok(buf)
}

impl Npy {
    /// `ndarray.astype(float)`, which is float64.
    pub fn f64s(&self) -> Result<Vec<f64>, CoreError> {
        match self.descr.as_str() {
            "<f8" => {
                let mut out = Vec::with_capacity(self.values.len() / 8);
                for at in (0..self.values.len()).step_by(8) {
                    out.push(f64::from_le_bytes(take(&self.values, at)?));
                }
                Ok(out)
            }
            "<f4" => {
                let mut out = Vec::with_capacity(self.values.len() / 4);
                for at in (0..self.values.len()).step_by(4) {
                    let bits: [u8; 4] = take(&self.values, at)?;
                    out.push(f64::from(f32::from_le_bytes(bits)));
                }
                Ok(out)
            }
            _ => Err(bad(format!("expected a float array, got {}", self.descr))),
        }
    }

    pub fn i64s(&self) -> Result<Vec<i64>, CoreError> {
        match self.descr.as_str() {
            "<i8" => {
                let mut out = Vec::with_capacity(self.values.len() / 8);
                for at in (0..self.values.len()).step_by(8) {
                    out.push(i64::from_le_bytes(take(&self.values, at)?));
                }
                Ok(out)
            }
            "<i4" => {
                let mut out = Vec::with_capacity(self.values.len() / 4);
                for at in (0..self.values.len()).step_by(4) {
                    let bits: [u8; 4] = take(&self.values, at)?;
                    out.push(i64::from(i32::from_le_bytes(bits)));
                }
                Ok(out)
            }
            _ => Err(bad(format!("expected an int array, got {}", self.descr))),
        }
    }

    /// `<c16`: real then imag, each little-endian f64.
    pub fn complexes(&self) -> Result<Vec<(f64, f64)>, CoreError> {
        if self.descr != "<c16" {
            return Err(bad(format!("expected complex128, got {}", self.descr)));
        }
        let mut out = Vec::with_capacity(self.values.len() / 16);
        for at in (0..self.values.len()).step_by(16) {
            let re = f64::from_le_bytes(take(&self.values, at)?);
            let im = f64::from_le_bytes(take(&self.values, at + 8)?);
            out.push((re, im));
        }
        Ok(out)
    }
}
