//! Python `json.loads`-compatible parser for edit/recipe JSON.
//!
//! Preserves arbitrary-precision integers (so `10**20` is handled like Python), distinguishes
//! `int` from `float` (`1` vs `1.0`), deduplicates object keys keeping first-seen order and last
//! value, accepts `NaN`/`Infinity`/`-Infinity`, and enforces `MAX_JSON_BYTES` and `MAX_DEPTH`.

use std::cmp::Ordering;
use std::collections::BTreeMap;

use gxr_raw::CoreError;

pub const MAX_JSON_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_DEPTH: usize = 128;

/// Python `str`-compatible string that preserves unpaired UTF-16 surrogate code units
/// (`0xD800..=0xDFFF`) instead of replacing them with U+FFFD.
#[derive(Clone, Debug)]
pub struct PyStr(PyStrRepr);

#[derive(Clone, Debug)]
enum PyStrRepr {
    Utf8(String),
    CodePoints(Vec<u32>),
}

impl PyStr {
    pub fn as_str(&self) -> Option<&str> {
        match &self.0 {
            PyStrRepr::Utf8(s) => Some(s),
            PyStrRepr::CodePoints(_) => None,
        }
    }

    pub fn is_empty(&self) -> bool {
        match &self.0 {
            PyStrRepr::Utf8(s) => s.is_empty(),
            PyStrRepr::CodePoints(v) => v.is_empty(),
        }
    }

    fn from_code_points(v: Vec<u32>) -> Self {
        Self(PyStrRepr::CodePoints(v))
    }

    fn code_points(&self) -> Box<dyn Iterator<Item = u32> + '_> {
        match &self.0 {
            PyStrRepr::Utf8(s) => Box::new(s.chars().map(|c| c as u32)),
            PyStrRepr::CodePoints(v) => Box::new(v.iter().copied()),
        }
    }

    /// Write as a JSON string literal, escaping lone surrogates as `\uXXXX`.
    pub fn write_json(&self, out: &mut String) {
        out.push('"');
        for cp in self.code_points() {
            match char::from_u32(cp) {
                Some('"') => out.push_str("\\\""),
                Some('\\') => out.push_str("\\\\"),
                Some('\n') => out.push_str("\\n"),
                Some('\r') => out.push_str("\\r"),
                Some('\t') => out.push_str("\\t"),
                Some(c) if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
                Some(c) => out.push(c),
                None => out.push_str(&format!("\\u{cp:04x}")),
            }
        }
        out.push('"');
    }

    /// Lossless UTF-8 text: valid scalar characters verbatim, lone surrogates as `\uXXXX`.
    pub fn to_escaped_text(&self) -> String {
        match &self.0 {
            PyStrRepr::Utf8(s) => s.clone(),
            PyStrRepr::CodePoints(v) => {
                let mut out = String::new();
                for &cp in v {
                    match char::from_u32(cp) {
                        Some(c) => out.push(c),
                        None => out.push_str(&format!("\\u{cp:04x}")),
                    }
                }
                out
            }
        }
    }
}

impl From<String> for PyStr {
    fn from(s: String) -> Self {
        Self(PyStrRepr::Utf8(s))
    }
}

impl From<&str> for PyStr {
    fn from(s: &str) -> Self {
        Self(PyStrRepr::Utf8(s.to_string()))
    }
}

impl PartialEq for PyStr {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}

impl Eq for PyStr {}

impl PartialOrd for PyStr {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for PyStr {
    fn cmp(&self, other: &Self) -> Ordering {
        match (&self.0, &other.0) {
            (PyStrRepr::Utf8(a), PyStrRepr::Utf8(b)) => a.cmp(b),
            _ => self.code_points().cmp(other.code_points()),
        }
    }
}

impl PartialEq<str> for PyStr {
    fn eq(&self, other: &str) -> bool {
        self.as_str() == Some(other)
    }
}

impl PartialEq<&str> for PyStr {
    fn eq(&self, other: &&str) -> bool {
        self.as_str() == Some(*other)
    }
}

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
    Str(PyStr),
    Array(Vec<PyJson>),
    Object(Vec<(PyStr, PyJson)>),
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
            PyJson::Str(s) => s.as_str(),
            _ => None,
        }
    }

    pub fn as_object(&self) -> Option<&[(PyStr, PyJson)]> {
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

    /// Render as compact JSON text, escaping lone surrogates as `\uXXXX`.
    pub fn render(&self) -> String {
        let mut out = String::new();
        self.write_json(&mut out);
        out
    }

    fn write_json(&self, out: &mut String) {
        match self {
            Self::Null => out.push_str("null"),
            Self::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Self::Num(PyNum::Int(i)) => out.push_str(&i.to_string()),
            Self::Num(PyNum::BigInt { neg, digits }) => {
                if *neg {
                    out.push('-');
                }
                out.push_str(digits);
            }
            Self::Num(PyNum::Float(f)) => out.push_str(&gxr_raw::json::float_text(*f)),
            Self::Str(s) => s.write_json(out),
            Self::Array(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    item.write_json(out);
                }
                out.push(']');
            }
            Self::Object(pairs) => {
                out.push('{');
                for (i, (k, v)) in pairs.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    k.write_json(out);
                    out.push(':');
                    v.write_json(out);
                }
                out.push('}');
            }
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
        let mut pairs: Vec<(PyStr, PyJson)> = Vec::new();
        let mut seen: BTreeMap<PyStr, usize> = BTreeMap::new();
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

    fn string(&mut self) -> Result<PyStr, CoreError> {
        self.bump()?;
        let mut utf8 = String::new();
        let mut units: Option<Vec<u32>> = None;
        loop {
            match self.bump()? {
                '"' => {
                    return Ok(match units {
                        Some(v) => PyStr::from_code_points(v),
                        None => PyStr::from(utf8),
                    });
                }
                '\\' => {
                    let cp = self.escape_cp()?;
                    push_cp(&mut utf8, &mut units, cp);
                }
                c if (c as u32) < 0x20 => return Err(bad("control character in string")),
                c => push_cp(&mut utf8, &mut units, c as u32),
            }
        }
    }

    fn escape_cp(&mut self) -> Result<u32, CoreError> {
        match self.bump()? {
            '"' => Ok('"' as u32),
            '\\' => Ok('\\' as u32),
            '/' => Ok('/' as u32),
            'b' => Ok(0x0008),
            'f' => Ok(0x000c),
            'n' => Ok('\n' as u32),
            'r' => Ok('\r' as u32),
            't' => Ok('\t' as u32),
            'u' => self.unicode(),
            _ => Err(bad("bad escape")),
        }
    }

    fn unicode(&mut self) -> Result<u32, CoreError> {
        let first = self.hex4()?;
        if (0xD800..0xDC00).contains(&first) && self.text[self.i..].starts_with("\\u") {
            let save = self.i;
            self.i += 2;
            let second = self.hex4()?;
            if (0xDC00..0xE000).contains(&second) {
                return Ok(0x10000 + ((first - 0xD800) << 10) + (second - 0xDC00));
            }
            self.i = save;
        }
        Ok(first)
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

fn push_cp(utf8: &mut String, units: &mut Option<Vec<u32>>, cp: u32) {
    if let Some(v) = units.as_mut() {
        v.push(cp);
    } else if let Some(c) = char::from_u32(cp) {
        utf8.push(c);
    } else {
        let mut v: Vec<u32> = utf8.chars().map(|ch| ch as u32).collect();
        v.push(cp);
        utf8.clear();
        *units = Some(v);
    }
}
