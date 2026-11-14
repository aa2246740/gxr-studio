//! JSON parser for `--space JSON` with a strict 128-level nesting limit.

use gxr_raw::CoreError;

/// Maximum allowed nesting of JSON arrays and objects before descending.
pub const MAX_DEPTH: usize = 128;

/// Error message when JSON nesting exceeds `MAX_DEPTH`.
pub const DEPTH_ERROR: &str = "JSON 嵌套太深";

#[derive(Clone, Debug, PartialEq)]
pub enum JsonVal {
    Null,
    Bool(bool),
    Num,
    Str(String),
    Arr(Vec<JsonVal>),
    Obj(Vec<(String, JsonVal)>),
}

impl JsonVal {
    pub fn as_object(&self) -> Option<&[(String, JsonVal)]> {
        match self {
            Self::Obj(pairs) => Some(pairs),
            _ => None,
        }
    }
}

fn bad_syntax() -> CoreError {
    CoreError::new("value_error", "invalid space JSON")
}

/// Parse one JSON value, capping array and object nesting at `MAX_DEPTH`.
pub fn parse_json(text: &str) -> Result<JsonVal, CoreError> {
    let mut p = Parser {
        bytes: text.as_bytes(),
        text,
        pos: 0,
        depth: 0,
    };
    p.skip_ws();
    let val = p.value()?;
    p.skip_ws();
    if p.pos != p.bytes.len() {
        return Err(bad_syntax());
    }
    Ok(val)
}

struct Parser<'a> {
    bytes: &'a [u8],
    text: &'a str,
    pos: usize,
    depth: usize,
}

impl<'a> Parser<'a> {
    fn skip_ws(&mut self) {
        while self.pos < self.bytes.len()
            && matches!(self.bytes[self.pos], b' ' | b'\t' | b'\r' | b'\n')
        {
            self.pos += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn descend(&mut self) -> Result<(), CoreError> {
        if self.depth >= MAX_DEPTH {
            return Err(CoreError::new("json", DEPTH_ERROR));
        }
        self.depth += 1;
        Ok(())
    }

    fn value(&mut self) -> Result<JsonVal, CoreError> {
        self.skip_ws();
        match self.peek() {
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => self.string().map(JsonVal::Str),
            Some(b'n') => self.literal(b"null", JsonVal::Null),
            Some(b't') => self.literal(b"true", JsonVal::Bool(true)),
            Some(b'f') => self.literal(b"false", JsonVal::Bool(false)),
            Some(b'-') | Some(b'0'..=b'9') => self.number(),
            _ => Err(bad_syntax()),
        }
    }

    fn literal(&mut self, word: &[u8], val: JsonVal) -> Result<JsonVal, CoreError> {
        if self.bytes[self.pos..].starts_with(word) {
            self.pos += word.len();
            Ok(val)
        } else {
            Err(bad_syntax())
        }
    }

    fn array(&mut self) -> Result<JsonVal, CoreError> {
        self.descend()?;
        self.pos += 1;
        self.skip_ws();
        let mut items = Vec::new();
        if self.peek() == Some(b']') {
            self.pos += 1;
            self.depth -= 1;
            return Ok(JsonVal::Arr(items));
        }
        loop {
            items.push(self.value()?);
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                    self.skip_ws();
                }
                Some(b']') => {
                    self.pos += 1;
                    self.depth -= 1;
                    return Ok(JsonVal::Arr(items));
                }
                _ => return Err(bad_syntax()),
            }
        }
    }

    fn object(&mut self) -> Result<JsonVal, CoreError> {
        self.descend()?;
        self.pos += 1;
        self.skip_ws();
        let mut pairs = Vec::new();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            self.depth -= 1;
            return Ok(JsonVal::Obj(pairs));
        }
        loop {
            self.skip_ws();
            let key = self.string()?;
            self.skip_ws();
            if self.peek() != Some(b':') {
                return Err(bad_syntax());
            }
            self.pos += 1;
            let val = self.value()?;
            pairs.push((key, val));
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                    self.skip_ws();
                }
                Some(b'}') => {
                    self.pos += 1;
                    self.depth -= 1;
                    return Ok(JsonVal::Obj(pairs));
                }
                _ => return Err(bad_syntax()),
            }
        }
    }

    fn number(&mut self) -> Result<JsonVal, CoreError> {
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        match self.peek() {
            Some(b'0') => self.pos += 1,
            Some(b'1'..=b'9') => {
                self.pos += 1;
                while matches!(self.peek(), Some(b'0'..=b'9')) {
                    self.pos += 1;
                }
            }
            _ => return Err(bad_syntax()),
        }
        if self.peek() == Some(b'.') {
            self.pos += 1;
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return Err(bad_syntax());
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.pos += 1;
            }
        }
        if matches!(self.peek(), Some(b'e') | Some(b'E')) {
            self.pos += 1;
            if matches!(self.peek(), Some(b'+') | Some(b'-')) {
                self.pos += 1;
            }
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return Err(bad_syntax());
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.pos += 1;
            }
        }
        Ok(JsonVal::Num)
    }

    fn string(&mut self) -> Result<String, CoreError> {
        if self.peek() != Some(b'"') {
            return Err(bad_syntax());
        }
        let mut out = String::new();
        let mut chars = self.text[self.pos + 1..].char_indices();
        while let Some((idx, ch)) = chars.next() {
            match ch {
                '"' => {
                    self.pos = self.pos + 1 + idx + 1;
                    return Ok(out);
                }
                '\\' => {
                    let (_, esc) = chars.next().ok_or_else(bad_syntax)?;
                    match esc {
                        '"' => out.push('"'),
                        '\\' => out.push('\\'),
                        '/' => out.push('/'),
                        'b' => out.push('\u{0008}'),
                        'f' => out.push('\u{000c}'),
                        'n' => out.push('\n'),
                        'r' => out.push('\r'),
                        't' => out.push('\t'),
                        'u' => {
                            let code = read_hex4(&mut chars).ok_or_else(bad_syntax)?;
                            if (0xD800..=0xDBFF).contains(&code) {
                                let (_, b1) = chars.next().ok_or_else(bad_syntax)?;
                                let (_, b2) = chars.next().ok_or_else(bad_syntax)?;
                                if b1 != '\\' || b2 != 'u' {
                                    return Err(bad_syntax());
                                }
                                let low = read_hex4(&mut chars).ok_or_else(bad_syntax)?;
                                if !(0xDC00..=0xDFFF).contains(&low) {
                                    return Err(bad_syntax());
                                }
                                let cp = 0x10000 + (((code - 0xD800) << 10) | (low - 0xDC00));
                                out.push(char::from_u32(cp).ok_or_else(bad_syntax)?);
                            } else if (0xDC00..=0xDFFF).contains(&code) {
                                return Err(bad_syntax());
                            } else {
                                out.push(char::from_u32(code).ok_or_else(bad_syntax)?);
                            }
                        }
                        _ => return Err(bad_syntax()),
                    }
                }
                c if (c as u32) < 0x20 => return Err(bad_syntax()),
                c => out.push(c),
            }
        }
        Err(bad_syntax())
    }
}

fn read_hex4(chars: &mut std::str::CharIndices<'_>) -> Option<u32> {
    let mut hex = [0u8; 4];
    for slot in &mut hex {
        let (_, ch) = chars.next()?;
        if !ch.is_ascii_hexdigit() {
            return None;
        }
        *slot = ch as u8;
    }
    let s = std::str::from_utf8(&hex).ok()?;
    u32::from_str_radix(s, 16).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_nesting_depth_caps_at_128() {
        let ok_arr = format!(
            r#"{{"input_space":"srgb","extra":{}{}}}"#,
            "[".repeat(127),
            "]".repeat(127)
        );
        assert!(parse_json(&ok_arr).is_ok());

        let bad_arr = format!(
            r#"{{"input_space":"srgb","extra":{}{}}}"#,
            "[".repeat(128),
            "]".repeat(128)
        );
        assert_eq!(
            parse_json(&bad_arr).unwrap_err(),
            CoreError::new("json", DEPTH_ERROR)
        );

        let ok_obj = format!(
            r#"{{"input_space":"srgb","extra":{}null{}}}"#,
            r#"{"a":"#.repeat(127),
            "}".repeat(127)
        );
        assert!(parse_json(&ok_obj).is_ok());

        let bad_obj = format!(
            r#"{{"input_space":"srgb","extra":{}null{}}}"#,
            r#"{"a":"#.repeat(128),
            "}".repeat(128)
        );
        assert_eq!(
            parse_json(&bad_obj).unwrap_err(),
            CoreError::new("json", DEPTH_ERROR)
        );

        let huge_arr = format!("{}{}", "[".repeat(20_000), "]".repeat(20_000));
        assert_eq!(
            parse_json(&huge_arr).unwrap_err(),
            CoreError::new("json", DEPTH_ERROR)
        );

        let huge_obj = format!("{}null{}", r#"{"a":"#.repeat(20_000), "}".repeat(20_000));
        assert_eq!(
            parse_json(&huge_obj).unwrap_err(),
            CoreError::new("json", DEPTH_ERROR)
        );
    }
}
