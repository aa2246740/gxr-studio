//! Minimal strict JSON parser for EXIF metadata input (no serde in core crates).

use gxr_raw::CoreError;

#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Array(Vec<Value>),
    Object(Vec<(String, Value)>),
}

const MAX_DEPTH: usize = 32;

pub fn parse(input: &str) -> Result<Value, CoreError> {
    let mut p = Parser {
        bytes: input.as_bytes(),
        pos: 0,
    };
    p.skip_ws();
    let val = p.parse_value(0)?;
    p.skip_ws();
    if p.pos != p.bytes.len() {
        return Err(CoreError::new(
            "bad_exif",
            format!("unexpected trailing JSON at byte {}", p.pos),
        ));
    }
    Ok(val)
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn err(&self, msg: impl Into<String>) -> CoreError {
        CoreError::new("bad_exif", msg)
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn next(&mut self) -> Result<u8, CoreError> {
        let b = self
            .peek()
            .ok_or_else(|| self.err("unexpected end of JSON"))?;
        self.pos += 1;
        Ok(b)
    }

    fn skip_ws(&mut self) {
        while let Some(b) = self.peek() {
            if matches!(b, b' ' | b'\t' | b'\n' | b'\r') {
                self.pos += 1;
            } else {
                break;
            }
        }
    }

    fn parse_value(&mut self, depth: usize) -> Result<Value, CoreError> {
        if depth > MAX_DEPTH {
            return Err(self.err("JSON nesting too deep"));
        }
        self.skip_ws();
        let b = self
            .peek()
            .ok_or_else(|| self.err("unexpected end of JSON"))?;
        match b {
            b'n' => self.parse_lit(b"null", Value::Null),
            b't' => self.parse_lit(b"true", Value::Bool(true)),
            b'f' => self.parse_lit(b"false", Value::Bool(false)),
            b'"' => self.parse_string().map(Value::Str),
            b'[' => self.parse_array(depth + 1),
            b'{' => self.parse_object(depth + 1),
            b'-' | b'0'..=b'9' => self.parse_number(),
            _ => Err(self.err(format!("unexpected JSON byte at {}", self.pos))),
        }
    }

    fn parse_lit(&mut self, lit: &[u8], out: Value) -> Result<Value, CoreError> {
        if self.bytes.get(self.pos..self.pos + lit.len()) == Some(lit) {
            self.pos += lit.len();
            Ok(out)
        } else {
            Err(self.err(format!("invalid JSON literal at byte {}", self.pos)))
        }
    }

    fn parse_string(&mut self) -> Result<String, CoreError> {
        if self.next()? != b'"' {
            return Err(self.err("expected '\"'"));
        }
        let mut out = String::new();
        loop {
            let b = self.next()?;
            match b {
                b'"' => return Ok(out),
                b'\\' => {
                    let esc = self.next()?;
                    match esc {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{0008}'),
                        b'f' => out.push('\u{000c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let code = self.parse_hex4()?;
                            if (0xD800..=0xDBFF).contains(&code) {
                                if self.next()? != b'\\' || self.next()? != b'u' {
                                    return Err(self.err("unpaired UTF-16 high surrogate"));
                                }
                                let low = self.parse_hex4()?;
                                if !(0xDC00..=0xDFFF).contains(&low) {
                                    return Err(self.err("invalid UTF-16 low surrogate"));
                                }
                                let cp = 0x10000
                                    + (((u32::from(code) - 0xD800) << 10)
                                        | (u32::from(low) - 0xDC00));
                                let ch = char::from_u32(cp)
                                    .ok_or_else(|| self.err("invalid unicode scalar"))?;
                                out.push(ch);
                            } else {
                                let ch = char::from_u32(u32::from(code))
                                    .ok_or_else(|| self.err("invalid unicode scalar"))?;
                                out.push(ch);
                            }
                        }
                        _ => return Err(self.err("invalid JSON escape")),
                    }
                }
                0x00..=0x1F => return Err(self.err("unescaped control character in JSON string")),
                _ => {
                    let start = self.pos - 1;
                    let width = utf8_width(b)
                        .ok_or_else(|| self.err("invalid UTF-8 leading byte in JSON"))?;
                    let end = start + width;
                    let slice = self
                        .bytes
                        .get(start..end)
                        .ok_or_else(|| self.err("truncated UTF-8 in JSON string"))?;
                    let s = std::str::from_utf8(slice)
                        .map_err(|_| self.err("invalid UTF-8 in JSON string"))?;
                    out.push_str(s);
                    self.pos = end;
                }
            }
        }
    }

    fn parse_hex4(&mut self) -> Result<u16, CoreError> {
        let mut val: u16 = 0;
        for _ in 0..4 {
            let b = self.next()?;
            let d = match b {
                b'0'..=b'9' => b - b'0',
                b'a'..=b'f' => b - b'a' + 10,
                b'A'..=b'F' => b - b'A' + 10,
                _ => return Err(self.err("invalid hex digit in \\u escape")),
            };
            val = (val << 4) | u16::from(d);
        }
        Ok(val)
    }

    fn parse_number(&mut self) -> Result<Value, CoreError> {
        let start = self.pos;
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
            _ => return Err(self.err("invalid JSON number")),
        }
        let mut is_float = false;
        if self.peek() == Some(b'.') {
            is_float = true;
            self.pos += 1;
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return Err(self.err("invalid JSON fraction"));
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
                return Err(self.err("invalid JSON exponent"));
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.pos += 1;
            }
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|_| self.err("invalid UTF-8 in number"))?;
        if is_float {
            let f: f64 = text.parse().map_err(|_| self.err("invalid JSON float"))?;
            if !f.is_finite() {
                return Err(self.err("non-finite JSON float"));
            }
            Ok(Value::Float(f))
        } else if let Ok(i) = text.parse::<i64>() {
            Ok(Value::Int(i))
        } else {
            let f: f64 = text.parse().map_err(|_| self.err("invalid JSON integer"))?;
            if !f.is_finite() {
                return Err(self.err("non-finite JSON number"));
            }
            Ok(Value::Float(f))
        }
    }

    fn parse_array(&mut self, depth: usize) -> Result<Value, CoreError> {
        self.next()?;
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Value::Array(items));
        }
        loop {
            items.push(self.parse_value(depth)?);
            self.skip_ws();
            match self.next()? {
                b',' => continue,
                b']' => return Ok(Value::Array(items)),
                _ => return Err(self.err("expected ',' or ']' in JSON array")),
            }
        }
    }

    fn parse_object(&mut self, depth: usize) -> Result<Value, CoreError> {
        self.next()?;
        let mut pairs = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Value::Object(pairs));
        }
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return Err(self.err("expected string key in JSON object"));
            }
            let key = self.parse_string()?;
            self.skip_ws();
            if self.next()? != b':' {
                return Err(self.err("expected ':' after JSON object key"));
            }
            let val = self.parse_value(depth)?;
            pairs.push((key, val));
            self.skip_ws();
            match self.next()? {
                b',' => continue,
                b'}' => return Ok(Value::Object(pairs)),
                _ => return Err(self.err("expected ',' or '}' in JSON object")),
            }
        }
    }
}

fn utf8_width(first: u8) -> Option<usize> {
    if first < 0x80 {
        Some(1)
    } else if (first & 0xE0) == 0xC0 {
        Some(2)
    } else if (first & 0xF0) == 0xE0 {
        Some(3)
    } else if (first & 0xF8) == 0xF0 {
        Some(4)
    } else {
        None
    }
}
