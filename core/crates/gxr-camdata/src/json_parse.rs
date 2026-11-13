//! Minimal recursive-descent JSON parser for embedded `final.json` (no serde in core crates).

use gxr_raw::CoreError;

#[derive(Clone, Debug, PartialEq)]
pub enum Val {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Array(Vec<Val>),
    Object(Vec<(String, Val)>),
}

impl Val {
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Self::Int(i) => Some(*i),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Val]> {
        match self {
            Self::Array(a) => Some(a),
            _ => None,
        }
    }

    pub fn field(&self, key: &str) -> Option<&Val> {
        match self {
            Self::Object(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn err(&self, msg: &str) -> CoreError {
        CoreError::new(
            "camdata",
            format!("json parse error at {}: {msg}", self.pos),
        )
    }

    fn skip_ws(&mut self) {
        while let Some(&b) = self.bytes.get(self.pos) {
            if matches!(b, b' ' | b'\t' | b'\r' | b'\n') {
                self.pos += 1;
            } else {
                break;
            }
        }
    }

    fn peek(&mut self) -> Result<u8, CoreError> {
        self.skip_ws();
        self.bytes
            .get(self.pos)
            .copied()
            .ok_or_else(|| self.err("unexpected eof"))
    }

    fn expect(&mut self, ch: u8) -> Result<(), CoreError> {
        let got = self.peek()?;
        if got != ch {
            return Err(self.err("unexpected char"));
        }
        self.pos += 1;
        Ok(())
    }

    fn parse_val(&mut self, depth: usize) -> Result<Val, CoreError> {
        if depth > 64 {
            return Err(self.err("nesting too deep"));
        }
        match self.peek()? {
            b'n' => {
                if self.bytes.get(self.pos..self.pos + 4) == Some(b"null") {
                    self.pos += 4;
                    Ok(Val::Null)
                } else {
                    Err(self.err("expected null"))
                }
            }
            b't' => {
                if self.bytes.get(self.pos..self.pos + 4) == Some(b"true") {
                    self.pos += 4;
                    Ok(Val::Bool(true))
                } else {
                    Err(self.err("expected true"))
                }
            }
            b'f' => {
                if self.bytes.get(self.pos..self.pos + 5) == Some(b"false") {
                    self.pos += 5;
                    Ok(Val::Bool(false))
                } else {
                    Err(self.err("expected false"))
                }
            }
            b'"' => self.parse_str().map(Val::Str),
            b'[' => self.parse_array(depth + 1),
            b'{' => self.parse_object(depth + 1),
            b'-' | b'0'..=b'9' => self.parse_num(),
            _ => Err(self.err("unexpected token")),
        }
    }

    fn parse_str(&mut self) -> Result<String, CoreError> {
        self.expect(b'"')?;
        let mut out = String::new();
        while let Some(&b) = self.bytes.get(self.pos) {
            self.pos += 1;
            if b == b'"' {
                return Ok(out);
            }
            if b == b'\\' {
                let esc = self
                    .bytes
                    .get(self.pos)
                    .copied()
                    .ok_or_else(|| self.err("unterminated escape"))?;
                self.pos += 1;
                match esc {
                    b'"' => out.push('"'),
                    b'\\' => out.push('\\'),
                    b'/' => out.push('/'),
                    b'b' => out.push('\x08'),
                    b'f' => out.push('\x0c'),
                    b'n' => out.push('\n'),
                    b'r' => out.push('\r'),
                    b't' => out.push('\t'),
                    b'u' => {
                        let hex = self
                            .bytes
                            .get(self.pos..self.pos + 4)
                            .ok_or_else(|| self.err("short unicode escape"))?;
                        let s = std::str::from_utf8(hex)
                            .map_err(|_| self.err("invalid unicode escape"))?;
                        let code = u16::from_str_radix(s, 16)
                            .map_err(|_| self.err("invalid hex in unicode escape"))?;
                        self.pos += 4;
                        let ch = if (0xD800..=0xDBFF).contains(&code) {
                            if self.bytes.get(self.pos..self.pos + 2) != Some(b"\\u") {
                                return Err(self.err("missing low surrogate"));
                            }
                            self.pos += 2;
                            let hex2 = self
                                .bytes
                                .get(self.pos..self.pos + 4)
                                .ok_or_else(|| self.err("short low surrogate"))?;
                            let s2 = std::str::from_utf8(hex2)
                                .map_err(|_| self.err("invalid low surrogate"))?;
                            let low = u16::from_str_radix(s2, 16)
                                .map_err(|_| self.err("invalid hex in low surrogate"))?;
                            if !(0xDC00..=0xDFFF).contains(&low) {
                                return Err(self.err("invalid low surrogate range"));
                            }
                            self.pos += 4;
                            let combined = 0x10000
                                + (((u32::from(code) - 0xD800) << 10) | (u32::from(low) - 0xDC00));
                            char::from_u32(combined)
                                .ok_or_else(|| self.err("invalid surrogate scalar"))?
                        } else {
                            char::from_u32(u32::from(code))
                                .ok_or_else(|| self.err("invalid code point"))?
                        };
                        out.push(ch);
                    }
                    _ => return Err(self.err("unknown escape")),
                }
            } else if b < 0x20 {
                return Err(self.err("unescaped control char"));
            } else if b < 0x80 {
                out.push(char::from(b));
            } else {
                let start = self.pos - 1;
                while self.pos < self.bytes.len() && (self.bytes[self.pos] & 0xC0) == 0x80 {
                    self.pos += 1;
                }
                let s = std::str::from_utf8(&self.bytes[start..self.pos])
                    .map_err(|_| self.err("invalid utf-8"))?;
                out.push_str(s);
            }
        }
        Err(self.err("unterminated string"))
    }

    fn parse_array(&mut self, depth: usize) -> Result<Val, CoreError> {
        self.expect(b'[')?;
        let mut out = Vec::new();
        if self.peek()? == b']' {
            self.pos += 1;
            return Ok(Val::Array(out));
        }
        loop {
            out.push(self.parse_val(depth)?);
            match self.peek()? {
                b',' => self.pos += 1,
                b']' => {
                    self.pos += 1;
                    return Ok(Val::Array(out));
                }
                _ => return Err(self.err("expected ',' or ']'")),
            }
        }
    }

    fn parse_object(&mut self, depth: usize) -> Result<Val, CoreError> {
        self.expect(b'{')?;
        let mut out = Vec::new();
        if self.peek()? == b'}' {
            self.pos += 1;
            return Ok(Val::Object(out));
        }
        loop {
            if self.peek()? != b'"' {
                return Err(self.err("expected string key"));
            }
            let key = self.parse_str()?;
            self.expect(b':')?;
            let val = self.parse_val(depth)?;
            out.push((key, val));
            match self.peek()? {
                b',' => self.pos += 1,
                b'}' => {
                    self.pos += 1;
                    return Ok(Val::Object(out));
                }
                _ => return Err(self.err("expected ',' or '}'")),
            }
        }
    }

    fn parse_num(&mut self) -> Result<Val, CoreError> {
        let start = self.pos;
        if self.bytes.get(self.pos) == Some(&b'-') {
            self.pos += 1;
        }
        let mut is_float = false;
        while let Some(&b) = self.bytes.get(self.pos) {
            if b.is_ascii_digit() {
                self.pos += 1;
            } else if matches!(b, b'.' | b'e' | b'E' | b'+' | b'-') {
                is_float = true;
                self.pos += 1;
            } else {
                break;
            }
        }
        let s = std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|_| self.err("invalid number"))?;
        if is_float {
            let f: f64 = s.parse().map_err(|_| self.err("invalid float"))?;
            Ok(Val::Float(f))
        } else {
            let i: i64 = s.parse().map_err(|_| self.err("invalid int"))?;
            Ok(Val::Int(i))
        }
    }
}

pub fn parse_json(bytes: &[u8]) -> Result<Val, CoreError> {
    let mut p = Parser { bytes, pos: 0 };
    let v = p.parse_val(0)?;
    p.skip_ws();
    if p.pos != bytes.len() {
        return Err(p.err("trailing data"));
    }
    Ok(v)
}
