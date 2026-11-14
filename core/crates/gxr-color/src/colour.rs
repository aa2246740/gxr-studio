//! Sector-word codec used by the calibration check and the forward model.

use gxr_raw::CoreError;

fn value(message: impl Into<String>) -> CoreError {
    CoreError::new("value", message)
}

/// Port of gxr_studio/engine/colour.py:73 decode_word
pub fn decode_word(word: &[u8]) -> Result<(i64, i64), CoreError> {
    if word.len() != 4 {
        return Err(value("entry length"));
    }
    let (a, b, bx, a2) = (word[0], word[1], word[2], word[3]);
    if bx ^ 0x80 != b || a2 != a {
        return Err(value(hex_of(word)));
    }
    Ok((dec_byte(a), dec_byte(b)))
}

fn dec_byte(v: u8) -> i64 {
    if v < 128 {
        i64::from(v)
    } else {
        -i64::from(v - 128)
    }
}

fn hex_of(word: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(word.len() * 2);
    for byte in word {
        out.push(HEX[usize::from(byte >> 4)] as char);
        out.push(HEX[usize::from(byte & 0xf)] as char);
    }
    out
}

/// Port of gxr_studio/camdata/calib.py:51 stock_words_of_entry
pub fn stock_words_of_entry(entry: &[u8]) -> Result<[[i64; 2]; 24], CoreError> {
    let row = entry.get(115..211).ok_or_else(|| value("entry length"))?;
    let mut words = [[0i64; 2]; 24];
    for sector in 0..24 {
        let (a, b) = decode_word(&row[4 * sector..4 * sector + 4])?;
        words[sector] = [a, b];
    }
    Ok(words)
}
