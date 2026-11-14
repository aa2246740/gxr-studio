//! Layout items: the Python dicts the style functions build, kept as ordered key/value pairs so
//! `to_json` prints the same keys in the same order with the same int/float types.

use gxr_raw::{CoreError, Json};

use crate::py;
use crate::value::Val;

/// A Python number that may be an `int` or a `float`; arithmetic keeps Python's result type.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Num {
    I(i64),
    F(f64),
}

impl Num {
    /// `float(self)`.
    pub fn f(self) -> f64 {
        match self {
            Self::I(n) => py::f(n),
            Self::F(v) => v,
        }
    }

    /// Python `round(self)`: an int stays, a float rounds half to even.
    pub fn round(self) -> Result<i64, CoreError> {
        match self {
            Self::I(n) => Ok(n),
            Self::F(v) => py::round(v),
        }
    }

    pub fn val(self) -> Val {
        match self {
            Self::I(n) => Val::Int(n),
            Self::F(v) => Val::Float(v),
        }
    }
}

/// A dict literal.
pub fn obj(pairs: Vec<(&str, Val)>) -> Val {
    Val::Obj(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

/// A colour tuple, which `json.dumps` writes as a list.
pub fn rgb(c: [i64; 3]) -> Val {
    Val::Arr(c.iter().map(|&v| Val::Int(v)).collect())
}

pub fn int(n: i64) -> Val {
    Val::Int(n)
}

pub fn float(v: f64) -> Val {
    Val::Float(v)
}

pub fn text(s: &str) -> Val {
    Val::Str(s.to_string())
}

pub fn ints(values: &[i64]) -> Val {
    Val::Arr(values.iter().map(|&v| Val::Int(v)).collect())
}

/// `item[key] = value`: replace in place, or append a new key at the end.
pub fn set(item: &mut Val, key: &str, value: Val) {
    if let Val::Obj(pairs) = item {
        if let Some(slot) = pairs.iter_mut().find(|(k, _)| k == key) {
            slot.1 = value;
        } else {
            pairs.push((key.to_string(), value));
        }
    }
}

/// `{**item, key: value}`.
pub fn with(item: &Val, key: &str, value: Val) -> Val {
    let mut out = item.clone();
    set(&mut out, key, value);
    out
}

fn missing(key: &str) -> CoreError {
    CoreError::new("key_error", format!("'{key}'"))
}

/// `item[key]` as a number.
pub fn num(item: &Val, key: &str) -> Result<Num, CoreError> {
    match item.get(key) {
        Some(Val::Int(n)) => Ok(Num::I(*n)),
        Some(Val::Float(v)) => Ok(Num::F(*v)),
        Some(_) => Err(CoreError::new(
            "type_error",
            format!("{key} is not a number"),
        )),
        None => Err(missing(key)),
    }
}

/// `item[key]` as an int.
pub fn int_of(item: &Val, key: &str) -> Result<i64, CoreError> {
    match num(item, key)? {
        Num::I(n) => Ok(n),
        Num::F(_) => Err(CoreError::new("type_error", format!("{key} is not an int"))),
    }
}

/// `item[key]` as a string.
pub fn str_of<'a>(item: &'a Val, key: &str) -> Result<&'a str, CoreError> {
    match item.get(key) {
        Some(Val::Str(s)) => Ok(s),
        Some(_) => Err(CoreError::new(
            "type_error",
            format!("{key} is not a string"),
        )),
        None => Err(missing(key)),
    }
}

/// Truthiness of `item.get(key)`.
pub fn truthy(item: &Val, key: &str) -> bool {
    match item.get(key) {
        None | Some(Val::Null) | Some(Val::Bool(false)) | Some(Val::Int(0)) => false,
        Some(Val::Float(v)) => *v != 0.0,
        Some(Val::Str(s)) => !s.is_empty(),
        Some(Val::Arr(a)) => !a.is_empty(),
        Some(Val::Obj(o)) => !o.is_empty(),
        Some(_) => true,
    }
}

/// The `json.dumps` text model of a value.
pub fn to_json(value: &Val) -> Json {
    match value {
        Val::Null => Json::Null,
        Val::Bool(b) => Json::Bool(*b),
        Val::Int(n) => Json::Int(*n),
        Val::Float(v) => Json::Float(*v),
        Val::Str(s) => Json::Str(s.clone()),
        Val::Arr(items) => Json::Array(items.iter().map(to_json).collect()),
        Val::Obj(pairs) => {
            Json::Object(pairs.iter().map(|(k, v)| (k.clone(), to_json(v))).collect())
        }
    }
}
