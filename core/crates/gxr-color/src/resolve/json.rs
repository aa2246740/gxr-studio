//! Python `json.loads`-compatible parser for edit/recipe JSON.
//!
//! Preserves arbitrary-precision integers (so `10**20` is handled like Python), distinguishes
//! `int` from `float` (`1` vs `1.0`), deduplicates object keys keeping first-seen order and last
//! value, accepts `NaN`/`Infinity`/`-Infinity`, and enforces `MAX_JSON_BYTES` and `MAX_DEPTH`.

use std::collections::BTreeMap;

use gxr_raw::CoreError;

pub const MAX_JSON_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_DEPTH: usize = 128;

#[derive(Clone, Debug, PartialEq)]
pub enum PyNum {
    Int(i64),
    BigInt { neg: bool, digits: String },
    Float(f64),
}

#[derive(Clone, Debug, PartialEq)]
pub enum PyJson {
    Null,
    Bool(bool),
    Num(PyNum),
    Str(String),
    Array(Vec<PyJson>),
    Object(Vec<(String, PyJson)>),
}

impl PyJson {
    pub fn get(&self, key: &str) -> Option<&PyJson> {
        let PyJson::Object(pairs) = self else {
            return None;
        };
        pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            PyJson::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_object(&self) -> Option<&[(String, PyJson)]> {
        match self {
            PyJson::Object(pairs) => Some(pairs),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[PyJson]> {
        match self {
            PyJson::Array(items) => Some(items),
            _ => None,
        }
    }

    /// Python `bool(x)` truthiness.
    pub fn is_truthy(&self) -> bool {
        match self {
            PyJson::Null => false,
            PyJson::Bool(b) => *b,
            PyJson::Num(PyNum::Int(i)) => *i != 0,
            PyJson::Num(PyNum::BigInt { .. }) => true,
            PyJson::Num(PyNum::Float(f)) => *f != 0.0,
            PyJson::Str(s) => !s.is_empty(),
            PyJson::Array(items) => !items.is_empty(),
            PyJson::Object(pairs) => !pairs.is_empty(),
        }
    }
}

pub fn parse(bytes: &[u8]) -> Result<PyJson, CoreError> {
    if bytes.len() > MAX_JSON_BYTES {
        return Err(CoreError::new("json", "JSON 文件过大"));
    }
    let text = std::str::from_utf8(bytes).map_err(|_| bad("invalid utf-8"))?;
    let mut p = Parser {
        text,
        i: 0,
        depth: 0,
    };
    p.skip();
    let value = p.value()?;
    p.skip();
    if p.i != text.len() {
        return Err(bad("trailing junk"));
    }
    Ok(value)
}

fn bad(message: &str) -> CoreError {
    CoreError::new("json", message)
}

struct Parser<'a> {
    text: &'a str,
    i: usize,
    depth: usize,
}

impl<'a> Parser<'a> {
    fn skip(&mut self) {
        while let Some(c) = self.peek() {
            if c == ' ' || c == '\t' || c == '\n' || c == '\r' {
                self.i += c.len_utf8();
            } else {
                break;
            }
        }
    }

    fn peek(&self) -> Option<char> {
        self.text[self.i..].chars().next()
    }

    fn bump(&mut self) -> Result<char, CoreError> {
        let c = self.peek().ok_or_else(|| bad("unexpected end"))?;
        self.i += c.len_utf8();
        Ok(c)
    }

    fn value(&mut self) -> Result<PyJson, CoreError> {
        self.skip();
        match self.peek().ok_or_else(|| bad("unexpected end"))? {
            'n' => self.literal("null", PyJson::Null),
            't' => self.literal("true", PyJson::Bool(true)),
            'f' => self.literal("false", PyJson::Bool(false)),
            '"' => Ok(PyJson::Str(self.string()?)),
            '[' => self.nested(Self::array),
            '{' => self.nested(Self::object),
            'N' => self.literal("NaN", PyJson::Num(PyNum::Float(f64::NAN))),
            'I' => self.literal("Infinity", PyJson::Num(PyNum::Float(f64::INFINITY))),
            '-' if self.text[self.i..].starts_with("-Infinity") => {
                self.literal("-Infinity", PyJson::Num(PyNum::Float(f64::NEG_INFINITY)))
            }
            '-' | '0'..='9' => self.number(),
            _ => Err(bad("expected a value")),
        }
    }

    fn literal(&mut self, word: &str, value: PyJson) -> Result<PyJson, CoreError> {
        if self.text[self.i..].starts_with(word) {
            self.i += word.len();
            Ok(value)
        } else {
            Err(bad("bad literal"))
        }
    }

    fn nested(
        &mut self,
        f: fn(&mut Self) -> Result<PyJson, CoreError>,
    ) -> Result<PyJson, CoreError> {
        if self.depth >= MAX_DEPTH {
            return Err(CoreError::new("json", "JSON 嵌套太深"));
        }
        self.depth += 1;
        let out = f(self);
        self.depth -= 1;
        out
    }

    fn array(&mut self) -> Result<PyJson, CoreError> {
        self.bump()?;
        let mut items = Vec::new();
        self.skip();
        if self.peek() == Some(']') {
            self.bump()?;
            return Ok(PyJson::Array(items));
        }
        loop {
            items.push(self.value()?);
            self.skip();
            match self.bump()? {
                ']' => return Ok(PyJson::Array(items)),
                ',' => continue,
                _ => return Err(bad("expected ',' or ']'")),
            }
        }
    }

    fn object(&mut self) -> Result<PyJson, CoreError> {
        self.bump()?;
        let mut pairs: Vec<(String, PyJson)> = Vec::new();
        let mut seen: BTreeMap<String, usize> = BTreeMap::new();
        self.skip();
        if self.peek() == Some('}') {
            self.bump()?;
            return Ok(PyJson::Object(pairs));
        }
        loop {
            self.skip();
            if self.peek() != Some('"') {
                return Err(bad("expected a key"));
            }
            let key = self.string()?;
            self.skip();
            if self.bump()? != ':' {
                return Err(bad("expected ':'"));
            }
            let val = self.value()?;
            if let Some(&idx) = seen.get(&key) {
                pairs[idx].1 = val;
            } else {
                let idx = pairs.len();
                seen.insert(key.clone(), idx);
                pairs.push((key, val));
            }
            self.skip();
            match self.bump()? {
                '}' => return Ok(PyJson::Object(pairs)),
                ',' => continue,
                _ => return Err(bad("expected ',' or '}'")),
            }
        }
    }

    fn string(&mut self) -> Result<String, CoreError> {
        self.bump()?;
        let mut out = String::new();
        loop {
            match self.bump()? {
                '"' => return Ok(out),
                '\\' => out.push(self.escape()?),
                c if (c as u32) < 0x20 => return Err(bad("control character in string")),
                c => out.push(c),
            }
        }
    }

    fn escape(&mut self) -> Result<char, CoreError> {
        match self.bump()? {
            '"' => Ok('"'),
            '\\' => Ok('\\'),
            '/' => Ok('/'),
            'b' => Ok('\u{0008}'),
            'f' => Ok('\u{000c}'),
            'n' => Ok('\n'),
            'r' => Ok('\r'),
            't' => Ok('\t'),
            'u' => self.unicode(),
            _ => Err(bad("bad escape")),
        }
    }

    fn unicode(&mut self) -> Result<char, CoreError> {
        let first = self.hex4()?;
        if (0xD800..0xDC00).contains(&first) && self.text[self.i..].starts_with("\\u") {
            let save = self.i;
            self.i += 2;
            let second = self.hex4()?;
            if (0xDC00..0xE000).contains(&second) {
                let code = 0x10000 + ((first - 0xD800) << 10) + (second - 0xDC00);
                return char::from_u32(code).ok_or_else(|| bad("bad unicode escape"));
            }
            self.i = save;
        }
        char::from_u32(first).ok_or_else(|| bad("bad unicode escape"))
    }

    fn hex4(&mut self) -> Result<u32, CoreError> {
        let mut code = 0u32;
        for _ in 0..4 {
            let c = self.bump()?;
            let digit = c.to_digit(16).ok_or_else(|| bad("bad unicode escape"))?;
            code = (code << 4) | digit;
        }
        Ok(code)
    }

    fn number(&mut self) -> Result<PyJson, CoreError> {
        let start = self.i;
        if self.peek() == Some('-') {
            self.bump()?;
        }
        match self.peek() {
            Some('0') => {
                self.bump()?;
            }
            Some('1'..='9') => {
                while matches!(self.peek(), Some('0'..='9')) {
                    self.bump()?;
                }
            }
            _ => return Err(bad("bad number")),
        }
        let mut is_float = false;
        if self.peek() == Some('.') {
            is_float = true;
            self.bump()?;
            if !matches!(self.peek(), Some('0'..='9')) {
                return Err(bad("bad number"));
            }
            while matches!(self.peek(), Some('0'..='9')) {
                self.bump()?;
            }
        }
        if matches!(self.peek(), Some('e' | 'E')) {
            is_float = true;
            self.bump()?;
            if matches!(self.peek(), Some('+' | '-')) {
                self.bump()?;
            }
            if !matches!(self.peek(), Some('0'..='9')) {
                return Err(bad("bad number"));
            }
            while matches!(self.peek(), Some('0'..='9')) {
                self.bump()?;
            }
        }
        let text = &self.text[start..self.i];
        if is_float {
            let v: f64 = text.parse().map_err(|_| bad("bad number"))?;
            Ok(PyJson::Num(PyNum::Float(v)))
        } else if let Ok(v) = text.parse::<i64>() {
            Ok(PyJson::Num(PyNum::Int(v)))
        } else {
            let neg = text.starts_with('-');
            let digits = text.trim_start_matches('-').to_string();
            Ok(PyJson::Num(PyNum::BigInt { neg, digits }))
        }
    }
}
