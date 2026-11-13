//! A small JSON reader for the binding inputs (`Params`, `GainMeta`, EXIF tag maps), shared
//! by `gxr-cli` and `gxr-wasm` so both refuse bad input with the same text. Core crates
//! take no serde (CONVENTIONS §4). Numbers parse with Rust's correctly rounded `f64`
//! parser, which gives the same value as Python's `json`.

use gxr_raw::CoreError;

use crate::capture::TagValue;
use crate::container::GainMeta;
use crate::curve::Params;

/// A parsed JSON value. Integers without a fraction or exponent keep their exact value.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Arr(Vec<Value>),
    Obj(Vec<(String, Value)>),
}

fn bad(message: impl Into<String>) -> CoreError {
    CoreError::new("bad_input", message)
}

struct Reader<'a> {
    s: &'a [u8],
    i: usize,
}

impl Reader<'_> {
    fn ws(&mut self) {
        while self.i < self.s.len() && matches!(self.s[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn eat(&mut self, c: u8) -> Result<(), CoreError> {
        self.ws();
        if self.s.get(self.i) == Some(&c) {
            self.i += 1;
            Ok(())
        } else {
            Err(bad(format!("JSON 第 {} 个字节应该是 {:?}", self.i, char::from(c))))
        }
    }

    fn value(&mut self, depth: usize) -> Result<Value, CoreError> {
        if depth > 32 {
            return Err(bad("JSON 嵌套太深"));
        }
        self.ws();
        match self.s.get(self.i) {
            Some(b'{') => {
                self.i += 1;
                let mut out = Vec::new();
                self.ws();
                if self.s.get(self.i) == Some(&b'}') {
                    self.i += 1;
                    return Ok(Value::Obj(out));
                }
                loop {
                    self.ws();
                    let Value::Str(k) = self.value(depth + 1)? else {
                        return Err(bad("JSON 对象的键必须是字符串"));
                    };
                    self.eat(b':')?;
                    out.push((k, self.value(depth + 1)?));
                    self.ws();
                    match self.s.get(self.i) {
                        Some(b',') => self.i += 1,
                        Some(b'}') => {
                            self.i += 1;
                            return Ok(Value::Obj(out));
                        }
                        _ => return Err(bad("JSON 对象没有结束")),
                    }
                }
            }
            Some(b'[') => {
                self.i += 1;
                let mut out = Vec::new();
                self.ws();
                if self.s.get(self.i) == Some(&b']') {
                    self.i += 1;
                    return Ok(Value::Arr(out));
                }
                loop {
                    out.push(self.value(depth + 1)?);
                    self.ws();
                    match self.s.get(self.i) {
                        Some(b',') => self.i += 1,
                        Some(b']') => {
                            self.i += 1;
                            return Ok(Value::Arr(out));
                        }
                        _ => return Err(bad("JSON 数组没有结束")),
                    }
                }
            }
            Some(b'"') => self.string(),
            Some(b't') => self.word("true", Value::Bool(true)),
            Some(b'f') => self.word("false", Value::Bool(false)),
            Some(b'n') => self.word("null", Value::Null),
            Some(b'N') => self.word("NaN", Value::Float(f64::NAN)),
            Some(b'I') => self.word("Infinity", Value::Float(f64::INFINITY)),
            _ => self.number(),
        }
    }

    fn word(&mut self, w: &str, v: Value) -> Result<Value, CoreError> {
        if self.s[self.i..].starts_with(w.as_bytes()) {
            self.i += w.len();
            Ok(v)
        } else {
            Err(bad("JSON 有不认识的字"))
        }
    }

    fn number(&mut self) -> Result<Value, CoreError> {
        let start = self.i;
        if self.s[self.i..].starts_with(b"-Infinity") {
            self.i += 9;
            return Ok(Value::Float(f64::NEG_INFINITY));
        }
        while self.i < self.s.len() && matches!(self.s[self.i], b'0'..=b'9' | b'-' | b'+' | b'.' | b'e' | b'E') {
            self.i += 1;
        }
        let text = std::str::from_utf8(&self.s[start..self.i]).map_err(|_| bad("JSON 数字不对"))?;
        if text.is_empty() {
            return Err(bad("JSON 有不认识的字"));
        }
        if !text.contains(['.', 'e', 'E'])
            && let Ok(n) = text.parse::<i64>()
        {
            return Ok(Value::Int(n));
        }
        text.parse::<f64>()
            .map(Value::Float)
            .map_err(|_| bad(format!("JSON 数字不对：{text}")))
    }

    fn string(&mut self) -> Result<Value, CoreError> {
        self.i += 1;
        let mut out: Vec<u16> = Vec::new();
        let mut bytes: Vec<u8> = Vec::new();
        let flush = |bytes: &mut Vec<u8>, out: &mut Vec<u16>| -> Result<(), CoreError> {
            let s = std::str::from_utf8(bytes).map_err(|_| bad("JSON 字符串不是 UTF-8"))?;
            out.extend(s.encode_utf16());
            bytes.clear();
            Ok(())
        };
        loop {
            let c = *self.s.get(self.i).ok_or_else(|| bad("JSON 字符串没有结束"))?;
            self.i += 1;
            match c {
                b'"' => break,
                b'\\' => {
                    flush(&mut bytes, &mut out)?;
                    let e = *self.s.get(self.i).ok_or_else(|| bad("JSON 字符串没有结束"))?;
                    self.i += 1;
                    let unit = match e {
                        b'"' => 0x22,
                        b'\\' => 0x5c,
                        b'/' => 0x2f,
                        b'b' => 0x08,
                        b'f' => 0x0c,
                        b'n' => 0x0a,
                        b'r' => 0x0d,
                        b't' => 0x09,
                        b'u' => {
                            let hex = self.s.get(self.i..self.i + 4).ok_or_else(|| bad("JSON \\u 不完整"))?;
                            self.i += 4;
                            let hex = std::str::from_utf8(hex).map_err(|_| bad("JSON \\u 不对"))?;
                            u16::from_str_radix(hex, 16).map_err(|_| bad("JSON \\u 不对"))?
                        }
                        _ => return Err(bad("JSON 转义不对")),
                    };
                    out.push(unit);
                }
                _ => bytes.push(c),
            }
        }
        flush(&mut bytes, &mut out)?;
        String::from_utf16(&out)
            .map(Value::Str)
            .map_err(|_| bad("JSON 字符串里有落单的代理项"))
    }
}

/// Parse one JSON document.
pub fn parse(text: &str) -> Result<Value, CoreError> {
    let mut r = Reader { s: text.as_bytes(), i: 0 };
    let v = r.value(0)?;
    r.ws();
    if r.i != r.s.len() {
        return Err(bad("JSON 后面还有多余的内容"));
    }
    Ok(v)
}

fn object(text: &str) -> Result<Vec<(String, Value)>, CoreError> {
    match parse(text)? {
        Value::Obj(pairs) => Ok(pairs),
        _ => Err(bad("需要一个 JSON 对象")),
    }
}

fn float(key: &str, v: &Value) -> Result<f64, CoreError> {
    match v {
        #[allow(clippy::cast_precision_loss)]
        Value::Int(n) => Ok(*n as f64),
        Value::Float(f) => Ok(*f),
        _ => Err(bad(format!("{key} 必须是数字"))),
    }
}

fn int(key: &str, v: &Value) -> Result<i64, CoreError> {
    match v {
        Value::Int(n) => Ok(*n),
        _ => Err(bad(format!("{key} 必须是整数"))),
    }
}

/// `Params(**obj)`: unknown keys are refused, missing keys keep the defaults.
pub fn params_from_json(text: &str) -> Result<Params, CoreError> {
    let mut p = Params::default();
    for (k, v) in object(text)? {
        match k.as_str() {
            "map_factor" => p.map_factor = int(&k, &v)?,
            "max_stops" => p.max_stops = float(&k, &v)?,
            "knee" => p.knee = float(&k, &v)?,
            "hdr_slope" => p.hdr_slope = float(&k, &v)?,
            "gate_lo" => p.gate_lo = float(&k, &v)?,
            "gate_hi" => p.gate_hi = float(&k, &v)?,
            "quality" => p.quality = int(&k, &v)?,
            _ => return Err(bad(format!("Params 没有 {k}"))),
        }
    }
    Ok(p)
}

/// `GainMeta(**obj)`: unknown keys are refused, missing keys keep the defaults.
pub fn meta_from_json(text: &str) -> Result<GainMeta, CoreError> {
    let mut m = GainMeta::default();
    for (k, v) in object(text)? {
        let x = float(&k, &v)?;
        match k.as_str() {
            "gain_min" => m.gain_min = x,
            "gain_max" => m.gain_max = x,
            "gamma" => m.gamma = x,
            "offset_sdr" => m.offset_sdr = x,
            "offset_hdr" => m.offset_hdr = x,
            "cap_min" => m.cap_min = x,
            "cap_max" => m.cap_max = x,
            _ => return Err(bad(format!("GainMeta 没有 {k}"))),
        }
    }
    Ok(m)
}

fn tag_value(v: Value) -> Result<TagValue, CoreError> {
    Ok(match v {
        Value::Null => TagValue::None,
        Value::Bool(b) => TagValue::Bool(b),
        Value::Int(n) => TagValue::Int(n),
        Value::Float(f) => TagValue::Float(f),
        Value::Str(s) => TagValue::Text(s),
        Value::Arr(items) => TagValue::List(items.into_iter().map(tag_value).collect::<Result<_, _>>()?),
        Value::Obj(_) => return Err(bad("标签值不能是对象")),
    })
}

/// `{"<tag number>": value, ...}` → the tag list `_capture` reads.
pub fn tags_from_json(text: &str) -> Result<Vec<(u32, TagValue)>, CoreError> {
    object(text)?
        .into_iter()
        .map(|(k, v)| {
            let tag = k.parse::<u32>().map_err(|_| bad(format!("标签号不对：{k}")))?;
            Ok((tag, tag_value(v)?))
        })
        .collect()
}
