//! A JSON reader for menu tables and CLI parameter files.
//!
//! Numbers without a fraction or an exponent are integers, so a UI level of `1` stays an int
//! and `1.0` stays a float. That matches `type(x) is int` in `menu.request`.

use gxr_raw::CoreError;

/// One JSON value.
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

impl Value {
    pub fn get(&self, key: &str) -> Option<&Value> {
        let Value::Object(pairs) = self else {
            return None;
        };
        pairs.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    pub fn as_int(&self) -> Option<i64> {
        match self {
            Value::Int(v) => Some(*v),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Value]> {
        match self {
            Value::Array(items) => Some(items),
            _ => None,
        }
    }
}

/// Parse one JSON value. Trailing whitespace is allowed; trailing junk is not.
pub fn parse(bytes: &[u8]) -> Result<Value, CoreError> {
    let text = std::str::from_utf8(bytes).map_err(|_| bad("invalid utf-8"))?;
    let mut p = Parser { text, i: 0 };
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

    fn value(&mut self) -> Result<Value, CoreError> {
        self.skip();
        match self.peek().ok_or_else(|| bad("unexpected end"))? {
            'n' => self.literal("null", Value::Null),
            't' => self.literal("true", Value::Bool(true)),
            'f' => self.literal("false", Value::Bool(false)),
            '"' => Ok(Value::Str(self.string()?)),
            '[' => self.array(),
            '{' => self.object(),
            '-' | '0'..='9' => self.number(),
            _ => Err(bad("expected a value")),
        }
    }

    fn literal(&mut self, word: &str, value: Value) -> Result<Value, CoreError> {
        if self.text[self.i..].starts_with(word) {
            self.i += word.len();
            Ok(value)
        } else {
            Err(bad("bad literal"))
        }
    }

    fn array(&mut self) -> Result<Value, CoreError> {
        self.bump()?;
        let mut items = Vec::new();
        self.skip();
        if self.peek() == Some(']') {
            self.bump()?;
            return Ok(Value::Array(items));
        }
        loop {
            items.push(self.value()?);
            self.skip();
            match self.bump()? {
                ']' => return Ok(Value::Array(items)),
                ',' => continue,
                _ => return Err(bad("expected ',' or ']'")),
            }
        }
    }

    fn object(&mut self) -> Result<Value, CoreError> {
        self.bump()?;
        let mut pairs = Vec::new();
        self.skip();
        if self.peek() == Some('}') {
            self.bump()?;
            return Ok(Value::Object(pairs));
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
            pairs.push((key, self.value()?));
            self.skip();
            match self.bump()? {
                '}' => return Ok(Value::Object(pairs)),
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
            'u' => self.hex4(),
            _ => Err(bad("bad escape")),
        }
    }

    fn hex4(&mut self) -> Result<char, CoreError> {
        let mut code = 0u32;
        for _ in 0..4 {
            let c = self.bump()?;
            let digit = c.to_digit(16).ok_or_else(|| bad("bad unicode escape"))?;
            code = (code << 4) | digit;
        }
        char::from_u32(code).ok_or_else(|| bad("bad unicode escape"))
    }

    fn number(&mut self) -> Result<Value, CoreError> {
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
            Ok(Value::Float(v))
        } else {
            let v: i64 = text.parse().map_err(|_| bad("integer out of range"))?;
            Ok(Value::Int(v))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{Value, parse};

    #[test]
    fn reads_the_shapes_the_menu_uses() {
        let v = parse(br#"{"a": 1, "b": [true, false, null, "x\n", 1.5]}"#).unwrap();
        assert_eq!(v.get("a").and_then(Value::as_int), Some(1));
        let items = v.get("b").and_then(Value::as_array).unwrap();
        assert_eq!(items[0], Value::Bool(true));
        assert_eq!(items[3], Value::Str("x\n".into()));
        assert_eq!(items[4], Value::Float(1.5));
    }
}
