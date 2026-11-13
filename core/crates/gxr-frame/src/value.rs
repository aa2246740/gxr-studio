//! A small JSON reader for spec and layout input. No `serde`: core crates do not depend on it.
//!
//! Numbers with no fraction or exponent are integers, matching `json.loads`. `true` is a bool,
//! not an int (`type(True) is int` is false in the version check).

use gxr_raw::CoreError;

/// One JSON value.
#[derive(Clone, Debug, PartialEq)]
pub enum Val {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Arr(Vec<Val>),
    Obj(Vec<(String, Val)>),
}

impl Val {
    pub fn as_object(&self) -> Option<&[(String, Val)]> {
        match self {
            Self::Obj(pairs) => Some(pairs),
            _ => None,
        }
    }

    /// Last value for `key`, like a Python dict after `json.loads` (duplicates keep the last).
    pub fn get<'a>(&'a self, key: &str) -> Option<&'a Val> {
        self.as_object()?
            .iter()
            .rev()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v)
    }

    /// `self[i]` of an array.
    pub fn get_index(&self, i: usize) -> Option<&Val> {
        match self {
            Self::Arr(items) => items.get(i),
            _ => None,
        }
    }

    pub fn contains(&self, key: &str) -> bool {
        self.get(key).is_some()
    }

    pub fn keys(&self) -> Vec<&str> {
        let Some(pairs) = self.as_object() else {
            return Vec::new();
        };
        let mut names: Vec<&str> = pairs.iter().map(|(k, _)| k.as_str()).collect();
        names.sort_unstable();
        names.dedup();
        names
    }
}

/// `json.loads` of `bytes`, as UTF-8.
pub fn parse_json(bytes: &[u8]) -> Result<Val, CoreError> {
    let text = std::str::from_utf8(bytes).map_err(|_| json_err("JSON 不是 UTF-8"))?;
    let mut p = Parser { text, i: 0 };
    p.skip();
    let value = p.value()?;
    p.skip();
    if p.i != p.text.len() {
        return Err(json_err("JSON 后面还有多余内容"));
    }
    Ok(value)
}

struct Parser<'a> {
    text: &'a str,
    i: usize,
}

impl<'a> Parser<'a> {
    fn skip(&mut self) {
        while let Some(c) = self.peek() {
            if c == ' ' || c == '\n' || c == '\r' || c == '\t' {
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
        let c = self.peek().ok_or_else(|| json_err("JSON 在值的中途结束"))?;
        self.i += c.len_utf8();
        Ok(c)
    }

    fn value(&mut self) -> Result<Val, CoreError> {
        self.skip();
        match self.peek() {
            Some('n') => self.lit("null", Val::Null),
            Some('t') => self.lit("true", Val::Bool(true)),
            Some('f') => self.lit("false", Val::Bool(false)),
            Some('"') => Ok(Val::Str(self.string()?)),
            Some('[') => self.array(),
            Some('{') => self.object(),
            Some('-') | Some('0'..='9') => self.number(),
            _ => Err(json_err("JSON 里有无法识别的值")),
        }
    }

    fn lit(&mut self, word: &str, value: Val) -> Result<Val, CoreError> {
        if self.text[self.i..].starts_with(word) {
            self.i += word.len();
            Ok(value)
        } else {
            Err(json_err("JSON 里有无法识别的值"))
        }
    }

    fn array(&mut self) -> Result<Val, CoreError> {
        self.bump()?;
        let mut items = Vec::new();
        loop {
            self.skip();
            if self.peek() == Some(']') {
                self.bump()?;
                return Ok(Val::Arr(items));
            }
            if !items.is_empty() {
                self.comma()?;
            }
            items.push(self.value()?);
        }
    }

    fn object(&mut self) -> Result<Val, CoreError> {
        self.bump()?;
        let mut pairs = Vec::new();
        loop {
            self.skip();
            if self.peek() == Some('}') {
                self.bump()?;
                return Ok(Val::Obj(pairs));
            }
            if !pairs.is_empty() {
                self.comma()?;
            }
            self.skip();
            if self.peek() != Some('"') {
                return Err(json_err("对象的键必须是字符串"));
            }
            let key = self.string()?;
            self.skip();
            if self.bump()? != ':' {
                return Err(json_err("对象的键后面要有冒号"));
            }
            pairs.push((key, self.value()?));
        }
    }

    fn comma(&mut self) -> Result<(), CoreError> {
        self.skip();
        if self.bump()? != ',' {
            return Err(json_err("JSON 的项之间要有逗号"));
        }
        Ok(())
    }

    fn string(&mut self) -> Result<String, CoreError> {
        if self.bump()? != '"' {
            return Err(json_err("这里要有一个字符串"));
        }
        let mut out = String::new();
        loop {
            match self.bump()? {
                '"' => return Ok(out),
                '\\' => out.push(self.escape()?),
                c if c < '\u{0020}' => return Err(json_err("字符串里有未转义的控制字符")),
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
            _ => Err(json_err("字符串里有无法识别的转义")),
        }
    }

    fn unit(&mut self) -> Result<u32, CoreError> {
        let mut unit = 0u32;
        for _ in 0..4 {
            let c = self.bump()?;
            let dig = c
                .to_digit(16)
                .ok_or_else(|| json_err("\\u 后面要有四位十六进制"))?;
            unit = unit * 16 + dig;
        }
        Ok(unit)
    }

    /// `\uXXXX`, joining a UTF-16 surrogate pair the way `json.loads` does. A lone surrogate is
    /// legal in a Python `str` but not in a Rust one, so it is refused.
    fn hex4(&mut self) -> Result<char, CoreError> {
        let unit = self.unit()?;
        if (0xD800..0xDC00).contains(&unit) && self.text[self.i..].starts_with("\\u") {
            let save = self.i;
            self.i += 2;
            let low = self.unit()?;
            if (0xDC00..0xE000).contains(&low) {
                let code = 0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00);
                return char::from_u32(code).ok_or_else(|| json_err("\\u 不是一个字符"));
            }
            self.i = save;
        }
        char::from_u32(unit).ok_or_else(|| json_err("\\u 不是一个字符"))
    }

    fn number(&mut self) -> Result<Val, CoreError> {
        let start = self.i;
        if self.peek() == Some('-') {
            self.bump()?;
        }
        let digits = self.i;
        match self.peek() {
            Some('0') => {
                self.bump()?;
            }
            Some('1'..='9') => {
                while matches!(self.peek(), Some('0'..='9')) {
                    self.bump()?;
                }
            }
            _ => return Err(json_err("数字不完整")),
        }
        if matches!(self.peek(), Some('0'..='9')) && self.text.as_bytes().get(digits) == Some(&b'0')
        {
            return Err(json_err("数字不能有前导 0"));
        }
        let mut integer = true;
        if self.peek() == Some('.') {
            integer = false;
            self.bump()?;
            if !matches!(self.peek(), Some('0'..='9')) {
                return Err(json_err("小数点后面要有数字"));
            }
            while matches!(self.peek(), Some('0'..='9')) {
                self.bump()?;
            }
        }
        if matches!(self.peek(), Some('e') | Some('E')) {
            integer = false;
            self.bump()?;
            if matches!(self.peek(), Some('+') | Some('-')) {
                self.bump()?;
            }
            if !matches!(self.peek(), Some('0'..='9')) {
                return Err(json_err("指数后面要有数字"));
            }
            while matches!(self.peek(), Some('0'..='9')) {
                self.bump()?;
            }
        }
        let token = &self.text[start..self.i];
        if integer {
            if token == "-0" {
                return Ok(Val::Int(0));
            }
            let n = token.parse::<i64>().map_err(|_| json_err("整数超出范围"))?;
            return Ok(Val::Int(n));
        }
        let n = token
            .parse::<f64>()
            .map_err(|_| json_err("无法读取这个数字"))?;
        if !n.is_finite() {
            return Err(json_err("数字必须是有限的"));
        }
        Ok(Val::Float(n))
    }
}

fn json_err(message: &str) -> CoreError {
    CoreError::new("json", message)
}
