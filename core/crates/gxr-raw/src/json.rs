//! A small JSON writer shared by `gxr-cli` and `gxr-wasm`, so both print the same bytes.
//!
//! Format (docs/mobile.md): no whitespace; object keys in insertion order; strings as UTF-8 with
//! `"`, `\` and control characters escaped; floats with Rust's shortest round-trip digits
//! (`{}` on `f64`) plus `.0` when that text looks like an integer; non-finite floats as `NaN`,
//! `Infinity`, `-Infinity` (what Python's `json` writes and reads).

/// A JSON value.
#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Array(Vec<Json>),
    Object(Vec<(String, Json)>),
}

impl Json {
    /// An object from `(key, value)` pairs, keeping their order.
    pub fn object<K: Into<String>>(pairs: impl IntoIterator<Item = (K, Json)>) -> Self {
        Self::Object(pairs.into_iter().map(|(k, v)| (k.into(), v)).collect())
    }

    pub fn str(s: impl Into<String>) -> Self {
        Self::Str(s.into())
    }

    pub fn floats(values: &[f64]) -> Self {
        Self::Array(values.iter().map(|&v| Self::Float(v)).collect())
    }

    pub fn ints(values: &[i64]) -> Self {
        Self::Array(values.iter().map(|&v| Self::Int(v)).collect())
    }

    /// `None` → `null`.
    pub fn opt(value: Option<Json>) -> Self {
        value.unwrap_or(Self::Null)
    }

    /// The JSON text.
    pub fn render(&self) -> String {
        let mut out = String::new();
        self.write(&mut out);
        out
    }

    fn write(&self, out: &mut String) {
        match self {
            Self::Null => out.push_str("null"),
            Self::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Self::Int(i) => out.push_str(&i.to_string()),
            Self::Float(f) => out.push_str(&float_text(*f)),
            Self::Str(s) => write_str(out, s),
            Self::Array(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    item.write(out);
                }
                out.push(']');
            }
            Self::Object(pairs) => {
                out.push('{');
                for (i, (k, v)) in pairs.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_str(out, k);
                    out.push(':');
                    v.write(out);
                }
                out.push('}');
            }
        }
    }
}

/// A float as JSON text.
pub fn float_text(f: f64) -> String {
    if f.is_nan() {
        return "NaN".into();
    }
    if f.is_infinite() {
        return if f > 0.0 { "Infinity" } else { "-Infinity" }.into();
    }
    let s = format!("{f}");
    if s.contains('.') { s } else { s + ".0" }
}

fn write_str(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_compact_ordered_json() {
        let v = Json::object([
            ("b", Json::Int(-3)),
            ("a", Json::floats(&[4095.0, 0.1, -0.0, 1e-7, f64::NAN])),
            ("s", Json::str("这\"\\\n\u{1}")),
            ("n", Json::Null),
            ("t", Json::Bool(true)),
        ]);
        assert_eq!(
            v.render(),
            "{\"b\":-3,\"a\":[4095.0,0.1,-0.0,0.0000001,NaN],\"s\":\"这\\\"\\\\\\n\\u0001\",\"n\":null,\"t\":true}"
        );
    }
}
