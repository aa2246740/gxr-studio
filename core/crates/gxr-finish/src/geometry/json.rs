//! Minimal strict JSON parser (no serde in core crates).
//!
//! Supports Python `json.loads` extensions (`NaN`, `Infinity`, `-Infinity`) and distinguishes
//! integer tokens from floating-point tokens so `parse_geometry` matches Python's `isinstance(..., int)`.

use gxr_raw::CoreError;

const MAX_DEPTH: usize = 64;

/// A parsed JSON value.
#[derive(Clone, Debug, PartialEq)]
pub enum JsonValue {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Array(Vec<JsonValue>),
    Object(Vec<(String, JsonValue)>),
}

impl JsonValue {
    /// Convert a JSON number (Int or Float) to `f64`. Booleans and strings return `None`.
    pub fn as_f64(&self) -> Option<f64> {
        match *self {
            Self::Int(i) => Some(i as f64),
            Self::Float(f) => Some(f),
            _ => None,
        }
    }

    /// Extract an integer token (`JsonValue::Int`). Float tokens like `1.0` return `None`,
    /// matching Python's `isinstance(v, int) and not isinstance(v, bool)`.
    pub fn as_strict_int(&self) -> Option<i64> {
        match *self {
            Self::Int(i) => Some(i),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match *self {
            Self::Bool(b) => Some(b),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::Str(s) => Some(s.as_str()),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[JsonValue]> {
        match self {
            Self::Array(items) => Some(items.as_slice()),
            _ => None,
        }
    }

    pub fn get(&self, key: &str) -> Option<&JsonValue> {
        match self {
            Self::Object(pairs) => pairs.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
}

/// Parse a JSON string into `JsonValue`.
pub fn parse_json(
    input: &str,
    err_kind: &'static str,
    err_msg: &str,
) -> Result<JsonValue, CoreError> {
    let mut parser = Parser {
        bytes: input.as_bytes(),
        pos: 0,
    };
    parser.skip_ws();
    let value = parser
        .parse_value(0)
        .map_err(|_| CoreError::new(err_kind, err_msg))?;
    parser.skip_ws();
    if parser.pos != parser.bytes.len() {
        return Err(CoreError::new(err_kind, err_msg));
    }
    Ok(value)
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn skip_ws(&mut self) {
        while let Some(&b) = self.bytes.get(self.pos) {
            if matches!(b, b' ' | b'\t' | b'\r' | b'\n') {
                self.pos += 1;
            } else {
                break;
            }
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn starts_with(&self, lit: &[u8]) -> bool {
        self.bytes.get(self.pos..self.pos + lit.len()) == Some(lit)
    }

    fn parse_value(&mut self, depth: usize) -> Result<JsonValue, ()> {
        if depth > MAX_DEPTH {
            return Err(());
        }
        self.skip_ws();
        let b = self.peek().ok_or(())?;
        match b {
            b'n' => {
                if self.starts_with(b"null") {
                    self.pos += 4;
                    Ok(JsonValue::Null)
                } else {
                    Err(())
                }
            }
            b't' => {
                if self.starts_with(b"true") {
                    self.pos += 4;
                    Ok(JsonValue::Bool(true))
                } else {
                    Err(())
                }
            }
            b'f' => {
                if self.starts_with(b"false") {
                    self.pos += 5;
                    Ok(JsonValue::Bool(false))
                } else {
                    Err(())
                }
            }
            b'"' => self.parse_string().map(JsonValue::Str),
            b'[' => self.parse_array(depth + 1),
            b'{' => self.parse_object(depth + 1),
            b'N' => {
                if self.starts_with(b"NaN") {
                    self.pos += 3;
                    Ok(JsonValue::Float(f64::NAN))
                } else {
                    Err(())
                }
            }
            b'I' => {
                if self.starts_with(b"Infinity") {
                    self.pos += 8;
                    Ok(JsonValue::Float(f64::INFINITY))
                } else {
                    Err(())
                }
            }
            b'-' | b'0'..=b'9' => self.parse_number(),
            _ => Err(()),
        }
    }

    fn parse_string(&mut self) -> Result<String, ()> {
        if self.peek() != Some(b'"') {
            return Err(());
        }
        self.pos += 1;
        let mut out = String::new();
        while let Some(&b) = self.bytes.get(self.pos) {
            if b == b'"' {
                self.pos += 1;
                return Ok(out);
            }
            if b < 0x20 {
                return Err(());
            }
            if b == b'\\' {
                self.pos += 1;
                let esc = self.peek().ok_or(())?;
                self.pos += 1;
                match esc {
                    b'"' => out.push('"'),
                    b'\\' => out.push('\\'),
                    b'/' => out.push('/'),
                    b'b' => out.push('\u{0008}'),
                    b'f' => out.push('\u{000C}'),
                    b'n' => out.push('\n'),
                    b'r' => out.push('\r'),
                    b't' => out.push('\t'),
                    b'u' => {
                        let code = self.parse_hex4()?;
                        if (0xD800..=0xDBFF).contains(&code) {
                            if !self.starts_with(b"\\u") {
                                return Err(());
                            }
                            self.pos += 2;
                            let low = self.parse_hex4()?;
                            if !(0xDC00..=0xDFFF).contains(&low) {
                                return Err(());
                            }
                            let cp = 0x10000
                                + ((u32::from(code) - 0xD800) << 10)
                                + (u32::from(low) - 0xDC00);
                            let ch = char::from_u32(cp).ok_or(())?;
                            out.push(ch);
                        } else if (0xDC00..=0xDFFF).contains(&code) {
                            return Err(());
                        } else {
                            let ch = char::from_u32(u32::from(code)).ok_or(())?;
                            out.push(ch);
                        }
                    }
                    _ => return Err(()),
                }
            } else {
                let start = self.pos;
                while let Some(&c) = self.bytes.get(self.pos) {
                    if c == b'"' || c == b'\\' || c < 0x20 {
                        break;
                    }
                    self.pos += 1;
                }
                let slice = std::str::from_utf8(&self.bytes[start..self.pos]).map_err(|_| ())?;
                out.push_str(slice);
            }
        }
        Err(())
    }

    fn parse_hex4(&mut self) -> Result<u16, ()> {
        let slice = self.bytes.get(self.pos..self.pos + 4).ok_or(())?;
        self.pos += 4;
        let text = std::str::from_utf8(slice).map_err(|_| ())?;
        u16::from_str_radix(text, 16).map_err(|_| ())
    }

    fn parse_number(&mut self) -> Result<JsonValue, ()> {
        if self.starts_with(b"-Infinity") {
            self.pos += 9;
            return Ok(JsonValue::Float(f64::NEG_INFINITY));
        }
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        match self.peek() {
            Some(b'0') => {
                self.pos += 1;
                if matches!(self.peek(), Some(b'0'..=b'9')) {
                    return Err(());
                }
            }
            Some(b'1'..=b'9') => {
                self.pos += 1;
                while matches!(self.peek(), Some(b'0'..=b'9')) {
                    self.pos += 1;
                }
            }
            _ => return Err(()),
        }
        let mut is_float = false;
        if self.peek() == Some(b'.') {
            is_float = true;
            self.pos += 1;
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return Err(());
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.pos += 1;
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            is_float = true;
            self.pos += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.pos += 1;
            }
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return Err(());
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.pos += 1;
            }
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos]).map_err(|_| ())?;
        if !is_float && let Ok(i) = text.parse::<i64>() {
            return Ok(JsonValue::Int(i));
        }
        let f = text.parse::<f64>().map_err(|_| ())?;
        Ok(JsonValue::Float(f))
    }

    fn parse_array(&mut self, depth: usize) -> Result<JsonValue, ()> {
        self.pos += 1; // '['
        self.skip_ws();
        let mut items = Vec::new();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(JsonValue::Array(items));
        }
        loop {
            items.push(self.parse_value(depth)?);
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b']') => {
                    self.pos += 1;
                    return Ok(JsonValue::Array(items));
                }
                _ => return Err(()),
            }
        }
    }

    fn parse_object(&mut self, depth: usize) -> Result<JsonValue, ()> {
        self.pos += 1; // '{'
        self.skip_ws();
        let mut pairs = Vec::new();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(JsonValue::Object(pairs));
        }
        loop {
            self.skip_ws();
            let key = self.parse_string()?;
            self.skip_ws();
            if self.peek() != Some(b':') {
                return Err(());
            }
            self.pos += 1;
            let val = self.parse_value(depth)?;
            if let Some(pos) = pairs.iter().position(|(k, _)| k == &key) {
                pairs[pos].1 = val;
            } else {
                pairs.push((key, val));
            }
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b'}') => {
                    self.pos += 1;
                    return Ok(JsonValue::Object(pairs));
                }
                _ => return Err(()),
            }
        }
    }
}
