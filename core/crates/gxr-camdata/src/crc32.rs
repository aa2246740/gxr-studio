//! CRC-32 (IEEE 802.3, matching `zlib.crc32`) and big-endian slice readers.

/// Precomputed IEEE 802.3 CRC-32 lookup table.
const fn make_table() -> [u32; 256] {
    let mut table = [0u32; 256];
    let mut i = 0u32;
    while i < 256 {
        let mut c = i;
        let mut k = 0;
        while k < 8 {
            if c & 1 != 0 {
                c = 0xEDB8_8320 ^ (c >> 1);
            } else {
                c >>= 1;
            }
            k += 1;
        }
        table[i as usize] = c;
        i += 1;
    }
    table
}

const CRC_TABLE: [u32; 256] = make_table();

/// Incremental CRC-32 update.
pub fn crc_update(mut crc: u32, data: &[u8]) -> u32 {
    crc = !crc;
    for &b in data {
        crc = CRC_TABLE[((crc ^ u32::from(b)) & 0xFF) as usize] ^ (crc >> 8);
    }
    !crc
}

/// Port of gxr_studio/camdata/datcodec.py:122 crc.
pub fn crc(data: &[u8]) -> u32 {
    crc_update(0, data)
}

/// Port of gxr_studio/camdata/datcodec.py:135 _zero_crc: CRC-32 with `b[at..at+4]` zeroed.
pub fn zero_crc(data: &[u8], at: usize) -> u32 {
    let c = crc_update(0, &data[..at]);
    let c = crc_update(c, &[0, 0, 0, 0]);
    crc_update(c, &data[at + 4..])
}

/// Port of gxr_studio/camdata/datcodec.py:126 u16.
pub fn u16_be(data: &[u8], at: usize) -> u16 {
    u16::from_be_bytes([data[at], data[at + 1]])
}

/// Port of gxr_studio/camdata/datcodec.py:131 u32.
pub fn u32_be(data: &[u8], at: usize) -> u32 {
    u32::from_be_bytes([data[at], data[at + 1], data[at + 2], data[at + 3]])
}

/// Port of gxr_studio/camdata/datcodec.py:141 _align4.
pub const fn align4(n: usize) -> usize {
    (n + 3) & !3
}

/// Decode a 64-char hex digest into 32 bytes.
pub const fn hex32(hex: &str) -> [u8; 32] {
    let bytes = hex.as_bytes();
    assert!(bytes.len() == 64);
    let mut out = [0u8; 32];
    let mut i = 0;
    while i < 32 {
        out[i] = (nibble(bytes[2 * i]) << 4) | nibble(bytes[2 * i + 1]);
        i += 1;
    }
    out
}

const fn nibble(b: u8) -> u8 {
    match b {
        b'0'..=b'9' => b - b'0',
        b'a'..=b'f' => b - b'a' + 10,
        b'A'..=b'F' => b - b'A' + 10,
        _ => panic!("invalid hex digit"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc_matches_standard_vectors() {
        assert_eq!(crc(b""), 0);
        assert_eq!(crc(b"123456789"), 0xCBF4_3926);
        assert_eq!(zero_crc(b"1234XXXX9", 4), crc(b"1234\0\0\0\09"));
    }
}
