//! A minimal `.npy` writer (format version 1.0, little-endian, C order). No dependency.

use std::io::Write;

/// Element types the CLI writes.
pub trait NpyElement: Copy {
    /// numpy `descr`.
    const DESCR: &'static str;
    fn put(self, out: &mut Vec<u8>);
}

impl NpyElement for u16 {
    const DESCR: &'static str = "<u2";
    fn put(self, out: &mut Vec<u8>) {
        out.extend_from_slice(&self.to_le_bytes());
    }
}

impl NpyElement for f32 {
    const DESCR: &'static str = "<f4";
    fn put(self, out: &mut Vec<u8>) {
        out.extend_from_slice(&self.to_le_bytes());
    }
}

impl NpyElement for f64 {
    const DESCR: &'static str = "<f8";
    fn put(self, out: &mut Vec<u8>) {
        out.extend_from_slice(&self.to_le_bytes());
    }
}

/// The header dict numpy writes, padded with spaces and a newline so the data starts on a
/// 64-byte boundary.
fn header(descr: &str, shape: &[usize]) -> Vec<u8> {
    let dims: Vec<String> = shape.iter().map(ToString::to_string).collect();
    let shape_text = if dims.len() == 1 {
        format!("({},)", dims[0])
    } else {
        format!("({})", dims.join(", "))
    };
    let mut dict =
        format!("{{'descr': '{descr}', 'fortran_order': False, 'shape': {shape_text}, }}");
    let fixed = 10; // magic (6) + version (2) + header length (2)
    let total = fixed + dict.len() + 1;
    dict.push_str(&" ".repeat((64 - total % 64) % 64));
    dict.push('\n');
    let mut out = b"\x93NUMPY\x01\x00".to_vec();
    let len = u16::try_from(dict.len()).unwrap_or(u16::MAX);
    out.extend_from_slice(&len.to_le_bytes());
    out.extend_from_slice(dict.as_bytes());
    out
}

/// The whole `.npy` file for `values` of `shape`.
pub fn encode<T: NpyElement>(values: &[T], shape: &[usize]) -> Vec<u8> {
    let mut out = header(T::DESCR, shape);
    out.reserve(std::mem::size_of_val(values));
    for &v in values {
        v.put(&mut out);
    }
    out
}

/// Write `values` of `shape` to `path`.
pub fn write<T: NpyElement>(path: &str, values: &[T], shape: &[usize]) -> std::io::Result<()> {
    let mut file = std::fs::File::create(path)?;
    file.write_all(&encode(values, shape))?;
    file.flush()
}

#[cfg(test)]
mod tests {
    use super::encode;

    #[test]
    fn header_is_aligned_and_parsable() {
        let bytes = encode(&[1u16, 2, 3, 4, 5, 6], &[2, 3]);
        assert_eq!(&bytes[..8], b"\x93NUMPY\x01\x00");
        let len = usize::from(u16::from_le_bytes([bytes[8], bytes[9]]));
        assert_eq!((10 + len) % 64, 0);
        let text = std::str::from_utf8(&bytes[10..10 + len]).unwrap();
        assert!(text.starts_with("{'descr': '<u2', 'fortran_order': False, 'shape': (2, 3), }"));
        assert!(text.ends_with('\n'));
        assert_eq!(&bytes[10 + len..10 + len + 2], &[1, 0]);
        let one = encode(&[1.5f32], &[1]);
        assert!(std::str::from_utf8(&one[10..]).unwrap().contains("(1,)"));
    }
}
